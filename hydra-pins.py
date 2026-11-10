"""Pin nixpkgs packages Hydra failed to build to their last cached build.

On unstable, a package whose Hydra build failed (or whose dependency's did)
isn't on cache.nixos.org, so a switch builds it, and everything under it that
is uncached too, locally: dotnet and the Swift toolchain, for hours.

For every host this walks the toplevel's derivation graph from the top, asking
the cache for each output and descending only into what it doesn't hold. An
uncached derivation that is a plain nixpkgs package (the same derivation as
the host's nixpkgs without overlays builds for its name) is looked up on
Hydra, and the newest successful build the cache still holds is pinned, by its
nixpkgs revision, in pins.json, which overlay.nix applies. Only the outermost
such package is pinned (pre-commit, not the dotnet it checks with), and a pin
is kept only if, with it, nothing is missing from the cache that wasn't
before: pinning a library would rebuild everything that depends on it.

A pin is dropped once the current nixpkgs' build of it is cached, and moved
forward when Hydra has a newer cached build. Packages that are uncached but
can't be pinned are listed in the summary.

The hosts are every darwinConfiguration and every nixosConfiguration, or with
--nixos-dir only the nixosConfigurations with a directory there (to leave out
configurations that don't apply the pins). --dev-shells adds every
devShells.<system>.<name> of the systems it names, against the flake's
nixpkgs input, for a flake with no host configuration.

It needs nix, nix-prefetch-url and git on PATH, and network access to
cache.nixos.org, hydra.nixos.org and github.com.

Usage: hydra-pins [--flake DIR] [--pins FILE] [--nixos-dir DIR]
                  [--dev-shells SYSTEM...] [--summary FILE] [--message FILE]
"""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CACHE = "https://cache.nixos.org"
HYDRA = "https://hydra.nixos.org"
AGENT = "hydra-pins (+https://github.com/mchataigner/hydra-pins)"
# The Hydra jobset that builds nixpkgs for both systems. (nixpkgs/trunk and
# nixos/trunk-combined answer the latestbuilds API with nothing.) The NixOS
# hosts' nixos-unstable is a nixpkgs-unstable commit or a close one, and a pin
# is checked against the cache anyway.
JOBSET = "/api/latestbuilds?nr=30&project=nixpkgs&jobset=unstable&job="
# Successful Hydra builds to try per package, newest first, before giving up.
TRIES = 5
# A build older than this isn't pinned: Hydra has stopped building the package
# (dropped the platform, or the package went unfree), and a pin would be a
# silent downgrade by months.
MAX_AGE_DAYS = 30
ROUNDS = 3


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def run(*cmd):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, text=True).stdout


def base(path):
    return os.path.basename(path)


def pname(name):
    """builtins.parseDrvName's name: up to the first dash not before a letter."""
    for i, c in enumerate(name):
        if c == "-" and i + 1 < len(name) and not name[i + 1].isalpha():
            return name[:i]
    return name


def version_of(name):
    return name[len(pname(name)) + 1:]


def fetch(url, method="GET", accept=None):
    headers = {"User-Agent": AGENT}
    if accept:
        headers["Accept"] = accept
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError) as e:
            err = e
        time.sleep(2 ** attempt)
    raise err


def hydra(path):
    return json.loads(fetch(HYDRA + path, accept="application/json"))


_cached = {}


def cached(path):
    """Whether cache.nixos.org holds this store path."""
    h = base(path).split("-", 1)[0]
    if h not in _cached:
        try:
            fetch(f"{CACHE}/{h}.narinfo", method="HEAD")
            _cached[h] = True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            _cached[h] = False
    return _cached[h]


