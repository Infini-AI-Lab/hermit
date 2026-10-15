"""harnessadapter check | train | eval | deploy | convert | serve"""
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
    dep.add_argument("--workspace", "--output", dest="workspace", type=Path, required=True,
                     help="the project directory the agent will work in (--output is the older spelling)")
    dep.add_argument("--host", choices=["codex", "claude-code"], default="codex")
    dep.add_argument("--python", default=sys.executable)
    con = sub.add_parser("convert", help="convert an adapter into a plugin in a host's native format")
    con.add_argument("--adapter", type=Path, required=True)
    con.add_argument("--output", type=Path, required=True)
    con.add_argument("--target", choices=["claude-code", "codex", "pi", "hermes"], required=True)
    con.add_argument("--allow-lossy", action="store_true",
                     help="leave out the parts the target cannot carry instead of failing; listed under `omitted`")
    con.add_argument("--python", default=sys.executable)
    # The line `convert` writes into a host's configuration; a person does not type it. This is the stdio half of the
    # `serve` design in docs/framework/adapter-manifest.md: one server per host session, started and stopped by the
    # host. The standalone service and its relay are not implemented yet.
    srv = sub.add_parser("serve", help="run the adapter's MCP tool server on stdio, for a host session")
    srv.add_argument("adapter", type=Path)
    srv.add_argument("--audit-log", type=Path, help="append every tool call to this JSONL file")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from harnessadapter import __version__
        from harnessadapter.plugins.common import RECORD
        from harnessadapter.runtime.mcp_server import main as run_server
        # A plugin's embedded adapter has `convert`'s record next to it; the harnessadapter on PATH here may be
        # another version than the one that made the plugin. Say so on stderr (the host's log), then serve anyway.
        try:
            made_by = json.loads((args.adapter.resolve().parent / RECORD).read_text()).get("framework")
        except (OSError, ValueError):
            made_by = None
        if made_by and made_by != __version__:
            print(f"harnessadapter serve: this plugin was made by harnessadapter {made_by}; this is {__version__}",
                  file=sys.stderr)
        run_server(["--adapter", str(args.adapter)] + (["--audit-log", str(args.audit_log)] if args.audit_log else []))
        return 0

    if args.command == "convert":
        from harnessadapter.plugins import Unsupported, build
        try:
            result = build(args.target, args.adapter, args.output, str(Path(args.python).absolute()),
                           allow_lossy=args.allow_lossy)
        except Unsupported as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "deploy":
        if args.host == "codex":
            from harnessadapter.hosts.codex import prepare
        else:
            from harnessadapter.hosts.claude_code import prepare
        print(json.dumps(prepare(args.adapter, args.workspace, str(Path(args.python).absolute())), indent=2))
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
