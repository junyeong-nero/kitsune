"""Command line shared by `kitsune` and `kitsune-browser-mcp`."""

import argparse
import sys


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .server import main as serve

        return serve()

    parser = argparse.ArgumentParser(description="Without arguments, run the Kitsune MCP server over stdio.")
    setup = parser.add_subparsers(required=True).add_parser("setup", help="Register Kitsune with your MCP clients")
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="Show clients and config paths without changing them")
    mode.add_argument("--auto", action="store_true", help="Register with detected clients without prompting")
    args = parser.parse_args(argv)

    from .setup import run

    return run(list_only=args.list, auto=args.auto)
