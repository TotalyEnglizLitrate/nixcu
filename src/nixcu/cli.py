"""Command line entry point."""

import argparse
import os
import subprocess
import sys
import time

from nixcu.closure import LOAD_PHASES, Closure
from nixcu.dominators import BUILD_PHASES, DominatorTree
from nixcu.progress import loading
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
        nargs="?",
        const=15,
        type=int,
        metavar="ROWS",
        help="render below the prompt instead of taking over the screen, optionally specifying number of rows",
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
    return closure


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = resolve_root(parser, args.closure)

    started = time.perf_counter()
    try:
        with loading(LOAD_PHASES + BUILD_PHASES) as report:
            closure = Closure.from_store(root, report=report)
            dom = DominatorTree.build(closure, report=report)
    except FileNotFoundError:
        print("nixcu: nix not found on PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        _ = sys.stderr.write(e.stderr.decode(errors="replace"))
        return e.returncode or 1
    except ValueError as e:
        print(f"nixcu: {e}", file=sys.stderr)
        return 1

    app = NixcuApp(
        dom, root, inline=args.inline, elapsed=time.perf_counter() - started
    )
    app.run(inline=args.inline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
