"""Run OpenAgents by hand from the commit recorded in an AHE experiment manifest: to look at a worker while setting one
up. Not `vigil`, the worker itself, which takes tickets and runs the developers' commands: a run's delivery Evolver
starts it, in the run's sandbox (delivery/evolver.py), never this, which runs with this machine's home and network."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from openagents_runtime import command, own_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args, remainder = parser.parse_known_args()
    if "vigil" in remainder:
        parser.error("`vigil` runs only in a run's sandbox, which the delivery Evolver starts (delivery/evolver.py)")
    for index, value in enumerate(remainder[:-1]):       # absolute before the change of folder below
        if value == "--config":
            remainder[index + 1] = str(Path(remainder[index + 1]).expanduser().resolve())
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
    if "--project" in argv:                              # the checkout's own environment, as it was synced
        os.environ["UV_PROJECT_ENVIRONMENT"] = own_environment(Path(argv[argv.index("--project") + 1]))[
            "UV_PROJECT_ENVIRONMENT"]
    os.execv(argv[0], argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
