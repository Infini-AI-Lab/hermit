"""LiveMathBench's judge, on the host, as OpenCompass's LiveMathBenchEvaluator scores an answer
(opencompass/datasets/livemathbench/, commit 4f3812d4a1fb7ab80ab1f91e04494881400a165b): the answer is what its
`math_postprocess_v2` takes from the agent's reply (utils/agent/answer.py, the agent's answer hook), and a judge
model reads the question, the reference and that answer in OpenCompass's English judge prompt, at temperature 0 with
8,192 output tokens; the score is 1 when the last `\\boxed{}` of its reply is `yes`, else 0.

The judge model is a `<backend>/<model>` reference (docs/guides/model-backends.md) in LIVEMATHBENCH_JUDGE, by default
the paper's Qwen2.5-72B-Instruct on a local vLLM server (`vllm serve Qwen/Qwen2.5-72B-Instruct`); OpenCompass's
LiveMath-Judge (jnanliu/LiveMath-Judge) is the smaller one it offers. A judge call that fails raises: the run records a
failed judgement (`hermit eval` counts it among its errors, and as 0 in its mean_score). An empty answer (an empty
reply) scores 0 without a call; OpenCompass would still ask its judge model, with nothing to match."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent

DEFAULT_JUDGE = "vllm/Qwen/Qwen2.5-72B-Instruct"
# OpenCompass's JUDGE_PROMPT_EN (opencompass/datasets/livemathbench/prompts.py at the commit above), with its doubled
# braces as str.format reads them
JUDGE_PROMPT = '''Please act as an expert in grading mathematics exam papers, and judge whether the following answers match the standard answers, i.e., whether the examinee answered correctly. Here are some evaluation criteria:

1. Some answers may contain multiple parts, such as single-choice questions, multiple-choice questions, fill-in-the-blank questions, and problem-solving questions. As long as the answer matches the standard answer, it is considered correct. For multiple-choice questions and fill-in-the-blank questions with multiple blanks, the examinee must answer all corresponding options or blanks correctly to be considered correct.
2. Some answers may be expressed in different ways; for example, some answers may be mathematical expressions, while others may be textual descriptions. As long as the meaning conveyed is consistent, it is considered correct. Additionally, some formulas may be expressed differently but are equivalent, which is also considered correct.
3. You do not need to recalculate the problem answers, as the standard answers are already provided. You only need to judge whether the examinee's answer matches the standard answer based on the form of the question and whether it is correct.

Please judge whether the following answer matches the standard answer according to the above criteria. If they match, output \\boxed{{yes}}, otherwise output \\boxed{{no}}. If it is difficult to judge, also output \\boxed{{no}}.
Original Question: {question}
Standard Answer: {gold_answer}
Examinee's Answer: {answer}

Analysis:
'''
SETTINGS = {"temperature": 0.0, "max_tokens": 8192}      # OpenCompass's judge call: temperature 0, 8,192 out


PRIVATE = HERE / ".private"            # prepare.py's references, with each question and its type
_answers: dict[str, dict] | None = None
_client = None


def answers() -> dict[str, dict]:
    """The references by task id, read once."""
    global _answers
    if _answers is None:
        path = PRIVATE / "answers.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"{path}: run utils/benchmark/prepare.py first")
        _answers = {r["id"]: r for r in map(json.loads, path.read_text().splitlines()) if r}
    return _answers


def client():
    """The judge model's chat client, once per process (Layer 1: hermit.agent.model)."""
    global _client
    if _client is None:
        from hermit.agent.model.backends import registry, resolve
        from hermit.agent.model.llm import ChatClient
        _client = ChatClient.from_spec(resolve(os.environ.get("LIVEMATHBENCH_JUDGE", DEFAULT_JUDGE),
                                               registry()).spec())
    return _client


def verdict(reply: str) -> str | None:
    """OpenCompass's extract_judge_label: the last `\\boxed{...}` of the judge's reply; `yes` is right."""
    found = re.findall(r"\\boxed{(.+?)}", reply or "")
    return found[-1] if found else None


def score(task: dict, output) -> dict:
    from hermit.agent.model.direct import Direct
    row = answers()[task["id"]]
    out = output.output if isinstance(output.output, dict) else {}
    given = out.get("answer")
    if not given:
        why = out.get("fail_reason") or "no answer in the reply"
        return {"score": 0.0, "feedback": f"{why}; the answer is {row['answer']}", "answered": False}
    prompt = JUDGE_PROMPT.format(question=row["question"], gold_answer=row["answer"], answer=given)
    reply = Direct(SETTINGS, client()).run(prompt)
    said = verdict(reply.reply)
    right = said == "yes"
    return {"score": float(right), "answered": True, "judge_verdict": said,
            "judge_tokens": sum(u.input_tokens + u.output_tokens for u in reply.usage),
            "feedback": "correct" if right else f"answered {given}, but the answer is {row['answer']}"}
