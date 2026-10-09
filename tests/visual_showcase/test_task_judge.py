"""judge.py: mechanical items on real old capture summaries (task_fixtures/: harness/waves and direct/lava of the
2026-09-19 runs, with the old machine's vendor paths written as /old-experiment/) and the vision rubric's request and
JSON parsing with a fake client. No network."""
import base64
import copy
import json
from pathlib import Path
import re
import shutil
import sys

import pytest

from harnessadapter.evolver.base import Usage
from harnessadapter.task import RunOutput, normalise_judge

TASK = Path(__file__).resolve().parents[2] / "examples" / "tasks" / "visual_showcase"
FIXTURES = Path(__file__).resolve().parent / "task_fixtures"
sys.path.insert(0, str(TASK))
import judge  # noqa: E402
import prepare  # noqa: E402

TASKS = {json.loads(l)["id"]: json.loads(l) for l in
         prepare.generated(prepare.check_inputs())["tasks.jsonl"].splitlines()}       # the rows prepare.py writes
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")


def run_dir(tmp_path: Path, fixture: str, pngs: bool = True) -> Path:
    log = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, log)
    if pngs:
        for name in judge.SCREENSHOTS:
            (log / "capture" / f"{name}.png").write_bytes(PNG + name.encode())
    return log


def output(log: Path) -> RunOutput:
    return RunOutput(output={"case": "waves", "arm": "harness", "status": "x", "log_dir": str(log)}, log=log)


def reply_json(scores=(4, 3, 5, 2, 4), wrap=None) -> str:
    text = json.dumps({"dimensions": [{"dimension": i, "score": s, "screenshot": "desktop-t2",
                                       "reason": f"Synthetic reason {i}."} for i, s in enumerate(scores, 1)]})
    return wrap.format(text) if wrap else text


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.total = Usage(judge.MODEL, cost_usd=0.0)

    def chat(self, messages, **options):
        self.requests.append({"messages": copy.deepcopy(messages), "options": options})
        item = self.replies[len(self.requests) - 1]
        if isinstance(item, Exception):
            raise item
        t = self.total
        t.input_tokens += 5000; t.output_tokens += 300; t.calls += 1; t.cost_usd += 0.001
        return {"role": "assistant", "content": item, "reasoning_content": "SYNTHETIC_PRIVATE_FIELD",
                "finish_reason": "stop"}, Usage(judge.MODEL, 5000, 0, 300, 1, 0.001)


# ---------- mechanical ----------

def items_of(fixture: str) -> dict:
    summary = json.loads((FIXTURES / fixture / "capture" / "summary.json").read_text())
    result = json.loads((FIXTURES / fixture / "result.json").read_text())
    items = judge.mechanical(summary, result)
    assert [i["item"] for i in items] == list(judge.MECHANICAL)
    return {i["item"]: i for i in items}


def test_mechanical_items_on_a_working_old_run():
    items = items_of("harness-waves")
    assert all(i["passed"] for i in items.values()), [i for i in items.values() if not i["passed"]]
    assert "0.02937" in items["motion_t0_t2"]["detail"] and "resetCount 0 before reset, 1 after" in \
        items["reset_increments"]["detail"]


def test_mechanical_items_on_the_old_shader_failure():
    items = items_of("direct-lava")
    failed = sorted(name for name, i in items.items() if not i["passed"])
    # The page never rendered: it stays frozen, but its pause button never reports aria-pressed="true".
    assert failed == ["intensity_075", "motion_t0_t2", "motion_t2_t5", "no_page_errors", "pause_holds", "ready",
                      "reset_increments"]
    assert "dimension mismatch" in items["no_page_errors"]["detail"]


def test_mechanical_items_without_a_capture():
    items = {i["item"]: i for i in judge.mechanical({}, {"valid_submission": True})}
    assert [n for n, i in items.items() if i["passed"]] == ["valid_submission"]
    assert items["no_page_errors"]["detail"] == "the page did not load in the capture"


def test_mechanical_mode_scores_without_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_JUDGE", "mechanical")
    monkeypatch.setattr(judge, "make_client", lambda: pytest.fail("no model call in mechanical mode"))
    log = run_dir(tmp_path, "direct-lava", pngs=False)
    verdict = normalise_judge(judge.score(TASKS["lava"], output(log)))
    assert verdict.score == pytest.approx(5 / 12) and verdict.feedback == ""
    assert set(verdict.extra) == {"mechanical", "mechanical_score", "rubric", "rubric_score", "judge_usage",
                                  "judge_cost_usd", "judge_model"}
    assert verdict.extra["rubric_score"] is None and verdict.extra["judge_cost_usd"] is None
    written = json.loads((log / "judge" / "judge.json").read_text())
    assert written["mode"] == "mechanical" and written["score"] == written["mechanical_score"] == pytest.approx(5 / 12)


