"""Command line entry point."""

import argparse
import os
import subprocess
import sys

from nixcu.closure import Closure, SchemaError
from nixcu.dominators import DominatorTree
from nixcu.tui import NixcuApp

DEFAULT_ROOT = "/run/current-system"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nixcu",
        description="Explore what a Nix closure is actually made of.",
    )
    _ = parser.add_argument(
        "closure",
        nargs="?",
        help=f"store path to analyse (default: {DEFAULT_ROOT})",
    )
    _ = parser.add_argument(
        "--inline",
        action="store_true",
        help="render below the prompt instead of taking over the screen",
    )
    return parser


def resolve_root(parser: argparse.ArgumentParser, closure: str | None) -> str:
    """Pick the closure to analyse, erroring out the way argparse does."""
    if closure is None:
        if not os.path.exists(DEFAULT_ROOT):
            parser.error(
                f"no closure given and {DEFAULT_ROOT} does not exist; pass a store path explicitly"
            )
        return DEFAULT_ROOT
    if not os.path.exists(closure):
        parser.error(f"no such path: {closure}")
    return closure


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = resolve_root(parser, args.closure)

    try:
        closure = Closure.from_store(root)
    except FileNotFoundError:
        print("nixcu: nix not found on PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        _ = sys.stderr.write(e.stderr.decode(errors="replace"))
        return e.returncode or 1
    except SchemaError as e:
        print(f"nixcu: {e}", file=sys.stderr)
        return 1

    app = NixcuApp(DominatorTree.build(closure), root, inline=args.inline)
    app.run(inline=args.inline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
