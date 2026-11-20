"""Run OpenAgents from the commit recorded in an AHE experiment manifest."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from openagents_runtime import command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args, remainder = parser.parse_known_args()
    argv = command(args.manifest, remainder)
    os.environ.setdefault("OPENAGENTS_NO_SELF_UPDATE", "1")
    os.environ.setdefault("OPENAGENTS_NO_UPDATE", "1")
    os.execv(argv[0], argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