# ---------- rubric parsing ----------

@pytest.mark.parametrize("wrap", [None, "```json\n{}\n```", "Here is my review:\n{}\nThanks.", "```\n{}\n```  "])
def test_parse_rubric_accepts_json_with_fences_or_prose(wrap):
    dims = judge.parse_rubric(reply_json(wrap=wrap))
    assert [d["score"] for d in dims] == [4, 3, 5, 2, 4] and [d["dimension"] for d in dims] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("before", ["Ratings for dimensions [1]-[5]:\n", "```\n[1, 2]\n```\nScores {\"scale\": 5}:\n",
                                    '{"dimensions": "below"} then '])
def test_parse_rubric_skips_other_json_before_the_rubric(before):
    """Other JSON before the rubric (a "[1]" in prose, a fenced list, an object without a list) is not the rubric."""
    if before.startswith('{"dimensions"'):
        with pytest.raises(ValueError, match="exactly 5"):        # an object with `dimensions` is the rubric
            judge.parse_rubric(before + reply_json())
        return
    dims = judge.parse_rubric(before + reply_json(wrap="```json\n{}\n```"))
    assert [d["score"] for d in dims] == [4, 3, 5, 2, 4]


def test_parse_rubric_accepts_a_bare_list_and_integral_floats():
    entries = json.loads(reply_json())["dimensions"]
    for e in entries:
        e["score"] = float(e["score"])
        del e["dimension"]
    assert [d["score"] for d in judge.parse_rubric(json.dumps(entries))] == [4, 3, 5, 2, 4]


@pytest.mark.parametrize("change", ["four", "score6", "score0", "bool", "half", "string", "shot", "reason", "order",
                                    "text"])
def test_parse_rubric_rejects_unusable_replies(change):
    value = json.loads(reply_json())
    dims = value["dimensions"]
    if change == "four":
        dims.pop()
    elif change == "score6":
        dims[0]["score"] = 6
    elif change == "score0":
        dims[0]["score"] = 0
    elif change == "bool":
        dims[0]["score"] = True
    elif change == "half":
        dims[0]["score"] = 3.5
    elif change == "string":
        dims[0]["score"] = "3"
    elif change == "shot":
        dims[0]["screenshot"] = "diagnostic-paused"
    elif change == "reason":
        dims[0]["reason"] = " "
    elif change == "order":
        dims[0]["dimension"], dims[1]["dimension"] = 2, 1
    text = "I cannot review this." if change == "text" else json.dumps(value)
    with pytest.raises(ValueError):
        judge.parse_rubric(text)


# ---------- the rubric request ----------

def test_rubric_request_retry_and_score(tmp_path, monkeypatch):
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    client = FakeClient(["Sorry, here it is: {not json", reply_json(wrap="```json\n{}\n```")])
    monkeypatch.setattr(judge, "make_client", lambda: client)
    log = run_dir(tmp_path, "harness-waves")
    task = TASKS["waves-eval"]
    verdict = normalise_judge(judge.score(task, output(log)))
    expected = ((4 - 1) + (3 - 1) + (5 - 1) + (2 - 1) + (4 - 1)) / 4 / 5
    assert verdict.score == pytest.approx(expected) == verdict.extra["rubric_score"]
    assert verdict.feedback == "" and verdict.extra["mechanical_score"] == 1.0
    assert verdict.extra["judge_usage"] == {"input_tokens": 10000, "cached_input_tokens": 0, "output_tokens": 600,
                                            "calls": 2}
    assert verdict.extra["judge_cost_usd"] == pytest.approx(0.002) and verdict.extra["judge_model"] == judge.MODEL
    first, second = client.requests
    assert first["options"] == {"max_tokens": 32768, **judge.SAMPLING}
    system, user = first["messages"]
    assert system == {"role": "system", "content": judge.RUBRIC_SYSTEM}
    texts = [p["text"] for p in user["content"] if p["type"] == "text"]
    images = [p for p in user["content"] if p["type"] == "image_url"]
    assert len(images) == 5 and texts[-1] == judge.RUBRIC_FORMAT
    assert [t.split(":")[0] for t in texts[1:6]] == [f"Screenshot {n}" for n in judge.SCREENSHOTS]
    assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == PNG + b"desktop-t0"
    paragraphs = task["prompt"].split("\n\n")
    intro = texts[0]
    assert intro.startswith("Brief: Ocean waves\n\n" + paragraphs[1] + "\n\n" + paragraphs[2] + "\n\n")
    assert paragraphs[3] not in intro and paragraphs[8] not in intro
    for n, d in enumerate(task["rubric"], 1):
        assert f"{n}. {d['observation_en']}" in intro
    for item in judge.MECHANICAL:
        assert f"- {item}: pass - " in intro
    request_text = json.dumps(first["messages"])
    assert not re.search(r"\b(arm|adapter|adapted|round|harness|direct|learned)\b", request_text.replace(
        base64.b64encode(PNG).decode(), ""), re.I)
    assert second["messages"][:2] == first["messages"]
    assert second["messages"][2] == {"role": "assistant", "content": "Sorry, here it is: {not json"}
    assert second["messages"][3]["content"].startswith("That reply could not be used (")
    written = json.loads((log / "judge" / "judge.json").read_text())
    assert written["rubric"]["dimensions"][0] == {"dimension": 1, "score": 4, "screenshot": "desktop-t2",
                                                  "reason": "Synthetic reason 1."}
    assert "SYNTHETIC_PRIVATE_FIELD" not in json.dumps(written) and written["task_id"] == "waves-eval"