class Flake:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        # A flake in a repository's subdirectory (infra's nixos/) is ?dir=.
        top = run("git", "-C", self.root, "rev-parse", "--show-toplevel").strip()
        sub = os.path.relpath(self.root, top)
        self.ref = "git+file://" + top + ("" if sub == "." else "?dir=" + sub)
        self.graph = {}

    def nix(self, body, **args):
        """Evaluate a Nix expression with `flake` and `args` in scope."""
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump(args, f)
            f.flush()
            expr = (
                f'let flake = builtins.getFlake "{self.ref}";'
                f' args = builtins.fromJSON (builtins.readFile "{f.name}");'
                " shell = args.kind or null == \"devShell\";"
                " host = (if args.kind or null == \"darwin\" then flake.darwinConfigurations"
                " else flake.nixosConfigurations).${args.host or \"\"};"
                " top = if shell then flake.devShells.${args.system}.${args.host}"
                " else host.config.system.build.toplevel;"
                # nixpkgs as Hydra builds it: the host's, minus its overlays; a
                # dev shell's is the flake's nixpkgs input.
                " vanilla = if shell"
                " then import flake.inputs.nixpkgs { inherit (args) system; config = { }; overlays = [ ]; }"
                " else import host.pkgs.path { inherit (host.pkgs.stdenv.hostPlatform) system;"
                " config = host.config.nixpkgs.config; overlays = [ ]; };"
                " try = e: let r = builtins.tryEval e; in if r.success then r.value else null;"
                f" in {body}"
            )
            return json.loads(run("nix", "eval", "--impure", "--json", "--expr", expr))

    def show(self, drvs):
        """Fill self.graph with these derivations' name, output and inputs."""
        todo = [d for d in drvs if d not in self.graph]
        for i in range(0, len(todo), 500):
            data = json.loads(run("nix", "derivation", "show", *("/nix/store/" + d for d in todo[i:i + 500])))
            # Nix 2.33+ nests them under "derivations" and drops the store dir.
            for path, d in data.get("derivations", data).items():
                inputs = d["inputs"]["drvs"] if "inputs" in d else d["inputDrvs"]
                outs = d["outputs"]
                out = outs.get("out") or next(iter(outs.values()))
                self.graph[base(path)] = {
                    "name": d["name"],
                    "inputs": [base(p) for p in inputs],
                    "out": base(out.get("path", "")),
                    "fixed": any(k in out for k in ("hash", "hashAlgo", "method")),
                }

    def missing(self, root):
        """The derivations under root that cache.nixos.org has no output for."""
        seen, todo, result = {root}, [root], set()
        with concurrent.futures.ThreadPoolExecutor(32) as pool:
            while todo:
                self.show(todo)
                have = list(pool.map(lambda d: bool(self.graph[d]["out"]) and cached(self.graph[d]["out"]), todo))
                frontier = []
                for d, ok in zip(todo, have):
                    if ok:
                        continue
                    result.add(d)
                    for i in self.graph[d]["inputs"]:
                        if i not in seen:
                            seen.add(i)
                            frontier.append(i)
                todo = frontier
        return result


class Host:
    """A darwin or NixOS configuration, or a dev shell (devShells.<system>.<name>)."""

    def __init__(self, flake, kind, name, system=None):
        self.flake, self.kind, self.name = flake, kind, name
        self.label = f"devShells.{system}.{name}" if kind == "devShell" else name
        self.system = system or flake.nix("host.pkgs.stdenv.hostPlatform.system", kind=kind, host=name)

    def nix(self, body, **args):
        return self.flake.nix(body, kind=self.kind, host=self.name, system=self.system, **args)

    def scan(self):
        """Uncached derivations, and which of them are plain nixpkgs packages."""
        log(f"scanning {self.label}")
        drv = base(self.nix("top.drvPath"))
        miss = self.flake.missing(drv)
        g = self.flake.graph
        cands = [{"drv": d, "pname": pname(g[d]["name"])} for d in sorted(miss) if not g[d]["fixed"]]
        # Hydra builds what's free and lists the system in hydraPlatforms (which
        # defaults to platforms); the rest is never cached, so not pinnable.
        matched = self.nix(
            "let lib = vanilla.lib; system = vanilla.stdenv.hostPlatform.system;"
            " onHydra = p: lib.all (l: l.free or true) (lib.toList (p.meta.license or [ ]))"
            " && builtins.elem system (p.meta.hydraPlatforms or p.meta.platforms or [ system ]);"
            " in map (c: c // { attr = if builtins.hasAttr c.pname vanilla"
            " && try ((vanilla.${c.pname}.drvPath or null) == \"/nix/store/${c.drv}\") == true"
            " && try (onHydra vanilla.${c.pname}) == true"
            " then c.pname else null; }) args.cands",
            cands=cands,
        )
        attrs = {c["drv"]: c["attr"] for c in matched if c["attr"]}
        return miss, attrs

    def vanilla_out(self, attr):
        return self.nix("try (vanilla.${args.attr}.outPath or null)", attr=attr)


def roots(graph, miss, attrs):
    """The nixpkgs packages in miss that no other one in miss depends on."""
    inner = set()
    for d in attrs:
        stack = [i for i in graph[d]["inputs"] if i in miss]
        while stack:
            i = stack.pop()
            if i not in inner:
                inner.add(i)
                stack.extend(j for j in graph[i]["inputs"] if j in miss)
    return [d for d in attrs if d not in inner]


_revs, _hashes = {}, {}


