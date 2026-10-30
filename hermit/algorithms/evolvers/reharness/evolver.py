"""ReHarnessEvolver: the recurrent harness of Xiao et al. (arXiv:2610.12668) on the Trainer, one step per instance.

The harness state h_t = {R_i}_{i<t} is the adapter's reflection bank (template reharness). Each instance t of the
stream (a training step of one task, data.step_size 1, data.shuffle false) runs the paper's three steps:

1. Forward read (Eq. 1), on the acting side: I_t = Retrieval(x_t, h_t, k); the decision agent gets {R_i}_{i in I_t}
   below ADAPTER.md (the template's context tool, called by the pipeline through Session.context). The pipeline
   records what was retrieved in RunOutput.extra["context_tool"], which the Trainer keeps as `pipeline_extra`.
2. Backward revision (Eq. 2), here: R'_i = pi_R(R_i, tau_i; tau_t) for every i in I_t, each independently, with the
   paper's RevisionAgent prompt: the source trajectory tau_i (kept in the Training State) is the sole source of
   content, the new trajectory tau_t only feedback. The revised texts replace the retrieved ones; the rest of the
   bank is untouched (h'_t).
3. Forward write (Eq. 3), here: R_t = pi_M(tau_t; {R'_i}) with the ReflectionAgent prompt and the revised reflections
   as demonstrations; the new reflection, with the embedding of x_t, joins the bank: h_{t+1} = h'_t (+) R_t.

The agents are the run's `llm` tool (the same backbone as the acting model, as in the paper, when the run binds
them so); their calls are metered by the Trainer. Embeddings go to the `embedding` model (Qwen3-Embedding-8B in the
paper) through an OpenAI-compatible /embeddings endpoint; without one, retrieval is BM25 over the instances' inputs.
`bootstrap` writes index.json (k, the retrieval, the model) so the first instance already reads with the right
settings (its bank is empty). Ablations: `revise: false` (the paper's No Revision), `demonstrations: false`,
`raw_memory: true` (Top-K Raw: the trajectory itself is stored, no agent calls).

Training State: state/reharness.json (one record per instance: what was retrieved, each revision's size, the new
reflection, the calls) and state/trajectories/<memory id>.txt (tau_i, what a later revision of R_i reads).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import os
from pathlib import Path
import re

from hermit.algorithms.evolvers.reharness import prompts, trajectory
from hermit.algorithms.templates.reharness.reharness_core import bank as _bank, retrieval as _retrieval
from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult, Usage

STATE = "reharness.json"
TRAJECTORIES = "trajectories"
KEEP_CHARS = 400000            # of a stored source trajectory


def indel_distance(a: str, b: str) -> int:
    """A revision's magnitude: the insert/delete edit distance from difflib's matching blocks (len(a) + len(b) - 2 *
    matched characters). The paper reports a character-level edit distance (Figure 4a; conventionally Levenshtein,
    where a substitution costs 1 rather than 2): a comparable measure, not the same one."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return len(a) + len(b) - 2 * matched


_THINK_CLOSE = "</think>"
_NO_ESCAPE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')       # a backslash that starts no JSON escape


class _Object(dict):
    """A decoded JSON object that keeps every value of a key repeated inside it: json keeps only the last, and a
    model sometimes writes the reflection and then the same key again with a stub ({"reflection": "<the text>",
    "reflection": "placeholder"})."""

    def __init__(self, pairs):
        super().__init__(pairs)
        self.repeated: dict[str, list] = {}
        for k, v in pairs:
            self.repeated.setdefault(k, []).append(v)


_DECODER = json.JSONDecoder(strict=False, object_pairs_hook=_Object)


def _answer(text: str) -> str:
    """The reply without reasoning that leaked into it: what follows the last </think>."""
    return text.rsplit(_THINK_CLOSE, 1)[-1] if _THINK_CLOSE in text else text


