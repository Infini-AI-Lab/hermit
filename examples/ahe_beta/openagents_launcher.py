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
    config_arg = next(
        (Path(remainder[index + 1]) for index, value in enumerate(remainder[:-1]) if value == "--config"),
        None,
    )
    if config_arg is not None:
        config_path = config_arg.expanduser().resolve()
        if config_path.name == "openagents.yml" and config_path.parent.name == ".openagents":
            os.chdir(config_path.parent.parent)
    os.environ.setdefault("OPENAGENTS_NO_SELF_UPDATE", "1")
    os.environ.setdefault("OPENAGENTS_NO_UPDATE", "1")
    os.execv(argv[0], argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
