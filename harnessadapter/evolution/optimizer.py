"""Optimizer Evolvers: search algorithms that evolve an adapter's text by
rolling candidates out in an Environment (GEPA, SkillOpt, or your own).

An optimizer Evolver is a definition directory, like every Evolver:

    my-optimizer/
      evolver.toml    kind = "optimizer"
                      plugin = "plugin.py:run"     (a file here, or an importable module:function)
                      [settings]                   defaults; --set / --settings override them
      plugin.py       def run(context) -> Outcome

It runs through the same command as every Evolver:

    harnessadapter evolve prompt-only --evolver my-optimizer \\
      --environment module:factory --model MODEL

The plugin only searches. The framework snapshots the source adapter, hands
the plugin a Context (seed texts, train and val tasks, evaluate(), the model,
a Training State directory) and turns its Outcome into a committed candidate
adapter and a result record:

    <output>/evolve-<evolver>-XXXX/
      adapter/          the selected candidate, a git repository; deploy it
      source/           snapshot of the starting adapter (never modified)
      training_state/   the plugin's own state; pass it back with --training-state to resume
      work/             every evaluated candidate, materialized once by content hash
      control/          result.json, evaluations.jsonl (every evaluate call), model-calls/

Nothing is promoted automatically, and the test split is never touched here:
`harnessadapter evaluate` scores finished adapters on it, for every Evolver alike.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import tomllib
from typing import Callable, Sequence

import yaml

from harnessadapter.adapter import versioning
from harnessadapter.adapter.loader import inside, load_manifest
from harnessadapter.environment import (INFRA_PAUSE, INFRA_RERUNS, Environment, Rollout, Task,
                                        load_environment, run_tasks, split_sizes)
from harnessadapter.paths import load_object, resolve_evolver, workspace_root

IGNORED = {".git", "__pycache__", ".DS_Store"}
SPLITS_FILE = "harnessadapter-splits.json"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def candidate_hash(candidate: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def copy_adapter(source: Path, destination: Path) -> None:
    """Copy an adapter without VCS metadata. Symlinks are refused: the acting
    harness treats an adapter as a sealed snapshot."""
    source = Path(source).resolve()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Adapter symlinks are not supported: {relative}")
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


class TextSurface:
    """The files of an adapter an optimizer may rewrite, as a candidate
    {relative path: text}. The default is the adapter's context document
    (ADAPTER.md for prompt-only); every other file is copied unchanged."""

    def __init__(self, base_adapter: Path, components: list[str] | None = None):
        self.base = Path(base_adapter).resolve()
        if components is None:
            context = load_manifest(self.base).get("context")
            if not isinstance(context, str):
                raise ValueError("This adapter renders its context in code; name the text files with --component")
            components = [context]
        if not components:
            raise ValueError("A text surface needs at least one file")
        for name in components:
            inside(self.base, name)
            if name == "adapter.json":
                raise ValueError("The manifest is not an optimizable text")
        self.components = list(dict.fromkeys(components))

    def read(self) -> dict[str, str]:
        return {name: inside(self.base, name).read_text() for name in self.components}

    def check(self, candidate: dict[str, str]) -> dict[str, str]:
        if set(candidate) != set(self.components) or not all(isinstance(v, str) for v in candidate.values()):
            raise ValueError(f"A candidate maps exactly {self.components} to texts; got {sorted(candidate)}")
        return {name: candidate[name] for name in self.components}

    def write(self, candidate: dict[str, str], destination: Path) -> Path:
        """A complete, loadable adapter at destination (which must not exist):
        the base's files with the candidate's texts in place."""
        candidate = self.check(candidate)
        destination = Path(destination)
        if destination.exists():
            raise ValueError(f"Refusing to overwrite {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=destination.parent))
        try:
            copy_adapter(self.base, staging)
            for name, text in candidate.items():
                (staging / name).write_text(text)
            load_manifest(staging)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination.resolve()


@dataclass
class Outcome:
    """A plugin's answer: the selected texts, and a JSON summary (scores,
    lineage, budget used) recorded verbatim in result.json."""
    candidate: dict[str, str]
    summary: dict = field(default_factory=dict)


class Context:
    """What a plugin receives.

    seed          {relative path: text}: the adapter's optimizable texts
    components    their paths, in order
    train, val    the tasks a plugin may evaluate; test is never available
    evaluate()    deploy candidate texts and run tasks: the only way to score
    model         the Evolver model, a callable str | messages -> str
                  (ChatLM: .serve() gives a library that drives its own client
                  a local endpoint to the same model)
    settings      the definition's [settings] with this run's overrides
    state_dir     the Training State: the plugin's own files, already holding
                  the previous state when the session resumes
    log           progress messages

    A plugin writes only under state_dir. evaluate() keeps a well-behaved
    plugin away from held-out data; it is not a sandbox, and a plugin is
    trusted code in this process.
    """

    def __init__(self, surface: TextSurface, environment: Environment, *, model, settings: dict,
                 state_dir: Path, work_dir: Path, record: Path, log: Callable[[str], None] = print,
                 reruns: int = INFRA_RERUNS, pause: float = INFRA_PAUSE):
        self.components = list(surface.components)
        self.train = environment.tasks("train")
        self.val = environment.tasks("val")
        self.model = model
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.log = log
        self._surface = surface
        self._seed = surface.read()
        self._environment = environment
        self._tasks = {t.id: t for t in self.train + self.val}
        self._candidates = Path(work_dir) / "candidates"
        self._record = Path(record)
        self._reruns, self._pause = reruns, pause
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()
        self.calls = self.rollouts = self.infra_lost = 0

    @property
    def seed(self) -> dict[str, str]:
        return dict(self._seed)

    def _adapter(self, candidate: dict[str, str]) -> Path:
        path = self._candidates / f"cand-{candidate_hash(self._surface.check(candidate))[:16]}"
        if not path.exists():
            try:
                self._surface.write(candidate, path)
            except (ValueError, OSError):
                if not path.exists():   # else another thread wrote it first
                    raise
        return path

    def evaluate(self, candidate: dict[str, str], tasks: Sequence[Task], *, label: str = "") -> list[Rollout]:
        """Deploy the candidate texts and run the tasks (train or val) in the
        Environment. Every task is resolved by id against the registered
        train and val tasks, so a test task is refused and a relabeled one is
        run as what it is. Episodes lost to the infrastructure are re-run
        (still lost: infra=True, score 0). Rollouts come back without record
        locations, and every call is logged."""
        requested = list(tasks)
        unknown = [t.id for t in requested if t.id not in self._tasks]
        if unknown:
            raise PermissionError(f"Optimizers evaluate only train and val tasks; refused {unknown[:5]}")
        registered = [self._tasks[t.id] for t in requested]
        adapter = self._adapter(candidate)
        with self._lock:
            label = label or f"call-{next(self._numbers):04d}"
        started = time.monotonic()
        rollouts, extra = run_tasks(self._environment, adapter, registered, label, reruns=self._reruns, pause=self._pause)
        lost = sum(1 for r in rollouts if r.infra)
        with self._lock:
            self.calls += 1
            self.rollouts += len(rollouts) + extra
            self.infra_lost += lost
            self._record.parent.mkdir(parents=True, exist_ok=True)
            with self._record.open("a") as out:
                out.write(json.dumps({"utc": _utc(), "label": label, "candidate": adapter.name,
                                      "tasks": [t.id for t in registered], "splits": sorted({t.split for t in registered}),
                                      "scores": [r.score for r in rollouts], "statuses": [r.status for r in rollouts],
                                      "infra": lost, "infra_reruns": extra,
                                      "seconds": round(time.monotonic() - started, 1)}) + "\n")
        return [dataclasses.replace(r, record=None) for r in rollouts]


@dataclass
class OptimizerDefinition:
    root: Path
    name: str
    run: Callable[[Context], Outcome]
    settings: dict
    description: str = ""

    def merged(self, overrides: dict | None) -> dict:
        unknown = set(overrides or {}) - set(self.settings)
        if unknown:
            raise ValueError(f"Unknown {self.name} settings: {', '.join(sorted(unknown))}")
        return {**self.settings, **(overrides or {})}


def _config(reference: str) -> tuple[Path, dict]:
    root = resolve_evolver(reference).resolve()
    path = root / "evolver.toml"
    if not path.is_file():
        raise ValueError(f"No Evolver definition (evolver.toml) at {root}")
    return root, tomllib.loads(path.read_text())


def is_optimizer(reference: str) -> bool:
    try:
        return _config(reference)[1].get("kind") == "optimizer"
    except (ValueError, OSError, tomllib.TOMLDecodeError):
        return False


def load_optimizer(reference: str) -> OptimizerDefinition:
    root, config = _config(reference)
    if config.get("kind") != "optimizer":
        raise ValueError(f"{reference} is not an optimizer Evolver (evolver.toml has no kind = \"optimizer\")")
    unknown = set(config) - {"kind", "plugin", "settings", "description"}
    if unknown:
        raise ValueError(f"Unknown optimizer Evolver fields: {', '.join(sorted(unknown))}")
    run = load_object(str(config.get("plugin", "")), root)
    if not callable(run):
        raise ValueError(f"{config.get('plugin')} is not callable")
    return OptimizerDefinition(root=root, name=root.name, run=run, settings=dict(config.get("settings", {})),
                               description=str(config.get("description", "")))


def split_fingerprint(environment: Environment) -> dict:
    """The train/val ids and a hash of the test ids an optimizer ran with:
    stored in the Training State and checked when a session resumes from it."""
    test = sorted(t.id for t in environment.tasks("test"))
    return {"ids": {split: [t.id for t in environment.tasks(split)] for split in ("train", "val")},
            "test_sha256": hashlib.sha256(json.dumps(test).encode()).hexdigest(), "test_count": len(test)}


def check_resume(state: Path, fingerprint: dict) -> None:
    path = Path(state) / SPLITS_FILE
    if not path.is_file():
        raise ValueError(f"{state} records no split assignment ({SPLITS_FILE}); refusing to resume from it")
    stored = json.loads(path.read_text())
    if stored.get("ids") != fingerprint["ids"]:
        raise ValueError("The train/val assignment differs from the one this Training State was built on")
    if stored.get("test_sha256") != fingerprint["test_sha256"]:
        raise ValueError("The test split differs from the one this Training State was built on")


def optimize(source: Path, environment: Environment, evolver: str, *, model, output: Path,
             settings: dict | None = None, components: list[str] | None = None,
             training_state: Path | None = None, log: Callable[[str], None] = print) -> dict:
    """One optimizer Evolver session; returns its result record."""
    definition = load_optimizer(evolver)
    merged = definition.merged(settings)
    source = Path(source).resolve()
    sizes = split_sizes(environment)
    if not sizes["train"] or not sizes["val"]:
        raise ValueError("An optimizer Evolver needs nonempty train and val splits")
    fingerprint = split_fingerprint(environment)
    if training_state is not None:
        training_state = Path(training_state).resolve()
        check_resume(training_state, fingerprint)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"evolve-{definition.name}-", dir=output))
    for name in ("source", "training_state", "work", "control"):
        (session / name).mkdir()
    copy_adapter(source, session / "source")
    versioning.copy_history(source, session / "source")   # the history the candidate carries
    surface = TextSurface(session / "source", components)
    if training_state is not None:
        shutil.copytree(training_state, session / "training_state", dirs_exist_ok=True)
    _save(session / "training_state" / SPLITS_FILE, fingerprint)
    if getattr(model, "ledger", False) is None:
        model.ledger = session / "control/model-calls"
    context = Context(surface, environment, model=model, settings=merged, state_dir=session / "training_state",
                      work_dir=session / "work", record=session / "control/evaluations.jsonl", log=log)
    record = {"session": str(session), "status": "running", "evolver": definition.name, "evolver_kind": "optimizer",
              "evolver_definition": str(definition.root), "source": str(source), "settings": merged,
              "components": surface.components, "environment": environment.describe(), "splits": sizes,
              "split_test_sha256": fingerprint["test_sha256"],
              "model": model.describe() if hasattr(model, "describe") else None,
              "resumed_from": str(training_state) if training_state else None,
              "seed_hash": candidate_hash(surface.read()), "started_utc": _utc()}
    _save(session / "control/result.json", record)
    started = time.monotonic()

    def counts():
        return {"evaluation_calls": context.calls, "rollouts": context.rollouts,
                "infra_lost_rollouts": context.infra_lost, "usage": dict(getattr(model, "usage", {}) or {}),
                "finished_utc": _utc(), "seconds": round(time.monotonic() - started, 1)}
    try:
        outcome = definition.run(context)
        if not isinstance(outcome, Outcome):
            raise TypeError(f"{definition.name} returned {type(outcome).__name__}, not an Outcome")
        selected = surface.check(outcome.candidate)
    except BaseException as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}", **counts())
        _save(session / "control/result.json", record)
        raise
    # The candidate is the snapshot the search evaluated against, with the
    # source's history and one commit for the selected texts.
    adapter = session / "adapter"
    copy_adapter(session / "source", adapter)
    versioning.copy_history(session / "source", adapter)
    versioning.ensure_repo(adapter)
    for name, text in selected.items():
        (adapter / name).write_text(text)
    commit = versioning.commit_all(adapter, f"evolve: {definition.name} session {session.name}")
    seed = surface.read()
    record.update(status="candidate_ready", candidate=str(adapter), candidate_hash=candidate_hash(selected),
                  candidate_commit=commit, changed_components=[n for n in surface.components if selected[n] != seed[n]],
                  plugin=outcome.summary, **counts())
    _save(session / "control/result.json", record)
    return record


def run_from_cli(parser, args, source: Path, evolver: str) -> int:
    """`harnessadapter evolve` with an optimizer Evolver (session.main parses)."""
    from harnessadapter.evolution.llm import ChatLM

    settings = {}
    if args.settings:
        settings = yaml.safe_load(Path(args.settings).read_text()) or {}
        if not isinstance(settings, dict):
            parser.error("--settings must contain a mapping")
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            parser.error("--set takes KEY=VALUE")
        try:                  # JSON first (what launch YAML writes: exact floats and text), then YAML
            settings[key.strip()] = json.loads(value)
        except ValueError:
            try:
                settings[key.strip()] = yaml.safe_load(value)
            except yaml.YAMLError:
                settings[key.strip()] = value
    environment = load_environment(args.environment, args.environment_config)
    output = (args.output or workspace_root() / "evolution").resolve()

    def session(model):
        return optimize(source, environment, evolver, model=model, output=output, settings=settings,
                        components=args.component, training_state=args.training_state)

    if args.scripted_model:
        from harnessadapter.testing import FakeChatServer
        os.environ.setdefault("HARNESSADAPTER_SCRIPTED_KEY", "scripted")
        with FakeChatServer() as server:
            result = session(ChatLM("scripted", base_url=server.base_url, api_key_env="HARNESSADAPTER_SCRIPTED_KEY",
                                    retries=1))
    else:
        if not args.model:
            parser.error("An optimizer Evolver needs --model (or --scripted-model for a $0 toy run)")
        result = session(ChatLM(args.model, api_key_env=args.provider_env_key or "OPENAI_API_KEY",
                                base_url=args.provider_base_url or os.environ.get("OPENAI_BASE_URL")
                                or "https://api.openai.com/v1",
                                max_tokens=args.max_tokens, temperature=args.temperature,
                                reasoning_effort=args.reasoning_effort))
    print(json.dumps({k: result.get(k) for k in ("status", "session", "candidate", "changed_components",
                                                 "rollouts", "usage")}, indent=2))
    return 0 if result.get("status") == "candidate_ready" else 1