def last_cached(host, attr, drv=None):
    """The newest successful Hydra build of attr that the cache still holds.

    "pending" if Hydra hasn't built drv (the current one) yet: it may well
    succeed, so it isn't worth pinning yet.
    """
    builds = sorted(hydra(f"{JOBSET}{attr}.{host.system}"), key=lambda b: -b["id"])
    if drv and any(not b["finished"] and base(hydra(f"/build/{b['id']}")["drvpath"]) == drv for b in builds):
        return "pending"
    oldest = time.time() - MAX_AGE_DAYS * 86400
    good = [b for b in builds if b["finished"] and b["buildstatus"] == 0]
    if good and all(b["timestamp"] < oldest for b in good):
        return "stale " + time.strftime("%Y-%m-%d", time.gmtime(good[0]["timestamp"]))
    for b in [b for b in good if b["timestamp"] >= oldest][:TRIES]:
        detail = hydra(f"/build/{b['id']}")
        outs = detail["buildoutputs"]
        out = (outs.get("out") or next(iter(outs.values())))["path"]
        if not cached(out):
            continue
        evals = detail["jobsetevals"]
        if not evals:
            continue
        if evals[0] not in _revs:
            _revs[evals[0]] = hydra(f"/eval/{evals[0]}")["jobsetevalinputs"]["nixpkgs"]["revision"]
        rev = _revs[evals[0]]
        url = f"https://github.com/NixOS/nixpkgs/archive/{rev}.tar.gz"
        if rev not in _hashes:
            _hashes[rev] = run("nix-prefetch-url", "--unpack", url).strip()
        # The pin evaluates it the way overlay.nix will; it must be Hydra's.
        got = host.flake.nix(
            "(import (builtins.fetchTarball { inherit (args) url sha256; })"
            " { inherit (args) system; config = { }; overlays = [ ]; }).${args.attr}.outPath",
            url=url, sha256=_hashes[rev], system=host.system, attr=attr,
        )
        if got == out:
            return {"rev": rev, "sha256": _hashes[rev], "version": version_of(b["nixname"]), "hydraBuild": b["id"]}
    return None


