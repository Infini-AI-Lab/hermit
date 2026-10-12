"""Evidence rendering for the CL-Bench database_exploration venue.

The venue-specific surface is deliberately thin: per question it renders the
RESULT (what was asked, what was answered, what the grader said) and the
ADDRESS of the raw record, nothing else. The query-by-query transcript stays
on disk under /evidence/blockNNN/ for the meta-agent to open when an episode
warrants it — the opposite trade from the coding venue, where four long
episodes go in the prompt whole. Two facts drove the split: the official
harness repeats the full question text on every turn, so inlining raw records
would spend most of the prompt on duplicates; and the graded feedback here is
one line that carries the decisive information (the revealed correct answer)
on its own.

The revealed answer goes in the basic info, not the raw file. The official
ICL baseline keeps every FEEDBACK line — including `Correct answer: ...` —
in its context forever, so leaving it out would make the comparison one of
information access rather than of organisation.

Input is the official harness's trace format: run_XXXX.json["interactions"],
each with query.metadata.question_num / question_id, response.action
{"action": QUERY|ANSWER, "content": ...}, and observation.content.
"""

import collections
import json
import os
import re

from . import record as _record

# The task's scoring (CLB_SCORING): "accuracy" grades a correct answer 1
# whatever it cost; the legacy default is regret, 1 - queries/budget. The
# reward lines below are only true under regret; under accuracy they told
# the learner "each query spends 1/15 of that question's score" (measured
# in the 2026-09-03 flash v1.2 prompts), so they render only when the
# scoring is regret AND a budget exists.
def _regret_scoring() -> bool:
    return os.environ.get("CLB_SCORING", "regret").lower() != "accuracy"


# The question text arrives wrapped in per-turn boilerplate ("Question 1/5",
# the database description, the action menu). Everything from the first
# boilerplate marker on is template, not question.
_BOILERPLATE = re.compile(
    r"\n\s*(You have access to a SQLite database|You may run up to|Actions:)")
_QHEADER = re.compile(r"^Question \d+/\d+\s*\n", re.M)

_CORRECT = re.compile(r"^Question \d+: CORRECT!", re.M)
_INCORRECT = re.compile(r"^Question \d+: INCORRECT\.", re.M)
_TIMEOUT = re.compile(r"^Question \d+: TIMED OUT", re.M)
# The task grades an over-budget question as incorrect but phrases the
# feedback differently ("Query budget exceeded — treated as incorrect"),
# matching none of the above — so it fell through to UNGRADED and the
# narrowing agenda, which only reproduces INCORRECT, skipped it. Never
# triggered in the V6/V7 campaigns (max queries_used == 15, nobody went
# over), so no reported number is affected; folded into INCORRECT here so
# the next one is graded rather than silently dropped.
_BUDGET = re.compile(r"^Question \d+: Query budget exceeded", re.M)
_REVEALED = re.compile(r"^(?:Correct answer|The correct answer was): (.*)$",
                       re.M)

# Harness metadata keys the learner must never read (2026-09-06).
HIDDEN_METADATA = ("db_path", "difficulty")

# Table names as they appear after FROM/JOIN in the host's own queries.
_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
                    re.IGNORECASE)


def question_text(prompt: str) -> str:
    """The question alone, header and boilerplate stripped."""
    text = _QHEADER.sub("", prompt, count=1)
    m = _BOILERPLATE.search(text)
    if m:
        text = text[:m.start()]
    return text.strip()


def group_questions(interactions: list[dict]) -> list[dict]:
    """One dict per question, in play order, from a trace's interaction list."""
    by_q: dict = collections.OrderedDict()
    for it in interactions:
        meta = (it.get("query") or {}).get("metadata") or {}
        key = (meta.get("question_num"), meta.get("question_id"))
        by_q.setdefault(key, []).append(it)

    out = []
    for (qnum, qid), turns in by_q.items():
        # Channel turns (RECALL/READ/SEARCH/RUN against the world model) are
        # interleaved with the question's own turns and must not be read as
        # part of the question's play: the last turn decides the verdict, and
        # a channel turn landing there would leave every such question
        # UNGRADED. They are separated here rather than dropped — dropping
        # them upstream is how the learner came to have no record at all of
        # what its own world model was asked for or what came back.
        chan = [t for t in turns if t.get("channel")]
        main = [t for t in turns if not t.get("channel")] or turns
        first, last = main[0], main[-1]
        acts = [(t.get("response") or {}).get("action") or {} for t in main]
        queries = [a.get("content", "") for a in acts
                   if a.get("action") == "QUERY"]
        answer = next((a.get("content", "") for a in reversed(acts)
                       if a.get("action") == "ANSWER"), None)
        fb = ((last.get("observation") or {}).get("content")) or ""
        if _CORRECT.search(fb):
            verdict = "CORRECT"
        elif _TIMEOUT.search(fb):
            verdict = "TIMEOUT"
        elif _INCORRECT.search(fb) or _BUDGET.search(fb):
            verdict = "INCORRECT"
        else:
            verdict = "UNGRADED"          # ran out of turns with no feedback
        m = _REVEALED.search(fb)
        meta = (first.get("query") or {}).get("metadata") or {}
        tables = set()
        for q in queries:
            tables.update(t.lower() for t in _TABLE.findall(q))
        out.append({
            "question_num": qnum,
            "question_id": qid,
            "question": question_text(
                (first.get("query") or {}).get("prompt") or ""),
            "budget": meta.get("query_budget"),
            "queries_used": len(queries),
            "final_answer": answer,
            "verdict": verdict,
            "revealed_answer": m.group(1).strip() if m else None,
            "tables": sorted(tables),
            "channel": [_channel_use(t) for t in chan],
            "turns": turns,               # raw, for the on-disk record
        })
    return out


