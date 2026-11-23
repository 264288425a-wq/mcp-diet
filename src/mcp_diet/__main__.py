"""Module entry point for `python -m mcp_diet`."""

from __future__ import annotations

import sys

from mcp_diet.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
