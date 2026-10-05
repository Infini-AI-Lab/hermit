"""harnessadapter check | train | eval | deploy"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="harnessadapter", description="Train harness adapters.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("check", "check the settings before training"), ("train", "run the Trainer"),
                       ("eval", "evaluate one adapter on the test split")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--config", type=Path, required=True)
        p.add_argument("overrides", nargs="*", help="key.sub=value")
        if name == "eval":
            p.add_argument("--adapter", required=True, help="final | root | none | a tag | a commit | a local path")
        if name == "check":
            p.add_argument("--no-probe", action="store_true", help="skip running one task through the pipeline")
    dep = sub.add_parser("deploy", help="deploy an adapter's exports into a Codex or Claude Code workspace")
    dep.add_argument("--adapter", type=Path, required=True)
    dep.add_argument("--output", type=Path, required=True)
    dep.add_argument("--host", choices=["codex", "claude-code"], default="codex")
    dep.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if args.command == "deploy":
        if args.host == "codex":
            from harnessadapter.hosts.codex import prepare
        else:
            from harnessadapter.hosts.claude_code import prepare
        print(json.dumps(prepare(args.adapter, args.output, str(Path(args.python).absolute())), indent=2))
        return 0
    from harnessadapter.config import Config
    config = Config.load(args.config, args.overrides)
    if args.command == "check":
        from harnessadapter.settings_check import run_check
        return 0 if run_check(config, probe=not args.no_probe) else 1
    if args.command == "train":
        from harnessadapter.trainer.trainer import Trainer
        summary = Trainer(config).train()
        print(json.dumps(summary, indent=1))
        return 0
    from harnessadapter.evaluate import evaluate
    print(json.dumps(evaluate(config, args.adapter), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
