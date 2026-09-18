"""The interface system: how the world model reaches the hosts (V2.2).

Design: claude-doc/interface-system.md. Two halves, both venue-agnostic:

  * the PUSH package — five sections in a fixed order, rendered by
    ``package()`` for every venue; a venue supplies only the transport note
    of the last section (how ``wm`` arrives for this kind of host);
  * the PULL tool — ``wm(name, args)``: six built-in names and the world
    model's own admitted tools in one namespace, dispatched by
    ``WorldModelInterface.call``.

The package and the tool text are information: what exists, what each
call takes and returns, what it costs. Through ``wm`` a host reaches the
world model repository only; earlier episodes reach it as the worked
examples the meta-agent wrote (``recall``) and, since 2026-09-08, as the
raw records behind the same addresses (``trace``, windowed).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, List, Optional

from . import capabilities, clbench_recall, host_framing, wm_tools
from .brief import served

BUILTINS = ("memory", "recall", "trace", "search", "read", "run", "delegate", "list")

# wm calls per episode, every venue, delegation included — the one number
# a host is told. It is a runaway guard, not a budget (user decision
# 2026-09-06: 50; observed use is one or two calls per episode).
CALLS_PER_EPISODE = 100     # 2026-09-13 (caps review item 17, user): 50 -> 100
CAP_NOTICE = ("wm call limit reached for this episode "
              f"({CALLS_PER_EPISODE}); act on the environment.")

# The native tool declaration, in the OpenAI function-tool shape every
# provider adapter accepts. How a model emits the call is the provider's
# business.
TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "wm",
        "description": (
            "The world model — knowledge maintained between episodes from "
            "earlier graded episodes. name: one of memory, recall, trace, "
            "search, read, run, delegate, list, or an admitted tool's name; args: an "
            "object per that name (see the 'Reaching the world model' "
            "section). wm reaches the world model only, "
            "never the environment."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["name"],
        },
    },
}

SECTION_TITLES = {
    "brief": "=== Environment briefing ===",
    "open": "=== Open questions ===",
    "catalog": "=== World-model memory (catalog) ===",
    "reach": "=== Reaching the world model ===",
}

BUILTIN_DOC = """The world model is a repository maintained between episodes from the graded
records of earlier episodes. You reach it through one tool, wm(name, args).
These calls reach the world model, not the environment. This standalone
deployment has no benchmark score or episode budget; the acting host owns
its limits. One past episode exists at three
grains: the briefing line that draws a conclusion from it; `recall`, the
world model's summary of that episode — what was asked, what decided it,
what was accepted, what the environment said — under the address the
line names; and `trace`, the raw record behind the same address, the
process turn by turn with nothing summarised. Before you rely on a
briefing claim for a decisive choice, and whenever exploration seems
expensive, reach for the world model first: `memory` for an entry named
in the catalog, `recall` for the summary behind an address, `trace` when
you need the process itself. Following an address is your call: the
briefing offers it, nothing requires it. Built-in names, input -> output:

  memory   input {"id": "<an [id] from the catalog>"}
    output: that entry's file — last_confirmed_t (the tick it was last
    confirmed at), scope (when it applies and when not), derived_from
    (the record addresses it was built from), brief (its one-line
    summary), open (an unsettled reading, if any), then the entry itself.
    e.g. {eg_memory}

  recall   input {"address": "<a record address — the form a briefing
    line or a derived_from line names, e.g. q17>"}
    output: the world model's summary of that episode — input (what the
    episode was given), outcome (how it ended, in the environment's own
    terms), scope, Decisive (the interpretation choices that settled it),
    Skeleton (the action or computation that was accepted, trimmed to its
    decisive shape), Feedback (what the environment said back). Its last
    line points at the raw record (trace). "no example" when none is kept
    for that address.
    e.g. {eg_recall}

  trace    input {"address": "<a record address, as for recall>", "from":
    <a turn number, default 0>}
    output: the process behind what recall summarises — the raw record
    of that episode, the file the world model was built from — from that
    turn on: each turn's action, the reasoning behind it where the record
    has it, and what the environment answered; the last turn carries the
    environment's feedback. Its first line gives the turn count and the
    file's path under /evidence — if you can reach a filesystem, you can
    also read it there yourself. As many turns as fit in 30,000
    characters; when turns remain, the last line says which turn to
    continue from. "no record" for an address that has none.
    e.g. {eg_trace}

  search   input {"query": "<a word or phrase>"}
    output: every line in the world model that contains it, each with
    the file it is in — a way to find the entry that mentions a table, a
    term or a value when the catalog does not name it. "no matches"
    otherwise.
    e.g. {eg_search}

  read     input {"path": "<a /world-model/... path — from the layout
    that list returns, from a search hit, or a directory: reading
    /world-model/ lists what is there>"}
    output: the file's text, or the directory's listing.
    e.g. {eg_read}

  list     input {}
    output: this section as it stands now, followed by the world model's
    current layout — each directory, how many files it holds, and for
    examples/ the record addresses it holds.
    e.g. {eg_list}"""

# Call shapes shown after each built-in (placeholders only — never a real
# id or address, which would steer the host toward an entry).
_EXAMPLE_ARGS = {
    "memory": '{"id": "<id>"}',
    "recall": '{"address": "q17"}',
    "trace": '{"address": "q17", "from": 0}',
    "search": '{"query": "<word>"}',
    "read": '{"path": "/world-model/memory/"}',
    "run": '{"code": "<Python>", "args": {}}',
    "delegate": '{"spec": "<name>", "task": "<text>"}',
    "list": "{}",
}


def _example(name: str, args_json: str) -> str:
    return f'wm(name="{name}", args={args_json})'


def _example_for_schema(name: str, schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    body = ", ".join(f'"{p}": <{(v or {}).get("type", "value")}>' for p, v in props.items())
    return _example(name, "{" + body + "}")

# Rendered after the built-ins only where delegation exists (a host with a
# delegation channel); sub-agents and MW_DELEGATE=0 runs never see it.
# The host's code channel (2026-09-15, user-approved text): rendered, like
# the delegate entry, only where the venue has it — a host whose harness
# runs python itself, in an image without bubblewrap (dabstep), gets
# neither the block nor the sentence.
RUN_INTRO = ("`run` executes code you write, in a\n"
             "sandbox that reaches neither. ")
RUN_DOC = """  run      input {"code": "<Python>", "args": {<a JSON object, default {}>}}
    output: the code runs in a sandbox — its own process, no network,
    no file system beyond a private /tmp, no access to the environment
    or to the world model — with `args` bound to the object you pass;
    whatever it assigns to `result` comes back as JSON, with anything it
    printed. Libraries: the sandbox Python standard library; no extra packages are promised. A traceback comes back as such.
    Wall-clock limit 600 s; printed output is cut at 50,000 characters,
    result at 20,000. Nothing persists between calls.
    e.g. {eg_run}
"""

DELEGATE_DOC = """  delegate input {"spec": "<a spec name listed below>", "task": "<what
    you want from it, in one message>"}
    output: the final message of a sub-agent started under that spec. It
    receives this episode's input and the same package you did, plus the
    spec; it acts in your seat with your tools except the one that ends
    this episode, and with wm: its environment actions and wm calls count
    as yours. It does not see this conversation. Slow (minutes).
    e.g. {eg_delegate}
"""

# Two transport modes, chosen by a harness fact (user decision 2026-09-06:
# a host must always have a way to run the world model, so there is no
# "briefing only" form). native: the harness declares tools through the
# provider and wm is one of them (database, poker, banking, retail).
# program: the harness runs the host's code and wm is a program in that
# environment (dabstep: a python function; a venue's transport note says
# which). Only the last sentence of section 5 changes between them.
NATIVE_TOOL_NOTE = "Here wm is one of your tools, beside the environment's."


def transport_note() -> str:
    return NATIVE_TOOL_NOTE


def _first_line(path: Path) -> str:
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line
    except OSError:
        pass
    return ""


def _schema_problem(schema: dict, args: dict) -> str:
    """The first way `args` violates the tool's JSON Schema, as one legible
    clause; "" when it validates. Full JSON Schema (types, enums, nested
    objects, ranges) through jsonschema when present; otherwise the
    required/unknown-parameter check."""
    if not isinstance(schema, dict) or not schema:
        return ""
    props = schema.get("properties") or {}
    try:
        import jsonschema
    except ImportError:                                    # pragma: no cover
        required = [r for r in (schema.get("required") or []) if r not in args]
        unknown = [k for k in args if props and k not in props]
        parts = ([f"missing {', '.join(required)}"] if required else []) + \
                ([f"unknown {', '.join(unknown)}"] if unknown else [])
        return "; ".join(parts)
    try:
        validator = jsonschema.Draft7Validator(schema)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    except jsonschema.SchemaError as exc:
        return f"the tool's SCHEMA is invalid ({exc.message})"
    if not errors:
        unknown = [k for k in args if props and k not in props
                   and schema.get("additionalProperties") is not False]
        return f"unknown {', '.join(unknown)}" if unknown else ""
    e = errors[0]
    where = ".".join(str(p) for p in e.path)
    return e.message + (f" (at {where})" if where else "")


def _params_of(schema: dict) -> str:
    props = (schema or {}).get("properties") or {}
    req = set((schema or {}).get("required") or [])
    return "{" + ", ".join(p if p in req else f"[{p}]" for p in props) + "}"


class WorldModelInterface:
    """One instance per world model (per arm); stateless across calls.

    delegate: the venue's delegation runner ``(spec, task) -> str`` or
    None where the venue offers none. tool_filter: an optional venue rule
    ``(run_py_path) -> refusal text or ""`` applied before an admitted
    tool runs (the database venue refuses tools that import sqlite3).
    uptake_log: where recall hits are logged (host-side, not learner-
    readable)."""

    def __init__(self, wrepo: str, evidence_dir: str = "", *,
                 delegate: Optional[Callable[[str, str], str]] = None,
                 tool_filter: Optional[Callable[[Path], str]] = None,
                 uptake_log: str = "", delegate_cap: int = CALLS_PER_EPISODE,
                 delegation: Optional[str] = None, code_channel: bool = True):
        from . import delegation as _dl
        self.wrepo = Path(wrepo)
        self.evidence_dir = Path(evidence_dir or (str(wrepo) + "_evidence"))
        self.delegate = delegate
        # Who runs a spec here (mwm/delegation.py): managed venues pass a
        # runner; native venues pass none and get the information text;
        # unspecified = managed iff a runner was given.
        self.delegation = delegation or ("managed" if delegate is not None else "none")
        if self.delegation not in _dl.MODES:
            raise ValueError(f"delegation mode {self.delegation!r}")
        if self.delegation == "managed" and delegate is None:
            raise ValueError("delegation='managed' needs a runner (delegate=)")
        self.tool_filter = tool_filter
        self.uptake_log = uptake_log
        self.delegate_cap = delegate_cap
        # wm run (mwm/sandbox.py): on unless the venue's host runs python
        # itself in an image without bubblewrap (dabstep).
        self.code_channel = code_channel

    # ----------------------------------------------------------- listing
    def admitted_tools(self) -> List[dict]:
        try:
            return capabilities.discover(str(self.wrepo))
        except Exception:                                  # noqa: BLE001
            return []

    def specs(self) -> List[tuple]:
        from . import delegate as _dg
        if not _dg.ENABLED or self.delegation == "none":
            return []
        try:
            return _dg.list_specs(str(self.wrepo))
        except Exception:                                  # noqa: BLE001
            return []

    # ------------------------------------------------ host tool declarations
    def host_tool_specs(self) -> List[dict]:
        """What a native-tool host declares beside the environment's tools:
        the one wm tool."""
        return [TOOL_SPEC]

    def host_tool_names(self) -> set:
        return {s["function"]["name"] for s in self.host_tool_specs()}

    def resolve(self, name: str, args) -> tuple:
        """(built-in or tool name, args) from a host tool call: the wm
        envelope, or a bare name tolerated as is."""
        if name == "wm":
            return parse_tool_call(args)
        return name, (args if isinstance(args, dict) else {})

    def reach_text(self, transport_note: str, with_delegate: bool = True) -> str:
        """Section 5; transport_note is the venue's last sentence.
        with_delegate=False renders it for a sub-agent, which cannot start
        a sub-agent (user 2026-09-07)."""
        from . import delegation as _dl
        doc = BUILTIN_DOC.replace("{calls_cap}", str(CALLS_PER_EPISODE))
        if self.code_channel:
            doc = doc.replace("never the environment. One past episode",
                              "never the environment. " + RUN_INTRO
                              + "One past episode")
            doc = doc.replace("  list     input {}", RUN_DOC + "\n  list     input {}")
        if self.delegation == "managed" and with_delegate:
            doc = doc.replace("  list     input {}", DELEGATE_DOC + "\n  list     input {}")
        for n, a in _EXAMPLE_ARGS.items():
            doc = doc.replace("{eg_" + n + "}", _example(n, a))
        parts = [doc]
        tools = self.admitted_tools()
        rows = [f"  {t['name']}  {_params_of(t.get('parameters'))}  "
                f"{t.get('description', '')}\n    e.g. "
                f"{_example_for_schema(t['name'], t.get('parameters'))}"
                for t in tools if t.get("name") not in BUILTINS]
        parts.append(
            "Tools admitted by the world model — wm(name=<name>, args=...); each "
            + "runs in a sandbox with no network and no access to the "
            "environment, on the arguments you pass, and returns its "
            "output:\n" + ("\n".join(rows) if rows else "  (none)"))
        specs = self.specs()
        srows = [f"  {n}  {first}" for n, first in specs]
        if self.delegation == "managed" and with_delegate:
            parts.append(
                'Sub-agent specs — wm(name="delegate", args={"spec": <name>, '
                '"task": ...}):\n' + ("\n".join(srows) if srows else "  (none)"))
        elif self.delegation == "native" and with_delegate:
            import textwrap
            parts.append(textwrap.fill(_dl.NATIVE_DOC, width=76) + "\n"
                         + ("\n".join(srows) if srows else "  (none)"))
        if transport_note:
            parts.append(transport_note)
        return "\n\n".join(parts)

    def package(self, transport_note: str, with_delegate: bool = True) -> str:
        """The five-section push package, identical in shape everywhere.
        with_delegate=False: the same package for a sub-agent, whose
        section 5 lists no delegate."""
        w = str(self.wrepo)
        bp = self.wrepo / "consumption" / "brief.md"
        try:
            # a brief.md that is a link outside the repository reads as
            # empty (the host must not fetch host files for the learner)
            brief = (served(bp.read_text().strip())
                     if wm_tools.inside(self.wrepo, bp) else "")
        except OSError:
            brief = ""
        oq = wm_tools.open_questions(w)
        cat = wm_tools.catalog(w)
        return "\n\n".join([
            host_framing.TEXT,
            SECTION_TITLES["brief"] + "\n" + (brief or "(empty briefing — nothing learned yet)"),
            SECTION_TITLES["open"] + "\n" + (oq or "(none)"),
            SECTION_TITLES["catalog"] + "\n" + (cat or "(empty)"),
            SECTION_TITLES["reach"] + "\n\n" + self.reach_text(transport_note, with_delegate),
        ])

    def layout(self) -> str:
        """The repository's current shape, for a host that wants a path:
        directories, file counts, and the addresses examples/ holds."""
        lines = ["Layout of /world-model (now):"]
        def files(d, suffix=".md", skip=("README.md", "_template.md")):
            p = self.wrepo / d
            if not p.is_dir():
                return []
            return sorted(f.name for f in p.iterdir()
                          if f.name.endswith(suffix) and f.name not in skip)
        mem = files("memory")
        lines.append(f"  memory/       {len(mem)} entries (the catalog above lists them; memory/<id>.md)")
        def _key(name):
            m = re.search(r"(\d+)", name)
            return (name[:m.start()] if m else name, int(m.group(1)) if m else 0, name)
        ex = sorted((f[:-3] for f in files("examples")), key=_key)
        rng = (f"{ex[0]} … {ex[-1]}" if len(ex) > 6 else ", ".join(ex)) if ex else "(none)"
        lines.append(f"  examples/     {len(ex)} records (examples/<address>.md): {rng}")
        tools = [t["name"] for t in self.admitted_tools()]
        lines.append(f"  tools/        {len(tools)} admitted" + (": " + ", ".join(tools) if tools else "") + " (tools/<name>/TOOL.md documents one)")
        specs = [n for n, _ in self.specs()]
        lines.append(f"  agents/       {len(specs)} sub-agent specs" + (": " + ", ".join(specs) if specs else "") + " (agents/<name>/SPEC.md)")
        lines.append("  consumption/  brief.md (the briefing above)")
        meta = self.wrepo / "meta"
        if meta.is_dir():
            items = sorted(f.name + ("/" if f.is_dir() else "") for f in meta.iterdir() if not f.name.startswith("."))
            lines.append("  meta/         " + ", ".join(items[:8]) + (" …" if len(items) > 8 else "") + " (the world model's own working notes)")
        return "\n".join(lines)

    # ----------------------------------------------------------- dispatch
    def call(self, name: str, args: Optional[dict] = None,
             transport_note: str = "") -> str:
        """Every failure is a legible string — to the host this is a tool
        result."""
        name = (name or "").strip()
        if name.lower() in BUILTINS:
            name = name.lower()
        args = args if isinstance(args, dict) else {}
        try:
            if name == "memory":
                return wm_tools.read_memory(self.wrepo, str(args.get("id", "")))
            if name == "recall":
                out = clbench_recall.recall(
                    str(self.wrepo), str(args.get("address", "")),
                    self.uptake_log)
                return out + self._trace_pointer(str(args.get("address", "")))
            if name == "trace":
                from . import trace as _trace
                return _trace.render(str(self.evidence_dir),
                                     str(args.get("address", "")),
                                     args.get("from", 0))
            if name == "search":
                return wm_tools._search(self.wrepo, str(args.get("query", "")))
            if name == "read":
                return self._read(str(args.get("path", "")))
            if name == "run":
                if not self.code_channel:
                    return ("run: not available in this deployment (the "
                            "required execution channel is unavailable)")
                return self._run_code(args)
            if name == "delegate":
                if self.delegation == "native":
                    from . import delegation as _dl
                    return _dl.NATIVE_CALL_NOTICE.format(
                        spec=str(args.get("spec", "")) or "<name>")
                if self.delegation != "managed" or self.delegate is None:
                    return ("delegate: not available in this environment "
                            "(no delegation channel)")
                return self.delegate(str(args.get("spec", "")),
                                     str(args.get("task", "")))
            if name == "list":
                return (self.reach_text(transport_note) + "\n\n"
                        + self.layout())
            if not name:
                return ("usage: wm(name, args) — name is one of "
                        + ", ".join(BUILTINS) + " or an admitted tool's name")
            return self._run_tool(name, args)
        except Exception as exc:                           # noqa: BLE001
            return f"wm error: {exc}"

    def _run_code(self, args: dict) -> str:
        """wm run (2026-09-15): the host's own code, once, in mwm/sandbox.py.
        The result dict goes back as JSON text — to the host this is a tool
        result like any other."""
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return ('run: give {"code": "<Python>", "args": {...}} — code is '
                    'a non-empty string; set `result` in it')
        inner = args.get("args")
        if inner is None:
            inner = {}
        if not isinstance(inner, dict):
            return "run: args must be a JSON object"
        from . import sandbox
        out = sandbox.run_sandboxed(code, inner)
        return json.dumps(out, ensure_ascii=False, default=str)

    def _trace_pointer(self, address: str) -> str:
        """The line recall ends with: where the raw record behind the same
        address is (2026-09-08 — one episode at three grains: briefing
        line, recall's summary, trace's process)."""
        from . import trace as _trace
        path = _trace.record_path(str(self.evidence_dir), address)
        if not path:
            return ""
        try:
            rec = json.load(open(path))
            n = f"{len(rec)} turns" if isinstance(rec, list) else "one record"
        except (OSError, ValueError):
            return ""
        mount = _trace._mount_path(str(self.evidence_dir), path)
        return (f"\nraw record: {n} — wm(name=\"trace\", args={{\"address\": "
                f"\"{address}\"}}), or the file {mount}")

    def _read(self, target: str) -> str:
        target = (target or "").strip()
        if target.startswith("/evidence"):
            return ("read: /evidence is not reachable through wm — earlier "
                    "episodes reach you as worked examples (recall); where "
                    "your harness reads files directly, open the record "
                    "store yourself")
        if not target.startswith("/world-model/"):
            return f"read: give a /world-model/... path, not {target!r}"
        return wm_tools._read_target(self.wrepo, self.evidence_dir, target)

    def _run_tool(self, name: str, args: dict) -> str:
        if "/" in name or name.startswith("."):
            return f"wm: not a tool name: {name!r}"
        tools = {t["name"]: t for t in self.admitted_tools()}
        paths = dict(capabilities._entries(str(self.wrepo)))
        if name not in tools or name not in paths:
            if (self.wrepo / "agents" / name).exists():
                return (f"{name!r} is a sub-agent spec: call "
                        f'wm("delegate", {{"spec": "{name}", "task": ...}})')
            names = list(tools)
            return (f"wm: no name {name!r}. wm reaches the world model only "
                    f"(built-ins: {', '.join(BUILTINS)}; admitted tools: "
                    f"{', '.join(names) if names else '(none)'}); it never "
                    f"reaches the environment.")
        # Arguments are checked against the SCHEMA the meta-agent wrote,
        # so a wrong call gets the parameter list back, not a traceback.
        schema = tools[name].get("parameters") or {}
        problem = _schema_problem(schema, args)
        if problem:
            return f"wm {name}: {problem}. Parameters: {_params_of(schema)}"
        if self.tool_filter is not None:
            refusal = self.tool_filter(Path(paths[name]))
            if refusal:
                return refusal
        return capabilities.invoke(str(self.wrepo), name, args)


def parse_tool_call(arguments) -> tuple:
    """(name, args) from a native tool call's JSON arguments string or
    dict; tolerant of a model that puts the name inside args."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return "", {}
    if not isinstance(arguments, dict):
        return "", {}
    name = str(arguments.get("name") or "")
    args = arguments.get("args")
    if not isinstance(args, dict):
        args = {k: v for k, v in arguments.items() if k not in ("name", "args")}
    return name, args