def _objects(text: str):
    """Every JSON object in the text, in order (fenced or not, with prose around it or not)."""
    pos = 0
    while (start := text.find("{", pos)) >= 0:
        try:
            value, end = _DECODER.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        if isinstance(value, dict):
            yield value
        pos = max(end, start + 1)


def _string(body: str) -> str | None:
    """The text of a JSON string from what stood between its quotes, repairing what models get wrong there: a
    backslash that starts no escape (`\\_`, `\\%`), then a bare double quote inside the text."""
    repaired = _NO_ESCAPE.sub(r"\\\\", body)
    for candidate in (body, repaired, re.sub(r'(?<!\\)"', r'\\"', repaired)):
        try:
            value = json.loads('"' + candidate + '"', strict=False)
        except ValueError:
            continue
        return value if value.strip() else None
    return None


def extract_field(text: str, key: str) -> tuple[str | None, str | None]:
    """The string under `key` in the JSON object the agent was asked for, and how it was read: "json" from a parsed
    object (the last one holding the key, when a draft precedes the final one, unless it is a stub under a tenth of
    an earlier one's length: a closing {"reflection":"see above"} after the real object; within one object that
    repeats the key, its longest value), "json field" from the field alone when no object parses (a bare quote or an
    invalid escape inside the string). Tolerant of fences, surrounding prose, the key's case and reasoning that
    leaked into the reply; (None, None) when the reply holds no such field."""
    if not text:
        return None, None
    text = _answer(text)
    hits = []
    for obj in _objects(text):
        values = [v for k, vs in obj.repeated.items() if k.lower() == key.lower()
                  for v in vs if isinstance(v, str) and v.strip()]
        if values:
            hits.append(max(values, key=lambda v: len(v.strip())))
    if hits:
        value, longest = hits[-1], max(hits, key=lambda v: len(v.strip()))
        return (longest if 10 * len(value.strip()) < len(longest.strip()) else value), "json"
    escaped = re.escape(key)
    for pattern in (r'"%s"\s*:\s*"(.*)"\s*\}' % escaped, r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % escaped):
        match = re.search(pattern, text, re.S | re.I)
        if match and (value := _string(match.group(1))) is not None:
            return value, "json field"
    return None, None


def parse_json_field(text: str, key: str) -> str | None:
    """The string under `key` in the JSON object the agent was asked for, or None (see extract_field)."""
    return extract_field(text, key)[0]


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))[:60] or "task"


