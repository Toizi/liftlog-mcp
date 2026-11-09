"""Read-only MCP server (streamable HTTP, stateless) over the newest LiftLog backup.

Dependency-free. The newest *.liftlogbackup.gz in LIFTLOG_BACKUP_DIR is
gunzipped into an in-memory SQLite database that is only ever queried.

Auth: `initialize`, `tools/list` and `ping` are open so a connector wizard can
probe the server; `tools/call` needs the MCP_API_KEY (X-API-Key or
Authorization: Bearer).
"""

import gzip
import time
import hmac
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKUP_DIR = Path(os.environ["LIFTLOG_BACKUP_DIR"])
API_KEY = os.environ.get("MCP_API_KEY", "")
PORT = int(os.environ.get("LIFTLOG_MCP_PORT", "3011"))
MAX_BODY = 1024 * 1024
SUFFIX = ".liftlogbackup.gz"
# Tables the model may read. Everything else in the backup (feed_*, backend_*
# with auth headers, ...) is private and denied by the SQLite authorizer.
READABLE = {
    "session",
    "recorded_exercise",
    "weighted_set",
    "cardio_set",
    "program",
    "effective_weighted_set",
}
QUERY_MAX_ROWS = 500
QUERY_MAX_BYTES = 200 * 1024
QUERY_TIMEOUT_S = 5.0
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    pass


# Monotonic deadline of the running query (None between queries); only touched
# while Backup.lock is held.
deadline = None


def check_deadline():
    """SQLite progress handler: non-zero aborts the running statement."""
    return 1 if deadline is not None and time.monotonic() > deadline else 0


