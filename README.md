# nixcu
[![Made with Textual](https://img.shields.io/badge/Made%20with-Textual-blue)](https://github.com/Textualize/textual)

A simple TUI for finding what's actually taking up space in a Nix store closure.

`nix path-info --closure-size` will tell you how big a path's closure is, but closures overlap constantly.

Enter: nixcu, It builds the [dominator tree](https://en.wikipedia.org/wiki/Dominator_(graph_theory)) of the closure's reference graph and attributes to each path only the space it *exclusively* owns, i.e. everything reachable _only_ by going through it. Shared dependencies stay pinned near the root where they belong, instead of inflating every consumer that happens to reference them.

## Usage

```bash
nix run github:TotalyEnglizLitrate/nixcu
```

Or add it to your flake as an input
```nix
inputs.nixcu.url = "github:TotalyEnglizLitrate/nixcu";
```

```bash
nixcu [closure] [--inline[=ROWS]]
```

- `closure` : a closure to analyse, passed as-is to nix and therefore supports everything that nix supports. Defaults to `/run/current-system`.
- `--inline[=ROWS]` : render below the prompt instead of taking over the screen, optionally capping the height. Defaults to 15 rows.

```bash
$ nixcu
$ nixcu --inline
$ nixcu /nix/store/xxxxx-my-package
$ nixcu github:TotalyEnglizLitrate/nixcu
$ nixcu --inline=10 github:TotalyEnglizLitrate/nixcu
```

Inside the TUI:

| Key | Action                       |
| --- | ---------------------------- |
| `q` | Quit                         |
| `c` | Copy the selected store path |

Each row shows
- The exclusive size in bold, i.e. what dropping that node would actually free.
- The path's name
- And, when it differs, the node's own `nar_size` in parens.

The TUI tree is populated lazily as you expand nodes, so opening a large closure stays fast, only the levels you actually inspect get built.

## Requirements

- Python ≥ 3.12 (the flake uses `python3` from nixpkgs)
- `nix` (≥ 2.33 for `--json-format 2` though older versions work) or nix provided by `lix` reachable on `PATH`, with read access to the local Nix store/DB.

## Alternatives

- [nix-tree](https://github.com/utdemir/nix-tree) - A more featureful derivation dependency graph explorer that also has unique closure size determination
- [nix-du](https://github.com/symphorien/nix-du) - Visualizes your whole store across all GC roots at once (though it allows for specifying a single GC root), grouping paths by which combination of roots keeps them alive, and rendering the result into a `.dot` file.
