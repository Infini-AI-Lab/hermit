"""harnessadapter optimize: run an optimizer Evolver (gepa, skillopt, or a
definition directory with kind = "optimizer") against an Environment.

    harnessadapter optimize prompt-only --evolver gepa \\
      --environment tasks/OfficeQA/host/optimize_env.py:make_environment \\
      --environment-config officeqa-env.yaml \\
      --optimizer-model accounts/fireworks/models/glm-5p3 \\
      --optimizer-base-url https://api.fireworks.ai/inference/v1 \\
      --optimizer-key-env FIREWORKS_API_KEY --set max_metric_calls=150 --output runs/

    # $0 end-to-end check of either optimizer:
    harnessadapter optimize prompt-only --evolver skillopt --fake-optimizer \\
      --environment harnessadapter.optimize.testing:KeywordEnvironment \\
      --set num_epochs=2 --set batch_size=4 --set minibatch_size=2 --set merge_batch_size=2
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import yaml

from harnessadapter.paths import resolve_architecture, workspace_root


def load_callable(spec: str):
    """module:callable or path/to/file.py:callable."""
    target, sep, attribute = spec.rpartition(":")
    if not sep or not attribute.isidentifier():
        raise ValueError("Use module:callable or file.py:callable")
    if target.endswith(".py"):
        path = Path(target).resolve()
        name = "harnessadapter_environment_" + path.stem
        module_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, attribute)


def _value(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _mapping(path: Path | None) -> dict:
    if path is None:
        return {}
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(prog="harnessadapter optimize", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="Adapter or architecture to start from (prompt-only, mwm, or a path)")
    parser.add_argument("--evolver", required=True, help="gepa, skillopt, or an optimizer Evolver directory")
    parser.add_argument("--environment", required=True, help="module:factory or file.py:factory returning an Environment")
    parser.add_argument("--environment-config", type=Path, help="YAML/JSON mapping passed to the factory as keyword arguments")
    parser.add_argument("--settings", type=Path, help="YAML/JSON mapping of optimizer settings")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="One optimizer setting (repeatable)")
    parser.add_argument("--component", action="append", default=None, help="Adapter text file to optimize (repeatable; default: the context file)")
    parser.add_argument("--training-state", type=Path, help="Resume from a previous session's training_state/")
    parser.add_argument("--output", type=Path, help="Directory for sessions (default: .demo/optimize)")
    parser.add_argument("--optimizer-model", help="Optimizer/reflection model id")
    parser.add_argument("--optimizer-base-url", default="https://api.fireworks.ai/inference/v1")
    parser.add_argument("--optimizer-key-env", default="FIREWORKS_API_KEY", help="Environment variable holding the optimizer key")
    parser.add_argument("--optimizer-max-tokens", type=int, default=32768)
    parser.add_argument("--optimizer-temperature", type=float, default=None)
    parser.add_argument("--fake-optimizer", action="store_true", help="$0: a local scripted model server instead of a provider")
    args = parser.parse_args(argv)

    from harnessadapter.optimize.llm import ChatLM
    from harnessadapter.optimize.session import optimize

    settings = _mapping(args.settings)
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error("--set takes KEY=VALUE")
        settings[key.strip()] = _value(value)
    environment = load_callable(args.environment)(**_mapping(args.environment_config))
    output = (args.output or workspace_root() / "optimize").resolve()
    source = resolve_architecture(args.source)

    def run(lm):
        return optimize(source, environment, args.evolver, lm=lm, output=output, settings=settings,
                        components=args.component, training_state=args.training_state)

    if args.fake_optimizer:
        from harnessadapter.optimize.testing import FakeChatServer
        os.environ.setdefault("HARNESSADAPTER_FAKE_KEY", "fake")
        with FakeChatServer() as server:
            lm = ChatLM("scripted", base_url=server.base_url, api_key_env="HARNESSADAPTER_FAKE_KEY", retries=1)
            result = run(lm)
    else:
        if not args.optimizer_model:
            parser.error("--optimizer-model is required (or --fake-optimizer)")
        lm = ChatLM(args.optimizer_model, base_url=args.optimizer_base_url, api_key_env=args.optimizer_key_env,
                    max_tokens=args.optimizer_max_tokens, temperature=args.optimizer_temperature)
        result = run(lm)
    print(json.dumps({k: result.get(k) for k in ("status", "session", "candidate", "changed_components", "rollouts",
                                                 "optimizer_usage")}, indent=2))
    return 0 if result.get("status") == "candidate_ready" else 1


def evaluate_main(argv=None):
    """harnessadapter evaluate: score adapters on one split with one Environment.

        harnessadapter evaluate --environment tasks/OfficeQA/host/optimize_env.py:make_environment \\
          --environment-config officeqa-env.yaml --split test \\
          --adapter none= --adapter gepa=runs/optimize-gepa-XXXX/candidate --output runs/test
    """
    parser = argparse.ArgumentParser(prog="harnessadapter evaluate", description=evaluate_main.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--environment-config", type=Path)
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--adapter", action="append", required=True, metavar="NAME=PATH",
                        help="An adapter to score; an empty PATH (NAME=) scores the harness without an adapter")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from harnessadapter.optimize.session import evaluate_adapters

    adapters = {}
    for item in args.adapter:
        name, sep, path = item.partition("=")
        if not sep or not name.strip():
            parser.error("--adapter takes NAME=PATH")
        adapters[name.strip()] = resolve_architecture(path).resolve() if path.strip() else None
    environment = load_callable(args.environment)(**_mapping(args.environment_config))
    results = evaluate_adapters(environment, adapters, args.split, output=args.output, repeats=args.repeats)
    print(json.dumps({name: {"mean": r["mean"], "runs": [run["mean"] for run in r["runs"]]} for name, r in results.items()}, indent=2))
    return 0