def authorize(action, arg1, arg2, dbname, source):
    """Allow SELECT over READABLE tables only: no writes, PRAGMA, ATTACH, sqlite_master."""
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in READABLE else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class Backup:
    """Newest backup as an in-memory database; reopened when a newer file lands."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name = None
        self.db = None
        self.schema = []

    def current(self):
        files = sorted(BACKUP_DIR.glob("*" + SUFFIX))
        if not files:
            raise ToolError("No backup has been uploaded yet.")
        newest = files[-1]
        if newest.name != self.name:
            db = sqlite3.connect(":memory:", check_same_thread=False)
            db.deserialize(gzip.decompress(newest.read_bytes()))
            db.row_factory = sqlite3.Row
            self.schema = [
                r[0]
                for r in db.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN (%s) "
                    "ORDER BY type DESC, name" % ",".join("?" * len(READABLE)),
                    sorted(READABLE),
                )
            ]
            db.execute("PRAGMA query_only = ON")
            db.set_authorizer(authorize)
            db.set_progress_handler(check_deadline, 10000)
            if self.db:
                self.db.close()
            self.db, self.name = db, newest.name
        return self.db, newest


backup = Backup()

# A session counts as performed once any of its sets was completed.
PERFORMED = """EXISTS (
    SELECT 1 FROM weighted_set w WHERE w.sessionId = s.id AND w.completedAtMs IS NOT NULL
    UNION ALL
    SELECT 1 FROM cardio_set c WHERE c.sessionId = s.id AND c.completedAtMs IS NOT NULL
)"""


def clamp(value, default, maximum):
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def like_pattern(text):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def exercise_json(db, session_id, exercise):
    ord_ = exercise["ord"]
    weighted = db.execute(
        """SELECT targetMin, targetMax, weightValue, weightUnit, weightKg, repsCompleted
           FROM weighted_set
           WHERE sessionId = ? AND exerciseOrd = ? AND completedAtMs IS NOT NULL
           ORDER BY ord""",
        (session_id, ord_),
    ).fetchall()
    cardio = db.execute(
        """SELECT durationMs, distanceValue, distanceUnit, resistance, incline, steps
           FROM cardio_set
           WHERE sessionId = ? AND exerciseOrd = ? AND completedAtMs IS NOT NULL
           ORDER BY ord""",
        (session_id, ord_),
    ).fetchall()
    if not weighted and not cardio:
        return None
    out = {"name": exercise["name"]}
    if exercise["resistance"]:
        out["resistance"] = exercise["resistance"]
    if exercise["notes"]:
        out["notes"] = exercise["notes"]
    if weighted:
        out["sets"] = [
            {
                "weight": s["weightValue"],
                "unit": s["weightUnit"],
                "kg": round(s["weightKg"], 2) if s["weightKg"] is not None else None,
                "reps": s["repsCompleted"],
                "target": str(s["targetMin"])
                if s["targetMin"] == s["targetMax"]
                else "%s-%s" % (s["targetMin"], s["targetMax"]),
            }
            for s in weighted
        ]
    if cardio:
        out["cardio"] = [
            {k: v for k, v in dict(c).items() if v is not None} for c in cardio
        ]
    return out


def session_json(db, s, only_exercise_ids=None):
    exercises = db.execute(
        "SELECT ord, name, notes, resistance FROM recorded_exercise WHERE sessionId = ? ORDER BY ord",
        (s["id"],),
    ).fetchall()
    out = {
        "id": s["id"],
        "date": s["date"],
        "name": s["name"],
        "exercises": [
            e
            for e in (
                exercise_json(db, s["id"], ex)
                for ex in exercises
                if only_exercise_ids is None or ex["ord"] in only_exercise_ids
            )
            if e
        ],
    }
    if s["notes"]:
        out["notes"] = s["notes"]
    if s["bodyweightValue"]:
        out["bodyweight"] = "%s %s" % (s["bodyweightValue"], s["bodyweightUnit"])
    return out


def tool_get_backup_info(db, newest, args):
    return {
        "backup_file": newest.name,
        "backup_size_bytes": newest.stat().st_size,
        "performed_sessions": db.execute(
            "SELECT COUNT(*) FROM session s WHERE " + PERFORMED
        ).fetchone()[0],
        "first_session_date": db.execute(
            "SELECT MIN(date) FROM session s WHERE " + PERFORMED
        ).fetchone()[0],
        "last_session_date": db.execute(
            "SELECT MAX(date) FROM session s WHERE " + PERFORMED
        ).fetchone()[0],
        "plans": db.execute("SELECT COUNT(*) FROM program").fetchone()[0],
    }


def tool_get_recent_sessions(db, newest, args):
    limit = clamp(args.get("limit"), 10, 50)
    rows = db.execute(
        "SELECT * FROM session s WHERE " + PERFORMED
        + " ORDER BY s.date DESC, s.finishedAtMs DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [session_json(db, s) for s in rows]


def tool_get_exercise_history(db, newest, args):
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolError("`name` is required.")
    limit = clamp(args.get("limit"), 10, 50)
    rows = db.execute(
        """SELECT s.*, re.ord AS exerciseOrd
           FROM recorded_exercise re JOIN session s ON s.id = re.sessionId
           WHERE re.name LIKE ? ESCAPE '\\' AND """ + PERFORMED + """
           ORDER BY s.date DESC, s.finishedAtMs DESC LIMIT ?""",
        (like_pattern(name.strip()), limit),
    ).fetchall()
    result = []
    for s in rows:
        entry = session_json(db, s, only_exercise_ids={s["exerciseOrd"]})
        if entry["exercises"]:
            result.append(entry)
    return result


def tool_list_exercises(db, newest, args):
    limit = clamp(args.get("limit"), 100, 500)
    rows = db.execute(
        """SELECT MAX(re.name) AS name,
                  COUNT(DISTINCT re.sessionId) AS sessions,
                  MAX(s.date) AS last_performed,
                  ROUND(MAX(re.bestOneRepMaxKg), 1) AS best_e1rm_kg,
                  ROUND(MAX(re.topWeightKg), 1) AS top_weight_kg
           FROM recorded_exercise re JOIN session s ON s.id = re.sessionId
           WHERE """ + PERFORMED + """
           GROUP BY re.movementKey
           ORDER BY last_performed DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def plan_json(row):
    payload = json.loads(row["payload"])
    return {"id": row["id"], "active": bool(row["active"]), "plan": payload}


def tool_get_plans(db, newest, args):
    out = []
    for row in db.execute("SELECT id, active, payload FROM program ORDER BY active DESC"):
        payload = json.loads(row["payload"])
        out.append(
            {
                "id": row["id"],
                "active": bool(row["active"]),
                "name": payload.get("name"),
                "version": payload.get("version"),
                "sessions": [s.get("name") for s in payload.get("sessions", [])],
            }
        )
    return out


