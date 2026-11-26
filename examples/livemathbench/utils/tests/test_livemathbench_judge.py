"""LiveMathBench's answer hook (utils/agent/answer.py), judge (utils/benchmark/judge.py) and metrics (utils/gpassk.py).
The hook takes the answer as OpenCompass's math_postprocess_v2 does (the last box, else the "final answer" sentence).
The judge is called as the run calls it, `score(task row, output)`, with a stand-in chat client at the model's edge
(`chat(messages, **kwargs) -> (message, usage)`, the shape hermit.agent.model.llm.ChatClient returns): the judge model
gets OpenCompass's judge prompt at temperature 0 with 8,192 tokens, and its last `\\boxed{yes}` is the score; no
answer is no model call, and a model call that fails raises (a failed judgement, not the agent's 0); its real client
is the default reference's, built without a call. G-Pass@k is checked against counts worked by hand, its thresholds
as integers past k = 24."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermit.agent.model.usage import Usage

UTILS = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Model:
    """A judge model that answers with `reply` and keeps what it was sent."""

    def __init__(self, reply="", error=None):
        self.reply, self.error, self.calls = reply, error, []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return {"role": "assistant", "content": self.reply}, Usage(model="judge", input_tokens=50, output_tokens=7)


@pytest.fixture
def judge(tmp_path, monkeypatch):
    module = load(UTILS / "benchmark" / "judge.py", "livemathbench_judge")
    (tmp_path / "answers.jsonl").write_text(json.dumps(
        {"id": "q1", "answer": "\\frac{1}{2}", "question": "What is 1/2?", "question_type": "Problem-Solving"}) + "\n")
    monkeypatch.setattr(module, "PRIVATE", tmp_path)
    return module


HOOK = load(UTILS / "agent" / "answer.py", "livemathbench_answer")


def output(text):
    return SimpleNamespace(output=HOOK.answer(text, {}))


@pytest.mark.parametrize("reply, answer", [
    ("so \\boxed{0.5}", "0.5"),
    ("so \\boxed{{1,2}}", "1,2"),                                     # one pair of outer braces stripped
    ("We compute it. The final answer is $\\frac{1}{2}$. Done", "\\frac{1}{2}"),   # no box: the sentence
    ("It is 7. Then more", "Itis7"),                                    # neither: the first sentence, normalized
])
def test_the_answer_is_what_opencompass_takes_from_a_reply(reply, answer):
    assert HOOK.answer(reply, {})["answer"] == answer


def test_the_judge_models_yes_is_the_score_and_it_reads_opencompasss_prompt(judge, monkeypatch):
    model = Model("The two agree. \\boxed{yes}")
    monkeypatch.setattr(judge, "_client", model)
    result = judge.score({"id": "q1"}, output("... so \\boxed{0.5}"))
    assert result["score"] == 1.0 and result["feedback"] == "correct" and result["judge_verdict"] == "yes"
    assert result["judge_tokens"] == 57
    messages, kwargs = model.calls[0]
    assert messages == [{"role": "user", "content": judge.JUDGE_PROMPT.format(
        question="What is 1/2?", gold_answer="\\frac{1}{2}", answer="0.5")}]
    assert (kwargs["temperature"], kwargs["max_tokens"]) == (0.0, 8192)


@pytest.mark.parametrize("reply", ["\\boxed{no}", "I cannot tell.", "\\boxed{yes} at first, then \\boxed{no}"])
def test_anything_but_a_last_boxed_yes_scores_0_and_names_the_reference(judge, monkeypatch, reply):
    monkeypatch.setattr(judge, "_client", Model(reply))
    result = judge.score({"id": "q1"}, output("\\boxed{0.4}"))
    assert result["score"] == 0.0 and "\\frac{1}{2}" in result["feedback"]


def test_an_empty_reply_is_0_without_a_model_call(judge, monkeypatch):
    model = Model("\\boxed{yes}")
    monkeypatch.setattr(judge, "_client", model)
    assert judge.score({"id": "q1"}, output(""))["score"] == 0.0 and not model.calls


def test_the_judges_client_is_the_default_references_built_without_a_call(judge, monkeypatch):
    monkeypatch.delenv("LIVEMATHBENCH_JUDGE", raising=False)
    monkeypatch.setattr(judge, "_client", None)
    client = judge.client()
    assert client.base_url.rstrip("/") == "http://127.0.0.1:8000/v1" and client.model == "Qwen/Qwen2.5-72B-Instruct"


def test_a_judge_call_that_fails_raises_rather_than_scoring(judge, monkeypatch):
    monkeypatch.setattr(judge, "_client", Model(error=RuntimeError("endpoint down")))
    with pytest.raises(RuntimeError, match="endpoint down"):
        judge.score({"id": "q1"}, output("\\boxed{0.5}"))


def test_g_pass_at_k_matches_counts_worked_by_hand():
    gpassk = load(UTILS / "gpassk.py", "livemathbench_gpassk")
    got = gpassk.metrics([(4, 4), (4, 0), (4, 2)], 4)     # all right, none right, two of four right
    assert got["G-Pass@4_0"] == pytest.approx(2 / 3)        # pass@4: any right
    assert got["G-Pass@4_0.5"] == pytest.approx(2 / 3)      # at least two of four
    assert got["G-Pass@4_0.75"] == pytest.approx(1 / 3)     # at least three
    assert got["mG-Pass@4"] == pytest.approx(1 / 3)         # (2/4)(G@4_3/4 + G@4_1)
    assert gpassk.g_pass(8, 4, 4, 4) == pytest.approx(1 / 70)     # all four right: C(4,4)/C(8,4)
    # k = 25: 14/25 as a float is past 0.56, ⌈0.56·25⌉ would be 15, not 14; the count is taken from integers
    assert gpassk.metrics([(25, 14)], 25)["mG-Pass@25"] == pytest.approx(
        2 / 25 * sum(gpassk.g_pass(25, 14, 25, i) for i in range(14, 26)))
    with pytest.raises(ValueError, match="at least k"):
        gpassk.metrics([(2, 1)], 4)
