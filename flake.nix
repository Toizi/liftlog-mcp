{
  description = "LiftLog backup receiver and read-only MCP server";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system}.default = pkgs.stdenvNoCC.mkDerivation {
        pname = "liftlog-mcp";
        version = "0.1.0";
        src = ./src;
        dontBuild = true;
        installPhase = ''
          mkdir -p $out/bin
          for name in receiver mcp; do
            { echo "#!${pkgs.python3}/bin/python3"; cat liftlog_$name.py; } > $out/bin/liftlog-$name
            chmod +x $out/bin/liftlog-$name
          done
        '';
        meta.mainProgram = "liftlog-mcp";
      };

      nixosModules.default = import ./nixos/module.nix self;
    };
}