def tool_get_plan(db, newest, args):
    plan_id = args.get("id")
    if plan_id:
        row = db.execute("SELECT id, active, payload FROM program WHERE id = ?", (plan_id,)).fetchone()
    else:
        row = db.execute("SELECT id, active, payload FROM program WHERE active = 1").fetchone()
    if not row:
        raise ToolError("No such plan." if plan_id else "There is no active plan.")
    return plan_json(row)


SCHEMA_NOTES = """\
Dates: session.date is 'YYYY-MM-DD'; *AtMs columns are epoch milliseconds.
A session counts as PERFORMED only if at least one of its weighted_set/cardio_set rows has completedAtMs IS NOT NULL; unperformed (planned/skipped) sessions also exist, so always filter on it:
  EXISTS (SELECT 1 FROM weighted_set w WHERE w.sessionId = s.id AND w.completedAtMs IS NOT NULL)
Only completed sets (completedAtMs IS NOT NULL) are real data; uncompleted sets just carry the planned target.
Weights: weightValue is text in weightUnit ('kilograms'/'pounds'); use the generated weightKg column. For bodyweight/assisted exercises use effective_weighted_set.effectiveKg (adds session bodyweight; resistance 'none' = 0). recorded_exercise.resistance is 'external', 'bodyweight' or 'none'.
recorded_exercise is one exercise in one session (PK sessionId+ord). Per-exercise precomputed stats: bestOneRepMaxKg (best estimated 1RM), topWeightKg, workingWeightKg, totalReps, bestSetReps. Group the same lift across sessions by movementKey (names can vary; progressionKey also encodes the rep scheme).
weighted_set.targetMin/targetMax is the planned rep range; repsCompleted is what was done.
session.volumeKg is total volume of the session; bodyweightKg is virtual (NULL when not logged).
program.payload is the plan JSON (use json_extract); program.active = 1 is the active plan.
recorded_exercise.plannedSets and progression are JSON text.
Example, weekly best e1RM for an exercise:
  SELECT strftime('%Y-%W', s.date) AS week, ROUND(MAX(re.bestOneRepMaxKg),1) AS e1rm
  FROM recorded_exercise re JOIN session s ON s.id = re.sessionId
  WHERE re.movementKey LIKE '%bench%' AND re.bestOneRepMaxKg IS NOT NULL GROUP BY week ORDER BY week
"""


def tool_get_schema(db, newest, args):
    return {"notes": SCHEMA_NOTES, "schema": backup.schema}


def tool_query(db, newest, args):
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ToolError("`sql` is required.")
    global deadline
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    try:
        cur = db.execute(sql)
        columns = [c[0] for c in cur.description or []]
        rows, size, truncated = [], 0, False
        for row in cur:
            if len(rows) >= QUERY_MAX_ROWS:
                truncated = True
                break
            row = list(row)
            size += len(json.dumps(row, separators=(",", ":"), default=str))
            if size > QUERY_MAX_BYTES:
                truncated = True
                break
            rows.append(row)
    except sqlite3.Error as e:
        raise ToolError("SQL error: %s" % e)
    finally:
        deadline = None
    out = {"columns": columns, "rows": rows}
    if truncated:
        out["truncated"] = "Output cut at %d rows / %d KB; add LIMIT, filters or aggregation." % (
            QUERY_MAX_ROWS,
            QUERY_MAX_BYTES // 1024,
        )
    return out


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required)}