class Updater:
    def __init__(self, flake, pins):
        self.flake = flake
        self.path = os.path.join(flake.root, pins)
        with open(self.path) as f:
            self.pins = json.load(f)
        self.report = {k: [] for k in ("pinned", "moved", "unpinned", "pending", "unpinnable", "rejected", "other")}

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.pins, f, indent=2, sort_keys=True)
            f.write("\n")

    def scan(self, hosts):
        result = {}
        for h in hosts:
            result[h.name] = h.scan()
        return result

    def names(self, scans):
        g = self.flake.graph
        return {(h, g[d]["name"]) for h, (miss, _) in scans.items() for d in miss if not g[d]["fixed"]}

    def system(self, system, hosts):
        pins = self.pins.setdefault(system, {})
        link = "https://hydra.nixos.org/build/"

        # Existing pins: drop the ones nixpkgs' current build of is cached
        # (or that nixpkgs no longer has), move the rest to Hydra's newest.
        for attr in sorted(pins):
            out = hosts[0].vanilla_out(attr)
            if out is None or cached(out):
                why = "gone from nixpkgs" if out is None else f"{version_of(base(out)[33:])} is cached again"
                self.report["unpinned"].append(f"{system} `{attr}`: {why}")
                del pins[attr]
                continue
            hit = last_cached(hosts[0], attr)
            if isinstance(hit, dict) and hit["hydraBuild"] > pins[attr]["hydraBuild"]:
                self.report["moved"].append(
                    f"{system} `{attr}`: {pins[attr]['version']} -> {hit['version']} ([{hit['hydraBuild']}]({link}{hit['hydraBuild']}))")
                pins[attr] = hit
        self.save()

        tried = set(pins)
        scans = self.scan(hosts)
        for _ in range(ROUNDS):
            g = self.flake.graph
            new = {}
            for h in hosts:
                miss, attrs = scans[h.name]
                for d in roots(g, miss, attrs):
                    attr = attrs[d]
                    if attr in tried:
                        continue
                    tried.add(attr)
                    log(f"  {system} {attr}: uncached, looking on Hydra")
                    hit = last_cached(h, attr, d)
                    if hit == "pending":
                        self.report["pending"].append(f"{system} `{attr}` {version_of(g[d]['name'])}")
                    elif isinstance(hit, str):
                        self.report["unpinnable"].append(
                            f"{system} `{attr}` {version_of(g[d]['name'])}: Hydra's last build is from {hit.split()[1]}")
                    elif hit is None:
                        self.report["unpinnable"].append(f"{system} `{attr}` {version_of(g[d]['name'])}: no cached Hydra build")
                    else:
                        new[attr] = (hit, version_of(g[d]["name"]))
            if not new:
                break
            # Keep a pin only if it leaves nothing uncached that wasn't
            # before; all together first, then one at a time.
            before = self.names(scans)
            groups = [list(new)] + ([[a] for a in new] if len(new) > 1 else [])
            accepted = False
            for group in groups:
                if all(a in pins for a in group):
                    continue
                for a in group:
                    pins[a] = new[a][0]
                self.save()
                after_scans = self.scan(hosts)
                after = self.names(after_scans)
                if after < before:
                    for a in group:
                        hit, current = new[a]
                        self.report["pinned"].append(
                            f"{system} `{a}`: {current} -> {hit['version']} ([{hit['hydraBuild']}]({link}{hit['hydraBuild']}))")
                    scans, before, accepted = after_scans, after, True
                    if len(group) > 1:
                        break
                else:
                    for a in group:
                        del pins[a]
                    self.save()
                    if len(group) == 1:
                        extra = sorted(n for h, n in after - before)[:5]
                        self.report["rejected"].append(f"{system} `{group[0]}`: would also rebuild {', '.join(extra)}")
            if not accepted:
                break

        g = self.flake.graph
        other = sorted({g[d]["name"] for miss, attrs in scans.values() for d in miss
                        if not g[d]["fixed"] and d not in attrs})
        if other:
            more = f" and {len(other) - 150} more" if len(other) > 150 else ""
            self.report["other"].append(f"{system}: " + ", ".join(other[:150]) + more)

    def summary(self):
        titles = {
            "pinned": "Pinned (current build not cached)",
            "moved": "Moved to a newer cached build",
            "unpinned": "Unpinned",
            "pending": "Uncached, but Hydra hasn't built it yet",
            "unpinnable": "Uncached, with no cached build to pin",
            "rejected": "Uncached, but a pin would rebuild other packages",
        }
        lines = []
        for key, title in titles.items():
            if self.report[key]:
                lines += [f"### {title}", ""] + [f"- {x}" for x in self.report[key]] + [""]
        if self.report["other"]:
            lines += ["<details><summary>Other uncached derivations (ours, overridden, unfree)</summary>", ""]
            lines += [f"- {x}" for x in self.report["other"]] + ["", "</details>", ""]
        return "\n".join(lines) or "Nothing to pin or unpin.\n"

    def message(self):
        def attrs(key):
            return sorted({x.split("`")[1] for x in self.report[key]})
        parts = []
        for verb, key in (("pin", "pinned"), ("move", "moved"), ("unpin", "unpinned")):
            if attrs(key):
                parts.append(f"{verb} {', '.join(attrs(key))}")
        return "hydra-pins: " + "; ".join(parts) if parts else ""


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--flake", default=".", help="the flake's directory, in a git repository")
    p.add_argument("--pins", default="hydra-pins/pins.json", help="the pins file, relative to the flake")
    p.add_argument("--nixos-dir",
                   help="only the NixOS hosts with a directory here, relative to the flake (default: all of them)")
    p.add_argument("--dev-shells", nargs="+", default=[], metavar="SYSTEM",
                   help="also every dev shell of these systems, whose nixpkgs is the flake's nixpkgs input")
    p.add_argument("--summary", help="write a markdown summary here")
    p.add_argument("--message", help="write a commit message here")
    a = p.parse_args()

    flake = Flake(a.flake)
    names = flake.nix(
        "{ darwin = builtins.attrNames (flake.darwinConfigurations or { });"
        " nixos = builtins.attrNames (flake.nixosConfigurations or { }); }"
    )
    if a.nixos_dir:
        nixos_dir = os.path.join(flake.root, a.nixos_dir)
        names["nixos"] = [n for n in names["nixos"] if os.path.isdir(os.path.join(nixos_dir, n))]
    by_system = {}
    for kind in ("darwin", "nixos"):
        for name in names[kind]:
            h = Host(flake, kind, name)
            by_system.setdefault(h.system, []).append(h)
    for system in a.dev_shells:
        for name in flake.nix("builtins.attrNames (flake.devShells.${args.system} or { })", system=system):
            by_system.setdefault(system, []).append(Host(flake, "devShell", name, system))

    up = Updater(flake, a.pins)
    for system in sorted(by_system):
        up.system(system, by_system[system])
    up.save()

    summary, message = up.summary(), up.message()
    print(summary)
    if a.summary:
        with open(a.summary, "w") as f:
            f.write(summary)
    if a.message and message:
        with open(a.message, "w") as f:
            f.write(message + "\n\n" + summary)


if __name__ == "__main__":
    main()
