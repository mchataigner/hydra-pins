# hydra-pins

Auto pin to the most recent hydra cached pre-build version.

A nixpkgs channel advances once its gating jobs pass, not every package. A
package whose Hydra build failed, or whose dependency's did, isn't on
cache.nixos.org, so a switch builds it locally, together with everything under
it that is uncached too (a `pre-commit` can mean building dotnet and the Swift
toolchain from source).

hydra-pins keeps a `pins.json` that lists such packages, per system, with the
nixpkgs revision of their newest Hydra build the cache still holds, and an
overlay that takes each of them from there.

## Usage

Add the flake and apply the overlay to your hosts' nixpkgs:

```nix
{
  inputs.hydra-pins = {
    url = "github:mchataigner/hydra-pins";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  # in a NixOS or nix-darwin module:
  nixpkgs.overlays = [ (inputs.hydra-pins.lib.overlay ./hydra-pins/pins.json) ];
}
```

Start with an empty pins file, `{}`, tracked by git (flakes only see tracked
files), then update it from the flake's directory:

```sh
nix run github:mchataigner/hydra-pins
```

It needs `nix`, `nix-prefetch-url` and `git` on `PATH`. Options:

- `--flake DIR`: the flake to scan (default `.`). It must be in a git
  repository; a flake in a subdirectory works.
- `--pins FILE`: the pins file, relative to the flake (default
  `hydra-pins/pins.json`).
- `--nixos-dir DIR`: scan only the `nixosConfigurations` with a directory
  there, to leave out configurations that don't apply the overlay. By default,
  every `darwinConfigurations` and `nixosConfigurations` entry is scanned.
- `--dev-shells SYSTEM...`: also scan every `devShells.<system>.<name>` of
  these systems, against the flake's `nixpkgs` input, for a flake with no host
  configuration.
- `--summary FILE`: write a markdown summary of what changed (also printed).
- `--message FILE`: write a commit message (`hydra-pins: pin x; unpin y`
  followed by the summary), only when something changed.

## How it works

For every host it walks the toplevel's derivation graph from the top, asking
cache.nixos.org for each output and descending only into what it doesn't hold.
An uncached derivation that is a plain nixpkgs package (the same derivation as
the host's nixpkgs builds for that attribute without overlays) is looked up on
Hydra's `nixpkgs/unstable` jobset, and its newest successful build whose output
the cache still holds is pinned by its nixpkgs revision. Before pinning, it
checks that the pinned nixpkgs evaluates the package to exactly Hydra's output.

- Only the outermost such package is pinned (`pre-commit`, not the dotnet it
  checks with), and a pin is kept only if, with it, nothing is missing from the
  cache that wasn't before: pinning a library would rebuild everything that
  depends on it.
- A pin is dropped once the current nixpkgs' build is cached again, and moved
  forward when Hydra has a newer cached build.
- Hydra's builds older than 30 days aren't pinned: Hydra has stopped building
  the package, and a pin would be a silent downgrade by months.
- When Hydra hasn't built the current derivation yet, nothing is pinned: the
  build may still succeed.
- Uncached derivations it can't pin (your own overlays and packages, unfree
  packages, packages with no cached build) are listed in the summary.

## Automation

Run it in CI after every `flake.lock` update and on a schedule, and open a pull
request with the result, e.g. with GitHub Actions:

```yaml
on:
  push:
    branches: [main]
    paths: [flake.lock]
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: cachix/install-nix-action@v31
      - run: nix run github:mchataigner/hydra-pins -- --summary "$RUNNER_TEMP/summary.md" --message "$RUNNER_TEMP/message"
      # then, if hydra-pins/pins.json changed: commit it with
      # `git commit -F "$RUNNER_TEMP/message"`, push a branch and open a pull
      # request with the summary as its body.
```

The darwin hosts are evaluated on any system, so one Linux runner can maintain
the pins of a flake with both Mac and NixOS hosts.