TOOLS = [
    (
        "get_backup_info",
        "When the data was last backed up, plus how many sessions and plans it contains. Check this first to judge how fresh the data is.",
        schema(),
        tool_get_backup_info,
    ),
    (
        "get_recent_sessions",
        "The most recent performed workouts, newest first, with every completed set (weight, unit, reps vs target) and cardio set.",
        schema({"limit": {"type": "integer", "description": "How many sessions, default 10, max 50."}}),
        tool_get_recent_sessions,
    ),
    (
        "get_exercise_history",
        "Performed history of one exercise across sessions, newest first. `name` is a case-insensitive substring match on the exercise name.",
        schema(
            {
                "name": {"type": "string", "description": "Exercise name or part of it, e.g. 'bench'."},
                "limit": {"type": "integer", "description": "How many sessions, default 10, max 50."},
            },
            required=["name"],
        ),
        tool_get_exercise_history,
    ),
    (
        "list_exercises",
        "Every exercise ever performed with session count, last date, best estimated 1RM and top weight (kg). Use it to find exact exercise names.",
        schema({"limit": {"type": "integer", "description": "Default 100, max 500."}}),
        tool_list_exercises,
    ),
    (
        "get_plans",
        "All saved workout plans (id, name, whether active, session names).",
        schema(),
        tool_get_plans,
    ),
    (
        "get_plan",
        "A full workout plan in LiftLog's plan JSON format. Without `id` returns the active plan.",
        schema({"id": {"type": "string", "description": "Plan id from get_plans."}}),
        tool_get_plan,
    ),
    (
        "get_schema",
        "Schema of the queryable tables plus semantics notes (what counts as a performed session, weight units, JSON columns, an example query). Read this before using `query`.",
        schema(),
        tool_get_schema,
    ),
    (
        "query",
        "Run one read-only SQLite SELECT (or WITH ... SELECT) over the workout history for custom analysis: progression trends, weekly volume, PRs, stalls, adherence. Only workout tables are readable (see get_schema). Max %d rows / %d KB / %ds per query; aggregate in SQL instead of pulling raw rows."
        % (QUERY_MAX_ROWS, QUERY_MAX_BYTES // 1024, QUERY_TIMEOUT_S),
        schema({"sql": {"type": "string", "description": "A single SELECT statement."}}, required=["sql"]),
        tool_query,
    ),
]
TOOL_BY_NAME = {name: fn for name, _, _, fn in TOOLS}


def call_tool(name, args):
    if name not in TOOL_BY_NAME:
        raise ToolError("Unknown tool: %s" % name)
    fn = TOOL_BY_NAME[name]
    with backup.lock:
        db, newest = backup.current()
        return fn(db, newest, args or {})


class AuthRequired(Exception):
    pass


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_rpc(msg, authed):
    if not isinstance(msg, dict) or "method" not in msg:
        return None
    method, request_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if request_id is None:
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOLS else PROTOCOLS[0]
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "liftlog", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": desc,
                    "inputSchema": sch,
                    "annotations": {"readOnlyHint": True, "openWorldHint": False},
                }
                for name, desc, sch, _ in TOOLS
            ]
        }
    elif method == "tools/call":
        if not authed:
            raise AuthRequired()
        try:
            data = call_tool(params.get("name"), params.get("arguments"))
            result = {"content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"))}]}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:
            sys.stderr.write("tool %s failed: %r\n" % (params.get("name"), e))
            result = {"content": [{"type": "text", "text": "Internal error reading the backup."}], "isError": True}
    else:
        return rpc_error(request_id, -32601, "Method not found: %s" % method)
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def equal(a, b):
    return bool(b) and hmac.compare_digest(a.encode(), b.encode())


class Handler(BaseHTTPRequestHandler):
    timeout = 60

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (self.address_string(), self.command, self.path.split("/")[1:2]))

    def respond(self, status, body=None, extra=()):
        payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def header_key_ok(self):
        sent = self.headers.get("X-API-Key") or ""
        bearer = self.headers.get("Authorization") or ""
        if bearer.startswith("Bearer "):
            sent = sent or bearer[len("Bearer "):]
        return equal(sent, API_KEY)

    def do_GET(self):
        self.respond(405, extra=[("Allow", "POST")])

    do_DELETE = do_GET

    def do_POST(self):
        if self.path != "/mcp":
            return self.respond(404)
        authed = self.header_key_ok()

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.respond(411)
        if length > MAX_BODY:
            return self.respond(413)
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self.respond(400, rpc_error(None, -32700, "Parse error"))

        try:
            if isinstance(body, list):
                replies = [r for r in (handle_rpc(m, authed) for m in body) if r]
                return self.respond(200, replies) if replies else self.respond(202)
            reply = handle_rpc(body, authed)
        except AuthRequired:
            return self.respond(401, rpc_error(None, -32001, "Unauthorized"))
        if reply is None:
            return self.respond(202)
        self.respond(200, reply)


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("MCP_API_KEY is not set")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