class ReHarnessEvolver(Evolver):
    def __init__(self):
        self._client = None          # the embedding client, or None for BM25
        self._client_for = None      # the params it was built from

    # ---------- the embedding model ----------
    def _embedder(self, params: dict):
        spec = params.get("embedding")
        key = json.dumps(spec, sort_keys=True, default=str)
        if self._client_for == key:
            return self._client
        self._client, self._client_for = None, key
        if not spec:
            return None
        from hermit.backends import Binding, as_binding, registry
        binding = as_binding(spec, registry()) if not (isinstance(spec, dict) and spec.get("base_url")) else None
        if binding is None:
            if isinstance(spec, dict) and spec.get("base_url") and spec.get("model"):
                binding = Binding.from_spec(spec)
            else:
                raise ValueError(f"embedding {spec!r}: a `<backend>/<model id>` reference (hermit backends list) or "
                                 "{base_url, model, api_key_env}")
        self._client = _retrieval.EmbeddingClient(binding.base_url, binding.model,
                                                  api_key_env=binding.key_env_if_set())
        self._client.price = binding.price
        self._client.ref = binding.ref
        return self._client

    def _embed(self, client, texts: list[str], usage: list) -> list[list[float]]:
        vectors, used = client.embed(texts)
        usage.append(Usage(client.ref, input_tokens=used["prompt_tokens"], calls=used["calls"],
                           cost_usd=(used["prompt_tokens"] * client.price.get("input", 0) / 1e6)
                           if client.price else None))
        return vectors

    # ---------- lifecycle ----------
    def setup(self, ctx) -> None:
        p = ctx.params
        if "llm" not in ctx.tools and not p.get("raw_memory"):
            raise RuntimeError("the reharness Evolver needs the run's llm tool (its reflection and revision agents): "
                               "set `llm` (or `model`) in the run config")
        client = self._embedder(p)
        if client is not None:
            url_env = _retrieval.READER_URL_ENV
            url = os.environ.get(url_env)
            if not url:
                raise RuntimeError(f"embedding retrieval needs {url_env} in the environment: the acting side's reader "
                                   "takes the embedding endpoint from it (inside a run's sandbox a loopback relay, "
                                   "127.0.0.1 or localhost, in front of the provider, as the examples' launch.sh "
                                   f"starts; the Evolver's own endpoint here is {client.base_url})")
            if ctx.sandbox is not None and getattr(ctx.sandbox, "enabled", False) and \
                    not re.match(r"https?://(?:127\.0\.0\.1|localhost)[:/]", url):
                raise RuntimeError(f"{url_env}={url}: a sandboxed adapter reaches loopback endpoints (127.0.0.1 or "
                                   "localhost) only; put a relay on the loopback in front of the provider, or run with "
                                   "sandbox.enabled: false")
            vectors, _ = client.embed(["ping"])       # fail now, not at the first instance
            self._dim = len(vectors[0])

    def bootstrap(self, ctx: StepContext) -> StepResult:
        """Step 0: index.json from the parameters, so the first instance reads with them; a starting bank from
        another retrieval setting is re-embedded."""
        p, adapter = ctx.params, ctx.workspace.adapter
        client = self._embedder(p)
        index = _bank.read_index(adapter)
        reflections = _bank.load_bank(adapter)
        usage: list[Usage] = []
        model = client.model if client else None
        if client is not None:
            stale = [r for r in reflections if r.embedding is None or r.model != model]
            if stale:
                for r, vector in zip(stale, self._embed(client, [r.query for r in stale], usage)):
                    r.embedding, r.model = vector, model
                    _bank.save_reflection(adapter, r)
        dim = getattr(self, "_dim", None) or next((len(r.embedding) for r in reflections if r.embedding), None)
        new = {"k": int(p.get("k", 5)), "retrieval": "embedding" if client else "bm25", "embedding_model": model,
               "dim": dim, "memory": "trajectory" if p.get("raw_memory") else "reflection"}
        _bank.write_index(adapter, new)
        state = self._state(ctx)
        state["index"] = new
        self._save_state(ctx, state)
        return StepResult("done", f"index.json: k={new['k']}, retrieval={new['retrieval']}"
                          + (f" ({model})" if model else "") + f"; {len(reflections)} reflection(s) to start from",
                          usage=usage)

    def step(self, ctx: StepContext) -> StepResult:
        p, adapter = ctx.params, ctx.workspace.adapter
        n = ctx.progress.step if ctx.progress else 0
        raw_memory = bool(p.get("raw_memory"))
        if "llm" not in ctx.tools and not raw_memory:
            return StepResult("failed", "reharness needs the run's llm tool: set `llm` in the run config")
        client = self._embedder(p)
        index = _bank.read_index(adapter)
        k = int(index["k"])
        reflections = _bank.load_bank(adapter)
        by_id = {r.id: r for r in reflections}
        bank_at_start = list(reflections)                 # h_t: what the rollouts read
        current = ctx.workspace.learning_material / "current"
        summary = json.loads((current / "summary.json").read_text())
        records = []
        for tid in summary["tasks"]:
            path = current / "tasks" / f"{_safe(tid)}.json"
            if path.is_file():
                records.append(json.loads(path.read_text()))
        if len(records) > 1:
            note = (f"{len(records)} instances in one step: processed in the stream's order, each with its own "
                    "retrieval and revision; the paper's protocol is one instance per step (data.step_size 1)")
        else:
            note = ""
        state = self._state(ctx)
        usage: list[Usage] = []
        sampling = {key: p[key] for key in ("max_tokens", "temperature") if p.get(key) is not None}
        trace_chars, obs_chars = int(p.get("trace_chars", 24000)), int(p.get("obs_chars", 4000))
        feedback = bool(p.get("feedback", True))      # off: the judge's verdict stays out of tau_t (a label-free stream)
        store = ctx.workspace.state / TRAJECTORIES
        store.mkdir(parents=True, exist_ok=True)
        instance_records, lines, unrecorded = [], [], 0

        def stopped() -> StepResult:
            # The Trainer keeps the previous adapter for a stopped step: the Training State must not claim what the
            # bank will not hold (the trajectories stored for this step's new memories go; the records are not saved).
            for rec in instance_records:
                if rec.get("memory_id"):
                    (store / f"{_bank.safe_id(rec['memory_id'])}.txt").unlink(missing_ok=True)
            return StepResult("failed", "stopped by the Trainer: this step's bank changes are not committed, so nothing "
                                        "of it is recorded", usage=usage)
        for record in records:
            if ctx.cancel is not None and ctx.cancel.is_set():
                return stopped()
            tid = str(record["id"])
            tau_t = trajectory.render(record, current, obs_chars=obs_chars, feedback=feedback)
            tau_t_shown = trajectory.clip_middle(tau_t, trace_chars)
            x_t = trajectory.query_of(record)
            rec = {"step": n, "task": tid, "query_chars": len(x_t), "trajectory_chars": len(tau_t),
                   "feedback": feedback}
            if record.get("status") == "error" and not (record.get("trajectory") or record.get("messages")
                                                        or record.get("output")):
                rec["outcome"] = "skipped: the pipeline failed and left no trajectory"
                instance_records.append(rec)
                lines.append(f"- {tid}: {rec['outcome']}")
                continue
            # 1. what the decision agent read: the pipeline's record of the context tool's result, else recomputed.
            info = (record.get("pipeline_extra") or {}).get("context_tool") or {}
            retrieved = info.get("retrieved") if isinstance(info, dict) else None
            recorded = retrieved is not None
            provenance = ""
            if recorded:
                rec["retrieval"] = str(info.get("retrieval", "recorded"))
            elif p.get("recompute_retrieval"):
                # The pipeline called the reader but did not record its result: the reader's own ranking, again.
                embed = (lambda texts: (self._embed(client, texts, usage), None)) if client else None
                retrieved = _retrieval.rank(x_t, bank_at_start, k, index, embed=embed)
                rec["retrieval"] = ("recomputed: the pipeline recorded no context_tool result (recompute_retrieval); "
                                    "the agent may not have read these reflections")
                provenance = " (recomputed)"
                unrecorded += 1
            else:
                # Eq. 2 revises the reflections the agent used; without a record, the agent is taken to have read none.
                retrieved = []
                rec["retrieval"] = ("none recorded: the pipeline recorded no context_tool result, so the agent is taken "
                                    "to have read no reflections; nothing revised, no demonstrations")
                provenance = " (none recorded)"
                unrecorded += 1
            retrieved = [item for item in retrieved if item.get("id") in by_id]
            rec["retrieved"] = [{"id": item["id"], "rank": item.get("rank"), "score": item.get("score")}
                                for item in retrieved]
            # 2. backward revision of each retrieved reflection, independently, with tau_t as feedback.
            rec["revisions"] = []
            if p.get("revise", True) and not raw_memory and retrieved:
                targets = [by_id[item["id"]] for item in retrieved]
                workers = max(1, min(len(targets), int(p.get("parallel", 5))))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(lambda r: self._revise(ctx, r, tau_t_shown, trace_chars, sampling),
                                            targets))
                for item, target, (text, meta) in zip(retrieved, targets, results):
                    entry = {"id": target.id, "rank": item.get("rank"), **meta}
                    if provenance:
                        entry["retrieval"] = provenance.strip(" ()")
                    if text and text.strip() and text.strip() != target.reflection.strip():
                        entry["indel_distance"] = indel_distance(target.reflection, text)
                        entry["chars_before"], entry["chars_after"] = len(target.reflection), len(text)
                        target.revisions.append({"step": n, "by": tid, "rank": item.get("rank"),
                                                 "indel_distance": entry["indel_distance"],
                                                 "chars_before": entry["chars_before"],
                                                 "chars_after": entry["chars_after"],
                                                 **({"retrieval": provenance.strip(" ()")} if provenance else {})})
                        target.reflection = text.strip()
                        _bank.save_reflection(adapter, target)
                    else:
                        entry["indel_distance"] = 0
                    rec["revisions"].append(entry)
            # 3. forward write: the instance's reflection, with the revised reflections as demonstrations.
            demonstrations = [by_id[item["id"]].reflection for item in retrieved] \
                if p.get("demonstrations", True) and not raw_memory else []
            if raw_memory:
                text, meta = tau_t_shown, {"calls": 0}
            else:
                text, meta = self._reflect(ctx, tau_t_shown, demonstrations, sampling)
            rec["write"] = meta
            if not text or not text.strip():
                rec["outcome"] = "no reflection written: " + str(meta.get("error") or "the agent returned none")
                instance_records.append(rec)
                lines.append(f"- {tid}: {rec['outcome']}")
                continue
            memory_id = tid if tid not in by_id else f"{tid}.s{n}"
            vector = self._embed(client, [x_t], usage)[0] if client else None
            new = _bank.Reflection(id=memory_id, task=tid, step=n, query=x_t, reflection=text.strip(),
                                   embedding=vector, model=client.model if client else None, created=_bank.now())
            _bank.save_reflection(adapter, new)
            reflections.append(new)
            by_id[new.id] = new
            (store / f"{_bank.safe_id(memory_id)}.txt").write_text(tau_t[:KEEP_CHARS])
            rec["memory_id"], rec["reflection_chars"] = memory_id, len(new.reflection)
            rec["outcome"] = (f"retrieved {len(retrieved)}{provenance}, revised "
                              f"{sum(1 for e in rec['revisions'] if e.get('indel_distance'))}, wrote {memory_id} "
                              f"({len(new.reflection)} chars)")
            instance_records.append(rec)
            lines.append(f"- {tid}: {rec['outcome']}")
            if vector is not None and index.get("dim") != len(vector):
                index["dim"] = len(vector)
                _bank.write_index(adapter, index)
        if ctx.cancel is not None and ctx.cancel.is_set():
            return stopped()
        if unrecorded:
            note = (note + " " if note else "") + (
                f"{unrecorded} instance(s) of this step recorded no context_tool result: the acting agent must call "
                "Session.context(query=..., task=...) and record session.last_context in RunOutput.extra['context_tool'] "
                "for the retrieved reflections to be revised (docs/methods/reharness.md)")
        state["steps"].append({"step": n, "instances": instance_records, "bank_size": len(reflections),
                               **({"note": note} if note else {})})
        self._save_state(ctx, state)
        report = [f"# ReHarness step {n}", "", f"Bank: {len(reflections)} reflection(s) after this step; k = {k}; "
                  f"retrieval = {index['retrieval']}" + (f" ({index.get('embedding_model')})"
                                                          if index.get("embedding_model") else "") + ".", ""]
        report += lines
        for rec in instance_records:
            for entry in rec.get("revisions") or []:
                report.append(f"  - revised {entry['id']} (rank {entry.get('rank')}): indel distance "
                              f"{entry.get('indel_distance', 0)}" + (f"; {entry['error']}" if entry.get("error") else ""))
        if note:
            report += ["", note]
        (ctx.workspace.output / "report.md").write_text("\n".join(report) + "\n")
        summary_line = "; ".join(r.get("outcome", "") for r in instance_records) or "no instance in this step"
        return StepResult("done", summary_line[:300], usage=usage,
                          extra={"bank_size": len(reflections), "instances": len(instance_records)})

    def finalize(self, ctx) -> FinalResult:
        reflections = _bank.load_bank(ctx.workspace.adapter)
        revised = sum(len(r.revisions) for r in reflections)
        return FinalResult(adapter=ctx.workspace.adapter,
                           summary=f"the harness state after the stream: {len(reflections)} reflection(s), "
                                   f"{revised} revision(s) in all")

    # ---------- the agents ----------
    def _chat(self, ctx, system: str, user: str, sampling: dict, key: str) -> tuple[str | None, dict]:
        """One agent call, the JSON field it was asked for; a second attempt when the reply is not that JSON (a
        reminder) or was cut off at max_tokens (the same request with twice the budget)."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        meta = {"calls": 0}
        last, first_cut = "", False
        for attempt in range(2):
            try:
                reply = ctx.tools["llm"](messages=messages, **sampling)
            except Exception as exc:                        # noqa: BLE001 — recorded; the instance goes on
                meta["error"] = f"{type(exc).__name__}: {exc}"[:300]
                return None, meta
            meta["calls"] += 1
            content = last = reply.get("content") or ""
            meta["finish_reason"] = reply.get("finish_reason")
            cut = reply.get("finish_reason") == "length"
            value, how = (None, None) if cut else extract_field(content, key)
            if value is not None:
                meta["parse"] = how if attempt == 0 else f"{how} after a {'larger budget' if first_cut else 'reminder'}"
                if "error" in meta:
                    meta["note"] = meta.pop("error")        # the first reply was cut off; the second attempt succeeded
                return value, meta
            if attempt == 0 and cut:
                meta["error"] = "the reply was cut off at max_tokens"
                first_cut = True
                sampling = {**sampling, "max_tokens": 2 * int(sampling.get("max_tokens") or 8192)}
                continue
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": f'Return only JSON exactly as {{"{key}":"..."}}.'}]
        # Neither reply was the JSON asked for: prose is kept rather than nothing; a JSON wrapper that did not parse or
        # a reply cut off at max_tokens is not a reflection.
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", _answer(last).strip())
        if not text or text.startswith("{") or meta.get("finish_reason") == "length":
            meta["parse"] = "none (no parseable JSON in two replies)"
            meta["error"] = meta.get("error") or "the reply was not the JSON asked for"
            return None, meta
        meta["parse"] = "raw text (no JSON in two replies)"
        return text, meta

    def _reflect(self, ctx, tau_t: str, demonstrations: list[str], sampling: dict) -> tuple[str | None, dict]:
        return self._chat(ctx, prompts.REFLECTION, prompts.reflection_input(tau_t, demonstrations), sampling,
                          "reflection")

    def _revise(self, ctx, target: _bank.Reflection, tau_t: str, trace_chars: int, sampling: dict):
        source_path = ctx.workspace.state / TRAJECTORIES / f"{_bank.safe_id(target.id)}.txt"
        source = source_path.read_text() if source_path.is_file() else \
            "(the source trajectory of this memory is not available; revise the conditions of use only)"
        user = prompts.revision_input(target.id, target.reflection, trajectory.clip_middle(source, trace_chars), tau_t)
        return self._chat(ctx, prompts.REVISION, user, sampling, "revised_reflection")

    # ---------- state ----------
    @staticmethod
    def _state(ctx) -> dict:
        path = ctx.workspace.state / STATE
        if path.is_file():
            return json.loads(path.read_text())
        return {"steps": [], "index": None}

    @staticmethod
    def _save_state(ctx, state: dict) -> None:
        (ctx.workspace.state / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=1))