def test_rubric_twice_invalid_or_transport_error_gives_null(tmp_path, monkeypatch):
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    for replies, calls in ((["no", "still no"], 2), ([RuntimeError("chat/completions HTTP 500: down")], 1)):
        client = FakeClient(replies)
        monkeypatch.setattr(judge, "make_client", lambda: client)
        log = run_dir(tmp_path / str(calls), "harness-waves")
        verdict = normalise_judge(judge.score(TASKS["waves"], output(log)))
        assert verdict.score == 0.0 and verdict.extra["rubric_score"] is None
        assert len(client.requests) == calls and verdict.extra["rubric"]["errors"]
        assert verdict.extra["mechanical_score"] == 1.0


def test_a_transport_error_is_recorded_without_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "SYNTH-JUDGE-KEY-24ce")
    client = FakeClient([RuntimeError("chat/completions HTTP 401: echo SYNTH-JUDGE-KEY-24ce")])
    monkeypatch.setattr(judge, "make_client", lambda: client)
    log = run_dir(tmp_path, "harness-waves")
    verdict = normalise_judge(judge.score(TASKS["waves"], output(log)))
    assert verdict.extra["rubric"]["errors"] == ["RuntimeError: chat/completions HTTP 401: echo [REDACTED]"]
    assert "SYNTH-JUDGE-KEY-24ce" not in (log / "judge" / "judge.json").read_text()


def test_rescore_a_run_log_keeps_the_previous_judgement(tmp_path, monkeypatch):
    """`python judge.py <log>`: a run whose rubric failed is scored again from its saved capture and case.json."""
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    log = run_dir(tmp_path, "harness-waves")
    (log / "case.json").write_text(json.dumps(TASKS["waves-eval"]))
    monkeypatch.setattr(judge, "make_client", lambda: FakeClient([RuntimeError("chat/completions HTTP 503: busy")]))
    first = judge.score(TASKS["waves-eval"], output(log))
    assert first["rubric_score"] is None and first["score"] == 0.0
    monkeypatch.setattr(judge, "make_client", lambda: FakeClient([reply_json()]))
    again = judge.rescore(log)
    assert again["rubric_score"] == pytest.approx(0.65) == again["score"]
    kept = sorted(p.name for p in (log / "judge").iterdir())
    assert len(kept) == 2 and kept[1] == "judge.json" and re.fullmatch(r"judge-\d{8}T\d{12}Z\.json", kept[0])
    assert json.loads((log / "judge" / kept[0]).read_text())["rubric"]["errors"] == ["RuntimeError: chat/completions "
                                                                                     "HTTP 503: busy"]
    written = json.loads((log / "judge" / "judge.json").read_text())
    assert (written["task_id"], written["rubric_score"]) == ("waves-eval", pytest.approx(0.65))


def test_no_valid_submission_skips_the_model(tmp_path, monkeypatch):
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    monkeypatch.setattr(judge, "make_client", lambda: pytest.fail("nothing to review"))
    log = run_dir(tmp_path, "harness-waves")
    result = json.loads((log / "result.json").read_text())
    result["valid_submission"] = False
    (log / "result.json").write_text(json.dumps(result))
    verdict = normalise_judge(judge.score(TASKS["waves"], output(log)))
    assert verdict.score == 0.0 and "skipped" in verdict.extra["rubric"]


def test_missing_screenshots_are_not_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    client = FakeClient([reply_json()])
    monkeypatch.setattr(judge, "make_client", lambda: client)
    log = run_dir(tmp_path, "harness-waves")
    (log / "capture" / "phone.png").unlink()
    (log / "capture" / "desktop-t5.png").unlink()
    (log / "capture" / "desktop-t5.png").symlink_to(log / "capture" / "desktop-t0.png")
    verdict = normalise_judge(judge.score(TASKS["waves"], output(log)))
    sent = verdict.extra["rubric"]["images"]
    assert [s["screenshot"] for s in sent if s.get("missing")] == ["desktop-t5", "phone"]
    assert sum(p["type"] == "image_url" for p in client.requests[0]["messages"][1]["content"]) == 3
