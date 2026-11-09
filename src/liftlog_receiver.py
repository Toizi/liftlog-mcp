"""Receives LiftLog remote backups (POST /backup) and stores them as files.

Write-only and dependency-free: it authenticates with a shared X-API-Key,
checks the gzip magic bytes, and never decompresses or parses the upload.
See docs/RemoteBackup.md in the LiftLog repository for the protocol.
"""

import hmac
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKUP_DIR = Path(os.environ["LIFTLOG_BACKUP_DIR"])
API_KEY = os.environ.get("RECEIVER_API_KEY", "").encode()
PORT = int(os.environ.get("LIFTLOG_RECEIVER_PORT", "3010"))
MAX_BYTES = int(os.environ.get("LIFTLOG_MAX_BYTES", str(256 * 1024 * 1024)))
KEEP = int(os.environ.get("LIFTLOG_KEEP", "20"))
CHUNK = 1024 * 1024
SUFFIX = ".liftlogbackup.gz"

store_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    timeout = 60

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        if self.path != "/backup":
            return self.reply(404)
        sent_key = self.headers.get("X-API-Key", "").encode()
        if not hmac.compare_digest(sent_key, API_KEY):
            return self.reply(401)
        if self.headers.get("X-LiftLog-Probe"):
            return self.reply(200)

        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411)
        if length < 2:
            return self.reply(400)
        if length > MAX_BYTES:
            return self.reply(413)

        with tempfile.NamedTemporaryFile(dir=BACKUP_DIR, prefix=".incoming-", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                remaining = length
                first = True
                while remaining:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("client closed before sending the full body")
                    if first and chunk[:2] != b"\x1f\x8b":
                        tmp_path.unlink()
                        return self.reply(400)
                    first = False
                    tmp.write(chunk)
                    remaining -= len(chunk)
            except (ConnectionError, TimeoutError, OSError):
                tmp_path.unlink(missing_ok=True)
                return self.reply(400)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        with store_lock:
            tmp_path.rename(BACKUP_DIR / (stamp + SUFFIX))
            for old in sorted(BACKUP_DIR.glob("*" + SUFFIX))[:-KEEP]:
                old.unlink(missing_ok=True)
        self.reply(200)

    def do_GET(self):
        self.reply(405)


if __name__ == "__main__":
    if not API_KEY:
        sys.exit("RECEIVER_API_KEY is not set")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
