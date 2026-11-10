# pinsFile -> an overlay that takes the packages the pins file lists from an
# older nixpkgs, because Hydra failed to build the current one, so it isn't on
# cache.nixos.org and a switch would build it (and whatever under it is
# uncached too) locally. The pins file is written by hydra-pins.py, which also
# drops a pin once the current package is cached again (README). Only the
# pinned attribute comes from the old nixpkgs, built as Hydra built it (no
# config, no overlays).
#
#   nixpkgs.overlays = [ (hydra-pins.lib.overlay ./hydra-pins/pins.json) ];
pinsFile: _final: prev:
let
  inherit (prev) lib;
  inherit (prev.stdenv.hostPlatform) system;
  pins = (lib.importJSON pinsFile).${system} or { };
  nixpkgsAt = lib.mapAttrs' (
    _: pin:
    lib.nameValuePair pin.rev (
      import
        (builtins.fetchTarball {
          url = "https://github.com/NixOS/nixpkgs/archive/${pin.rev}.tar.gz";
          inherit (pin) sha256;
        })
        {
          inherit system;
          config = { };
          overlays = [ ];
        }
    )
  ) pins;
in
lib.mapAttrs (name: pin: nixpkgsAt.${pin.rev}.${name}) pins