def _clip_turn(t: dict) -> None:
    """Cap the observation of a raw turn at record.OBS_CAP (in place)."""
    obs = t.get("observation")
    if isinstance(obs, dict) and isinstance(obs.get("content"), str):
        obs["content"] = _record.clip_obs(obs["content"])


def _channel_use(turn: dict) -> dict:
    """One wm call: the name and arguments the host passed, and what
    came back."""
    a = (turn.get("response") or {}).get("action") or {}
    result = ((turn.get("observation") or {}).get("content")) or ""
    return {"kind": "wm " + str(a.get("name") or a.get("action") or ""),
            "content": (json.dumps(a["args"]) if isinstance(a.get("args"), dict)
                        else str(a.get("content") or "")).strip(),
            "result": result.strip()}


def write_raw(questions: list[dict], evidence_dir: str, block: int) -> dict:
    """The complete turn records, one file per question; returns qnum->path.

    Paths are returned as the meta-agent will see them (/evidence/...), the
    files land under evidence_dir which the harness mounts at that address.
    """
    bdir = os.path.join(evidence_dir, f"block{block:03d}")
    os.makedirs(bdir, exist_ok=True)
    paths = {}
    for q in questions:
        name = f"q{q['question_num']:03d}_id{q['question_id']}.json"
        # Evaluator/harness-only metadata never reaches the record, whatever
        # the caller filtered: db_path names a host location, difficulty is
        # the dataset authors' label (no host prompt carries either).
        turns = json.loads(json.dumps(q["turns"]))
        for t in turns:
            meta = (t.get("query") or {}).get("metadata")
            if isinstance(meta, dict):
                for k in HIDDEN_METADATA:
                    meta.pop(k, None)
            _clip_turn(t)
        with open(os.path.join(bdir, name), "w") as f:
            json.dump(turns, f, indent=1)
        paths[q["question_num"]] = f"/evidence/block{block:03d}/{name}"
    return paths


def layer1(q: dict, raw_path: str) -> str:
    """One question: the result and the address, no transcript."""
    if not _regret_scoring():
        reward = ""
    elif q["verdict"] == "CORRECT" and q.get("budget"):
        reward = (f"   reward {1 - q['queries_used']/q['budget']:.2f} "
                  f"(= 1 - {q['queries_used']}/{q['budget']})")
    else:
        reward = "   reward 0.00" if q["verdict"] != "CORRECT" else ""
    lines = [
        f"## Question {q['question_num']}: id {q['question_id']}",
        f"question: {q['question']}",
        f"verdict: {q['verdict']}   queries used: {q['queries_used']}"
        + (f"/{q['budget']}" if q.get("budget") else "") + reward,
        f"final answer: {q['final_answer']}"
        if q["final_answer"] is not None else "final answer: (never answered)",
    ]
    if q["revealed_answer"] is not None:
        lines.append(f"revealed correct answer: {q['revealed_answer']}")
    # What the host asked the world model for on this question, and what came
    # back. Without it the learner can count that its model was consulted but
    # cannot tell a hit from a miss, which is the difference agenda item 6
    # asks it to act on. Results are capped: the full text is in the raw
    # trajectory, which the next line addresses.
    for c in q.get("channel") or []:
        lines.append(f"world-model {c['kind']}: {c['content']}")
        if c["result"]:
            r = c["result"].replace("\n", " ")
            lines.append("   -> " + (r[:300] + " ..." if len(r) > 300 else r))
    lines.append(f"raw trajectory: {raw_path}")
    return "\n".join(lines)


def render(questions: list[dict], evidence_dir: str, block: int) -> str:
    """The full evidence section for one block: one entry per question,
    each with its record address; no block-level statistics."""
    from . import evidence_readme
    evidence_readme.write(evidence_dir, "clbench")
    paths = write_raw(questions, evidence_dir, block)
    # Part 3.3 (2026-09-06, user rule): no wrapper-computed statistics —
    # the panel lists each episode's facts and its record address; the
    # learner computes whatever aggregate it wants from the record.
    body = ["=" * 70]
    for q in questions:
        body += [layer1(q, paths[q["question_num"]]), "=" * 70]
    return "\n".join(body)
