{
  description = "Pin nixpkgs packages Hydra failed to build to their last build on cache.nixos.org";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      forAllSystems =
        f:
        lib.genAttrs [
          "aarch64-darwin"
          "aarch64-linux"
          "x86_64-linux"
        ] (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # pinsFile -> overlay (overlay.nix).
      lib.overlay = import ./overlay.nix;

      packages = forAllSystems (pkgs: {
        default = pkgs.writers.writePython3Bin "hydra-pins" {
          # flake8 runs at build time; long lines and either line-break style
          # are fine.
          flakeIgnore = [
            "E501"
            "W503"
            "W504"
          ];
        } (builtins.readFile ./hydra-pins.py);
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          meta.description = "Pin packages Hydra failed to build to their last cached build";
        };
      });

      checks = forAllSystems (pkgs: {
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) default;
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
