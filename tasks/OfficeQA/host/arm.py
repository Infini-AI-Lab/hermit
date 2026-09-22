"""Run one arm of the OfficeQA evaluation on this machine: stateless (no
adapter), prompt-only (an evolving ADAPTER.md) or mwm (the world model).

Per question: a fresh workspace, native codex through its own Fireworks broker
(the branch's relay: budgets, usage), one prediction. After every
`learn_every` questions the controller grades the batch with the official
scorer, writes the learner's evidence and runs one framework evolve session;
the next questions use the adopted candidate. The final grade is written when
every question has a prediction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from paths import REPOSITORY, DEPLOYMENT  # noqa: E402
sys.path.insert(0, str(REPOSITORY))
import acting  # noqa: E402
import broker  # noqa: E402
import rate_control  # noqa: E402
import campaign  # noqa: E402
import data  # noqa: E402
import records  # noqa: E402
from learn import Learner  # noqa: E402

ARMS = ("stateless", "prompt-only", "mwm")


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def host_context(adapter: Path, evidence_dir: Path) -> str:
    """The package the host reads: the adapter's context with the deployment's
    host texts and the run's evidence bound (what the CLBench hosts render)."""
    from harnessadapter.adapter.loader import Adapter
    from harnessadapter.adapter.resources import LearningMaterial
    host = json.loads((DEPLOYMENT / "host.json").read_text())
    resources = {"deployment": host}
    if (evidence_dir / "README.md").is_file():
        resources["evidence"] = LearningMaterial(evidence_dir)
    return Adapter(adapter, resources).context()


INFRA_RETRIES = 3   # fresh attempts a question gets when the transport or the provider fails it


def actor(key, run: Path, ordinal: int, task: dict, adapter: Path | None, evidence_dir: Path, corpus, index, timeout: int, max_tool_ops: int | None = None):
    """One question with insurance: an attempt that ended in infrastructure
    failure (5xx, stream cut, capacity refusal, gateway page) is archived
    as <ordinal>.infraN and the question is attempted again from scratch, up
    to INFRA_RETRIES times. Only then is it recorded as transport_lost — a
    status the grade lists separately and never counts as the model's 0."""
    base = run / "questions" / f"{ordinal:03d}"
    for attempt in range(1, INFRA_RETRIES + 1):
        summary = _attempt(key, run, ordinal, task, adapter, evidence_dir, corpus, index, timeout, max_tool_ops)
        if not summary.pop("_infra", False):
            return summary
        summary["status"] = "transport_lost" if attempt == INFRA_RETRIES else summary["status"]
        if attempt == INFRA_RETRIES:
            save(base / "summary.json", summary)
            return summary
        n = len(list(run.glob(f"questions/{ordinal:03d}.infra*"))) + 1
        base.rename(run / "questions" / f"{ordinal:03d}.infra{n}")
        print(json.dumps({"ordinal": ordinal, "infra_retry": attempt, "archived": f"{ordinal:03d}.infra{n}", "reason": summary["status"]}), flush=True)
    return summary


