"""The OfficeQA optimizer Environment with a synthetic prepared root and a fake
actor: splits by ordinal, the official grader, the per-split feedback policy,
and trajectory rendering. No model or network call."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import optimize_env


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fake_prepared(root: Path, count: int = 8) -> list[dict]:
    prepared = root / "prepared"
    (prepared / "agent/corpus").mkdir(parents=True)
    (prepared / "agent/corpus.sqlite").write_bytes(b"")
    (prepared / "private").mkdir()
    tasks = [{"task_id": f"UID{i:04d}", "question": f"What is value {i}?"} for i in range(1, count + 1)]
    (prepared / "agent/tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
    answers = [{"task_id": t["task_id"], "answer": str(100 + i)} for i, t in enumerate(tasks, start=1)]
    (prepared / "private/answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in answers))
    ids = [t["task_id"] for t in tasks]
    (prepared / "manifest.json").write_text(json.dumps({
        "tasks_sha256": _sha(prepared / "agent/tasks.jsonl"), "sample_count": count, "task_ids": ids,
        "ordered_task_ids_sha256": hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()}))
    return answers


def fake_actor(answers_by_ordinal):
    def actor(key, run, ordinal, task, adapter, evidence_dir, corpus, index, timeout, max_tool_ops=None):
        base = run / "questions" / f"{ordinal:03d}"
        (base / "records").mkdir(parents=True)
        events = [{"type": "item.completed", "item": {"type": "command_execution", "command": "python officeqa.py search",
                                                      "aggregated_output": "table row 1990: 101", "exit_code": 0}},
                  {"type": "item.completed", "item": {"type": "reasoning", "text": "hidden"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": answers_by_ordinal[ordinal]}}]
        (base / "records/events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        actor.adapters.append(adapter)
        return {"ordinal": ordinal, "task_id": task["task_id"], "status": "completed",
                "answer": answers_by_ordinal[ordinal], "usage": {"output_tokens": 10}, "requests": 2, "elapsed_seconds": 1.0}
    actor.adapters = []
    return actor


class OfficeQAEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        fake_prepared(self.root / "prep")
        os.environ["OQA_TEST_KEY"] = "not-a-real-key"
        self.env = optimize_env.OfficeQAEnvironment(str(self.root / "prep"), str(self.root / "runs"), key_env="OQA_TEST_KEY",
                                                    splits={"test": "1-2", "val": "3-4", "train": "5-8"}, concurrency=2)

    def tearDown(self):
        self.directory.cleanup()

    def test_splits_follow_the_prepared_order(self):
        self.assertEqual([t.id for t in self.env.tasks("test")], ["UID0001", "UID0002"])
        self.assertEqual([t.id for t in self.env.tasks("val")], ["UID0003", "UID0004"])
        self.assertEqual(len(self.env.tasks("train")), 4)
        self.assertNotIn("answer", json.dumps([t.__dict__ for t in self.env.tasks("train")]))
        self.assertNotIn("not-a-real-key", json.dumps(self.env.describe()))

    def test_grading_and_feedback_policy(self):
        actor = fake_actor({5: "105", 6: "999", 3: "103", 4: "0", 1: "101"})
        with mock.patch.object(optimize_env.arm, "actor", actor):
            train = self.env.evaluate(None, self.env.tasks("train")[:2], label="gepa/0001-minibatch")
            val = self.env.evaluate(None, self.env.tasks("val"), label="gepa/0002-val")
            test = self.env.evaluate(None, self.env.tasks("test")[:1], label="test/w0")
        self.assertEqual([r.score for r in train], [1.0, 0.0])
        self.assertEqual(train[1].feedback, "INCORRECT. Your answer: 999. Correct answer: 106.")
        self.assertEqual([r.score for r in val], [1.0, 0.0])
        self.assertEqual(val[1].feedback, "INCORRECT.")               # no reference on the selection split
        self.assertEqual((test[0].score, test[0].feedback), (1.0, ""))
        self.assertIn("[command] python officeqa.py search", train[0].trace)
        self.assertNotIn("hidden", train[0].trace)                    # model reasoning is not trajectory
        self.assertEqual(train[0].steps[0], {"type": "tool_call", "cmd": "python officeqa.py search", "obs": "table row 1990: 101"})
        calls = sorted((self.root / "runs/evaluations").iterdir())
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads((calls[0] / "call.json").read_text())["label"], "gepa/0001-minibatch")

    def test_a_crashed_episode_is_a_scored_failure(self):
        def broken(*args, **kwargs):
            raise RuntimeError("sandbox refused")
        with mock.patch.object(optimize_env.arm, "actor", broken):
            [rollout] = self.env.evaluate(None, self.env.tasks("train")[:1], label="x")
        self.assertEqual((rollout.score, rollout.status), (0.0, "error: RuntimeError"))

    def test_candidate_adapter_is_passed_to_the_actor(self):
        actor = fake_actor({5: "105"})
        with mock.patch.object(optimize_env.arm, "actor", actor):
            self.env.evaluate(self.root / "prep", self.env.tasks("train")[:1], label="x")
        self.assertEqual(actor.adapters, [(self.root / "prep").resolve()])


if __name__ == "__main__":
    unittest.main()
