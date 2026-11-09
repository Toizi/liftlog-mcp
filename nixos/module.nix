# Hardened systemd services for the LiftLog backup receiver and read-only MCP
# server. Reverse proxying (Caddy), firewall and DNS are left to the host.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.liftlog;
  pkg = self.packages.${pkgs.stdenv.hostPlatform.system}.default;

  hardening = {
    User = "liftlog";
    Group = "liftlog";
    Restart = "always";
    RestartSec = 5;
    UMask = "0027";
    CapabilityBoundingSet = "";
    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    NoNewPrivileges = true;
    PrivateTmp = true;
    PrivateDevices = true;
    PrivateUsers = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectClock = true;
    ProtectControlGroups = true;
    ProtectKernelLogs = true;
    ProtectKernelModules = true;
    ProtectKernelTunables = true;
    ProtectProc = "invisible";
    ProtectHostname = true;
    RemoveIPC = true;
    RestrictNamespaces = true;
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" ];
    RestrictSUIDSGID = true;
    RestrictRealtime = true;
    SystemCallArchitectures = "native";
    SystemCallFilter = [ "@system-service" ];
  };
in
{
  options.services.liftlog = {
    receiverEnvironmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Environment file with RECEIVER_API_KEY. Receiver is disabled while null.";
    };
    mcpEnvironmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Environment file with MCP_API_KEY. MCP server is disabled while null.";
    };
    backupDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/liftlog/backups";
    };
    receiverPort = lib.mkOption {
      type = lib.types.port;
      default = 3010;
    };
    mcpPort = lib.mkOption {
      type = lib.types.port;
      default = 3011;
      description = "Listen port of the MCP server (bound to localhost; proxy it).";
    };
  };

  config = lib.mkIf (cfg.receiverEnvironmentFile != null || cfg.mcpEnvironmentFile != null) {
    users.users.liftlog = {
      isSystemUser = true;
      group = "liftlog";
    };
    users.groups.liftlog = { };

    systemd.tmpfiles.rules = [
      "d /var/lib/liftlog 0750 liftlog liftlog -"
      "d ${cfg.backupDir} 0750 liftlog liftlog -"
    ];

    systemd.services.liftlog-receiver = lib.mkIf (cfg.receiverEnvironmentFile != null) {
      description = "LiftLog backup receiver";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "systemd-tmpfiles-setup.service" ];
      environment = {
        LIFTLOG_BACKUP_DIR = cfg.backupDir;
        LIFTLOG_RECEIVER_PORT = toString cfg.receiverPort;
      };
      serviceConfig = hardening // {
        ExecStart = "${pkg}/bin/liftlog-receiver";
        EnvironmentFile = cfg.receiverEnvironmentFile;
        ReadWritePaths = [ cfg.backupDir ];
      };
    };

    # No ReadWritePaths: ProtectSystem=strict leaves the backups read-only here.
    systemd.services.liftlog-mcp = lib.mkIf (cfg.mcpEnvironmentFile != null) {
      description = "LiftLog MCP server (read-only)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "systemd-tmpfiles-setup.service" ];
      environment = {
        LIFTLOG_BACKUP_DIR = cfg.backupDir;
        LIFTLOG_MCP_PORT = toString cfg.mcpPort;
      };
      serviceConfig = hardening // {
        ExecStart = "${pkg}/bin/liftlog-mcp";
        EnvironmentFile = cfg.mcpEnvironmentFile;
      };
    };
  };
}