def _attempt(key, run: Path, ordinal: int, task: dict, adapter: Path | None, evidence_dir: Path, corpus, index, timeout: int, max_tool_ops: int | None = None):
    base = run / "questions" / f"{ordinal:03d}"
    workspace, out = base / "workspace", base / "records"
    if base.exists():
        raise ValueError(f"question {ordinal} was already attempted")
    base.mkdir(parents=True)
    context = host_context(adapter, evidence_dir) if adapter else None
    campaign.prepare_workspace(workspace, adapter, corpus=corpus, index=index, context=context,
                               evidence=evidence_dir if (evidence_dir / "README.md").is_file() else None,
                               deployment_host=DEPLOYMENT / "host.json", python_sandbox=True)
    save(base / "task.json", {"ordinal": ordinal, **task})
    revision = campaign.revision(adapter) if adapter else None
    # No per-question cap (user decision 2026-09-21): no cumulative output-token
    # budget; the wall-clock argument is an infrastructure guard, not a budget.
    if acting.LOGIN:
        # gpt-5.6-sol on the ChatGPT sign-in: codex talks to OpenAI itself, so
        # there is no broker and no per-request record; usage comes from the
        # CLI's own turn events (input/cached/output/reasoning tokens).
        result = acting.run_actor(workspace, out, campaign.prompt_for(task), corpus, index,
                                  adapter=adapter, timeout=timeout, endpoint=None, max_tool_ops=max_tool_ops)
        reported = result.get("usage") or {}
        transport = {"failed": False, "failure_kind": None, "calls": None, "usage_complete": bool(reported),
                     "usage": {"input_tokens": reported.get("input_tokens", 0), "output_tokens": reported.get("output_tokens", 0),
                               "cached_tokens": reported.get("cached_input_tokens", 0),
                               "reasoning_tokens": reported.get("reasoning_output_tokens", 0)}}
    else:
        # No broker-side limits (user decision 2026-09-22): every actor's
        # requests go straight out (no shared in-flight cap, no dispatch
        # interval), and one request may take as long as the question's own
        # guard allows. Only the 5xx retry rule remains.
        with broker.Broker(key, base / "api", max_completion=None, max_response=None, request_timeout=timeout,
                           scheduler=rate_control.RateScheduler(max_in_flight=1_000_000, min_interval=0.0)) as relay:
            # The guard excludes the transport's own failure time (insurance).
            result = acting.run_actor(workspace, out, campaign.prompt_for(task), corpus, index,
                                      adapter=adapter, timeout=timeout, endpoint=relay.endpoint, max_tool_ops=max_tool_ops,
                                      extra_time=relay.infra_seconds)
        transport = json.loads((base / "api/summary.json").read_text())
    final = out / "final.md"
    eligible = result["status"] == "completed" and not transport["failed"]
    answer = final.read_text().strip() if eligible and final.is_file() else ""
    save(base / "prediction.json", {"task_id": task["task_id"], "answer": answer})
    if adapter and revision != campaign.revision(adapter):
        raise ValueError("Actor mutated its read-only adapter")
    # A guard stop (timeout, request cap) is the question's outcome even though
    # it leaves a broker request pending; it is not a transport failure.
    status = (result["status"] if result["status"] in ("timeout", "request_cap", "tool_cap") else
              (transport["failure_kind"] or "transport_rejected") if transport["failed"] else result["status"])
    summary = {"ordinal": ordinal, "task_id": task["task_id"], "status": status, "answer": answer,
               "elapsed_seconds": result["elapsed_seconds"], "infra_seconds": result.get("infra_seconds", 0.0),
               "usage": transport["usage"],
               "requests": transport["calls"], "usage_complete": transport["usage_complete"],
               "adapter": adapter.name if adapter else None,
               "native_codex_items": result["completed_item_counts"]}
    save(base / "summary.json", summary)
    summary["_infra"] = acting.infra_failure(result, None if acting.LOGIN else transport) and status not in ("timeout", "request_cap", "tool_cap")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--prepared", type=Path, required=True, help="a data.py prepared root (contains prepared/)")
    parser.add_argument("--questions", type=int, default=50)
    parser.add_argument("--learn-every", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1, help="questions of one block run at once")
    parser.add_argument("--feedback", choices=("gold", "verdict", "none"), default="gold")
    parser.add_argument("--evolver-model", default="accounts/fireworks/models/glm-5p3-flash")
    parser.add_argument("--evolver-timeout", type=int, default=1800)
    parser.add_argument("--actor-timeout", type=int, default=1800, help="infrastructure guard per question, not a budget")
    parser.add_argument("--max-tool-ops", type=int, default=100, help="tool operations per question (commands, wm calls, file changes; 0 = unlimited); reaching it ends the question as tool_cap")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--resume", action="store_true",
                        help="continue an existing run: keep finished questions and learned blocks, redo questions that were in flight or lost to transport")
    args = parser.parse_args()
    cap = args.max_tool_ops if args.max_tool_ops > 0 else None

    key = args.key_file.read_text().strip()
    run = args.run_dir.resolve()
    if run.exists() and not args.resume:
        raise ValueError("Use a new run directory, or --resume")
    if args.resume and not (run / "state.json").is_file():
        raise ValueError("Nothing to resume here")
    run.mkdir(parents=True, exist_ok=True)
    prepared = data._prepared(args.prepared)
    tasks = data.load_tasks(args.prepared)[: args.questions]
    corpus, index = prepared / "agent/corpus", prepared / "agent/corpus.sqlite"
    answers = {r["task_id"]: r["answer"] for r in data._legacy.load_jsonl(prepared / "private/answers.jsonl")}
    evidence_dir = run / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    # The learner reads the same corpus and index the hosts search, at
    # /environment/corpus and /environment/corpus.sqlite (read-only).
    learner = Learner(run, args.arm, evidence_dir, model=args.evolver_model, timeout=args.evolver_timeout,
                      mounts={"corpus": corpus, "corpus.sqlite": index}) if args.arm != "stateless" else None
    settings = {"arm": args.arm, "actor": acting.ACTOR, "actor_model": acting.MODEL,
                "context_window": acting.FLASH_CONTEXT_WINDOW if not acting.LOGIN else "model-metadata",
                "evolver_model": args.evolver_model if args.arm != "stateless" else None,
                "learn_every": args.learn_every, "concurrency": args.concurrency, "feedback": args.feedback,
                "actor_timeout": args.actor_timeout, "max_tool_ops": cap, "prepared": str(prepared),
                "deployment": DEPLOYMENT.name, "task_ids_sha256": data._ids_hash([t["task_id"] for t in tasks])}
    if args.resume:
        state = json.loads((run / "state.json").read_text())
        # A resumed run must be the same experiment: the same question at every
        # kept ordinal, and the same settings (the question count may grow).
        by_ordinal = {o: t for o, t in enumerate(tasks, start=1)}
        for r in state["results"]:
            expected = by_ordinal.get(r["ordinal"], {}).get("task_id")
            if expected != r["task_id"]:
                raise ValueError(f"resume refused: ordinal {r['ordinal']} is {expected!r} in the prepared root but {r['task_id']!r} in the run")
        previous = state.get("settings")
        changed = {k: (previous.get(k), v) for k, v in settings.items() if k != "task_ids_sha256" and previous.get(k) != v} if previous else {}
        if previous and previous.get("task_ids_sha256") != settings["task_ids_sha256"]:
            # the prepared root may be a longer set with the same prefix (checked above per ordinal)
            changed.pop("task_ids_sha256", None)
        if changed and not os.environ.get("MW_RESUME_FORCE"):
            raise ValueError("resume refused: settings differ from the run's (" + ", ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changed.items()) + "); set MW_RESUME_FORCE=1 to continue anyway")
        # Final outputs of the earlier stretch are history until the extended run regrades.
        if state.get("grade") or state.get("finished_utc"):
            state.setdefault("grade_history", []).append({"finished_utc": state.get("finished_utc"), "questions": state.get("questions"), "grade": state.get("grade")})
            state.pop("grade", None); state.pop("finished_utc", None)
        state["questions"] = len(tasks)
        state["settings"] = settings
        learned = {int(l["block"]) for l in state.get("learning", []) if l.get("block")}
        final_blocks = set()          # questions of a learned block are final: the learner already read them
        if learner:
            for r in state["results"]:
                if (r["ordinal"] - 1) // args.learn_every + 1 in learned:
                    final_blocks.add(r["ordinal"])
        # A final answer the CLI wrote but the old event-only check refused
        # (invalid_final) is re-validated against the broker's recorded last
        # response; the attempt itself is not repeated.
        for r in state["results"]:
            if r["status"] != "invalid_final":
                continue
            q = run / "questions" / f"{r['ordinal']:03d}"
            try:
                responses = sorted((q / "api").glob("[0-9]*/response.json"))
                last = json.loads(responses[-1].read_text()) if responses else {}
                texts = ["".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                         for it in last.get("output", []) if isinstance(it, dict) and it.get("type") == "message"]
                # Login mode (no broker): a completed turn's CLI-written final stands on its own.
                prior = json.loads((q / "records/result.json").read_text())
                accept_any = acting.LOGIN and not responses and bool(prior.get("turn_completed")) and not prior.get("final_message_event")
                check = acting.collect_final(q / "workspace", q / "records", [t for t in texts if t], accept_any=accept_any)
            except (OSError, ValueError):
                check = {"valid": False}
            if check.get("valid"):
                answer = (q / "records/final.md").read_text().strip()
                task_id = r["task_id"]
                r.update(status="completed", answer=answer, revalidated=utc(),
                         feedback=records.feedback(answers[task_id], answer, args.feedback))
                save(q / "prediction.json", {"task_id": task_id, "answer": answer})
                print(json.dumps({"revalidated": r["ordinal"], "answer": answer[:60], "verdict": r["feedback"].get("verdict")}), flush=True)
        keep, redo = [], []
        def killed(o):
            f = run / "questions" / f"{o:03d}" / "records/result.json"
            try:
                return json.loads(f.read_text()).get("exit_code") in (-9, -15)
            except (OSError, ValueError):
                return False
        for r in state["results"]:
            # Guard stops are final: the question had its 30 minutes / its
            # request cap. Only transport losses and restart kills are redone.
            if r["ordinal"] in final_blocks or r["status"] in ("timeout", "request_cap", "tool_cap") or (r["status"] not in ("transport_rejected", "transport_lost") and not killed(r["ordinal"])):
                keep.append(r)
            else:
                redo.append(r["ordinal"])
        # Questions that were in flight when the run stopped have a directory but no result.
        for q in sorted(run.glob("questions/[0-9][0-9][0-9]")):
            o = int(q.name)
            if o not in {r["ordinal"] for r in keep} and o not in redo:
                redo.append(o)
        for o in sorted(set(redo)):
            q = run / "questions" / f"{o:03d}"
            if q.exists():
                n = len(list(run.glob(f"questions/{o:03d}.retry*"))) + 1
                q.rename(run / "questions" / f"{o:03d}.retry{n}")
        state["results"] = sorted(keep, key=lambda r: r["ordinal"])
        state.setdefault("resumes", []).append({"utc": utc(), "redo": sorted(set(redo)), "kept": len(keep),
                                               "settings_changed": changed or None, "questions": len(tasks)})
        save(run / "state.json", state)
        print(json.dumps({"resume": True, "kept": len(keep), "redo": sorted(set(redo)), "learned_blocks": sorted(learned)}), flush=True)
    else:
        state = {"arm": args.arm, "started_utc": utc(), "questions": len(tasks), "learn_every": args.learn_every,
                 "feedback": args.feedback, "framework": campaign.framework_revision(), "settings": settings,
                 "results": [], "learning": []}
        save(run / "state.json", state)
    if learner:
        records.write_readme(evidence_dir)
        if not (run / "bootstrap.json").exists():
            state["learning"].append(learner.bootstrap()); save(run / "state.json", state)
    done_ordinals = {r["ordinal"] for r in state["results"]}
    learned_blocks = {int(l["block"]) for l in state.get("learning", []) if l.get("block")}
    # The questions of one block (learn_every of them) run concurrently
    # against the same adapter, up to --concurrency at a time; the learner
    # runs after the whole block, so no question sees a candidate built from
    # a question of its own block.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    numbered = list(enumerate(tasks, start=1))
    if learner is None:
        # No learning, so no blocks: a sliding window of --concurrency
        # questions in flight; a slow question never holds the others.
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            futures = {pool.submit(actor, key, run, o, t, None, evidence_dir, corpus, index, args.actor_timeout, cap): (o, t) for o, t in numbered if o not in done_ordinals}
            for future in as_completed(futures):
                ordinal, task = futures[future]
                summary = future.result()
                summary["feedback"] = records.feedback(answers[task["task_id"]], summary["answer"], args.feedback)
                state["results"].append(summary); state["results"].sort(key=lambda r: r["ordinal"]); save(run / "state.json", state)
                print(json.dumps({"ordinal": ordinal, "status": summary["status"], "verdict": summary["feedback"].get("verdict"),
                                  "requests": summary["requests"]}), flush=True)
        numbered = []
    for start in range(0, len(numbered), args.learn_every):
        chunk = numbered[start:start + args.learn_every]
        block = start // args.learn_every + 1
        if block in learned_blocks:
            continue                      # resumed: this block and its learning are final
        adapter = learner.adapter if learner else None
        pending = [item for item in chunk if item[0] not in done_ordinals]
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            fresh = dict(zip([o for o, _ in pending], pool.map(lambda item: actor(key, run, item[0], item[1], adapter, evidence_dir, corpus, index, args.actor_timeout, cap), pending)))
        kept = {r["ordinal"]: r for r in state["results"]}
        batch = []
        for ordinal, task in chunk:
            if ordinal in fresh:
                summary = fresh[ordinal]
                summary["feedback"] = records.feedback(answers[task["task_id"]], summary["answer"], args.feedback)
                state["results"].append(summary)
                print(json.dumps({"ordinal": ordinal, "status": summary["status"], "verdict": summary["feedback"].get("verdict"),
                                  "requests": summary["requests"]}), flush=True)
            else:
                summary = kept[ordinal]
            batch.append({"ordinal": ordinal, "task": task, "prediction": {"task_id": task["task_id"], "answer": summary["answer"]},
                          "summary": summary, "events": run / "questions" / f"{ordinal:03d}" / "records/events.jsonl",
                          "feedback": summary["feedback"]})
        state["results"].sort(key=lambda r: r["ordinal"])
        save(run / "state.json", state)
        if learner:
            listing = records.render(block, batch, evidence_dir)
            goal = f"This block covers questions {batch[0]['ordinal']}-{batch[-1]['ordinal']}; the current tick is {batch[-1]['ordinal']}.\n\n{listing}"
            entry = learner.session(block, batch[-1]["ordinal"], goal)
            state["learning"].append(entry); save(run / "state.json", state)
            print(json.dumps({"learning_block": block, "status": entry.get("status"), "adopted": entry.get("adopted")}), flush=True)
    predictions = run / "predictions.jsonl"
    predictions.write_text("".join(json.dumps({"task_id": r["task_id"], "answer": r["answer"]}) + "\n" for r in state["results"]))
    scorer = records.grader()
    lost = [r["ordinal"] for r in state["results"] if r["status"] == "transport_lost"]
    rows = [{"task_id": r["task_id"], "strict": int(scorer(answers[r["task_id"]], r["answer"], tolerance=0.0)),
             "one_percent": int(scorer(answers[r["task_id"]], r["answer"], tolerance=0.01))}
            for r in state["results"] if r["status"] != "transport_lost"]
    n = len(rows)
    state.update(finished_utc=utc(), grade={"questions": n, "transport_lost": lost, "strict_correct": sum(r["strict"] for r in rows),
                                             "strict_accuracy": sum(r["strict"] for r in rows) / n,
                                             "one_percent_accuracy": sum(r["one_percent"] for r in rows) / n,
                                             "results": rows})
    save(run / "state.json", state)
    print(f"Score: {state['grade']['strict_accuracy']:.4f} (strict, {n} questions); 1%: {state['grade']['one_percent_accuracy']:.4f}"
          + (f"; transport_lost: {len(lost)} ({lost})" if lost else ""), flush=True)


if __name__ == "__main__":
    main()
