"""Meaningful leakage, immutability, and paired-scoring tests on toy data only."""

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import data
import audit


class DataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "experiment"
        self.corpus = self.base / "source-corpus"
        self.corpus.mkdir()
        (self.corpus / "treasury_bulletin_2000_01.txt").write_text("Synthetic evidence\nZephyr reserve is 25%.\n")
        (self.corpus / "treasury_bulletin_2000_02.txt").write_text("Synthetic unrelated document\nAurora reserve is 50%.\n")
        self.rows = [{"uid": f"toy-{i:03}", "question": f"Synthetic reserve question {i}",
                      "answer": "25%", "source_docs": "https://hidden.invalid/oracle",
                      "source_files": "gold-only-hint", "difficulty": "hard"} for i in range(80)]
        self.csv = self.base / "fixture.csv"
        self.write_rows(self.rows)

    def write_rows(self, rows):
        with self.csv.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def prepare(self):
        return data._prepare_from_sources(self.root, csv_path=self.csv, corpus=self.corpus,
                                          expected_questions=80, expected_documents=2)

    def predictions(self, *, baseline="25%", adapted="25.1%"):
        tasks = data.load_tasks(self.root)
        paths = {}
        for arm, value in (("baseline", baseline), ("adapted", adapted)):
            path = self.root / f"{arm}.jsonl"
            path.write_text("".join(json.dumps({"task_id": row["task_id"], "answer": value}) + "\n" for row in tasks))
            paths[arm] = path
        return paths

    def test_selection_is_independent_of_csv_order_answers_and_hints(self):
        first = self.prepare()
        self.write_rows([dict(row, answer="999999", source_docs="other", source_files="other") for row in reversed(self.rows)])
        second = data._prepare_from_sources(self.base / "second", csv_path=self.csv, corpus=self.corpus,
                                             expected_questions=80, expected_documents=2)
        self.assertEqual(first["task_ids"], second["task_ids"])
        self.assertEqual(first["ordered_task_ids_sha256"], second["ordered_task_ids_sha256"])
        self.assertEqual(first["selection"]["seed"], data.SEED)
        self.assertEqual(len(set(first["task_ids"])), 50)

    def test_fifty_is_prefix_of_old_seventy_without_reading_old_performance(self):
        manifest = self.prepare()
        old_order = sorted(self.rows, key=lambda row: (hashlib.sha256((data.SEED + "\0" + row["uid"]).encode()).hexdigest(), row["uid"]))[:70]
        self.assertEqual(manifest["task_ids"], [row["uid"] for row in old_order[:50]])
        self.assertEqual(manifest["learner_policy"]["reference_scope"], "completed_batch_only")
        self.assertTrue(manifest["learner_policy"]["includes_gold"])
        self.assertFalse(manifest["learner_policy"]["master_answer_key_mounted"])
        self.assertIsNone(manifest["actor_budgets"]["requests"])
        self.assertEqual(manifest["grading_policy"]["denominator_per_arm"], 50)
        self.assertFalse(manifest["grading_policy"]["send_results_to_learner"])

    def test_gold_and_task_list_are_not_actor_mounts(self):
        manifest = self.prepare()
        tasks = data.load_tasks(self.root)
        self.assertTrue(all(set(task) == {"task_id", "question"} for task in tasks))
        self.assertEqual(manifest["actor_read_allowlist"], ["agent/corpus/", "agent/corpus.sqlite"])
        self.assertNotIn("hidden.invalid", (self.root / "prepared/agent/tasks.jsonl").read_text())
        self.assertNotIn("gold-only-hint", (self.root / "prepared/agent/tasks.jsonl").read_text())
        index = data._tools.OfficeQACorpus(self.root / "prepared/agent/corpus", self.root / "prepared/agent/corpus.sqlite")
        self.assertEqual(index.search("hidden oracle gold-only-hint")["results"], [])
        self.assertTrue(data.verify_prepared(self.root)["verified"])
        self.assertTrue(manifest["fixture_only"])
        self.assertFalse(manifest["official_headline_comparable"])

    def test_prepare_refuses_overwrite(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.prepare()

    def test_grading_refuses_to_run_before_paired_seal(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, "sealed"):
            data.grade(self.root)

    def test_partial_duplicate_or_unknown_predictions_cannot_be_sealed(self):
        self.prepare()
        paths = self.predictions()
        rows = [json.loads(line) for line in paths["adapted"].read_text().splitlines()]
        for bad in (rows[:-1], rows[:-1] + [rows[0]], rows[:-1] + [{"task_id": "unknown", "answer": "25%"}]):
            paths["adapted"].write_text("".join(json.dumps(row) + "\n" for row in bad))
            with self.assertRaisesRegex(ValueError, "50 unique"):
                data.seal_predictions(self.root, paths)
        self.assertFalse((self.root / "private-evaluation/predictions-seal.json").exists())

    def test_sealed_prediction_mutation_is_detected(self):
        self.prepare()
        paths = self.predictions()
        data.seal_predictions(self.root, paths)
        paths["baseline"].chmod(0o600)
        paths["baseline"].write_text(paths["baseline"].read_text().replace("25%", "24%"))
        with self.assertRaisesRegex(ValueError, "Predictions changed"):
            data.grade(self.root)

    def test_same_prediction_file_cannot_impersonate_two_arms(self):
        self.prepare()
        paths = self.predictions()
        with self.assertRaisesRegex(ValueError, "distinct"):
            data.seal_predictions(self.root, {"baseline": paths["baseline"], "adapted": paths["baseline"]})

    def test_official_strict_secondary_and_empty_denominator(self):
        self.prepare()
        paths = self.predictions()
        rows = [json.loads(line) for line in paths["baseline"].read_text().splitlines()]
        rows[0]["answer"] = ""
        paths["baseline"].write_text("".join(json.dumps(row) + "\n" for row in rows))
        seal_path = data.seal_predictions(self.root, paths)
        self.assertEqual(json.loads(seal_path.read_text())["total_predictions"], 100)
        result = data.grade(self.root)
        self.assertEqual(result["denominator_per_arm"], 50)
        self.assertEqual(result["arms"]["baseline"]["correct"], 49)
        self.assertEqual(result["arms"]["adapted"]["correct"], 0)
        self.assertEqual(result["arms"]["adapted"]["one_percent_accuracy"], 1)
        self.assertEqual(result["paired_strict"]["baseline_only"], 49)
        self.assertEqual(result["paired_strict"]["both_wrong"], 1)
        for arm in data.ARMS:
            grades = json.loads((self.root / f"private-evaluation/{arm}-grades.json").read_text())
            self.assertTrue(all("answer" not in row and "ground_truth" not in row for row in grades["results"]))
        with self.assertRaisesRegex(ValueError, "overwrite"):
            data.grade(self.root)

    def test_changed_gold_key_is_rejected(self):
        self.prepare()
        paths = self.predictions()
        data.seal_predictions(self.root, paths)
        key = self.root / "prepared/private/answers.jsonl"
        key.chmod(0o600)
        key.write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "Prepared input changed"):
            data.grade(self.root)

    def test_task_order_hash_detects_tampering(self):
        self.prepare()
        tasks = self.root / "prepared/agent/tasks.jsonl"
        tasks.chmod(0o600)
        lines = tasks.read_text().splitlines()
        tasks.write_text("\n".join(reversed(lines)) + "\n")
        with self.assertRaisesRegex(ValueError, "Task records changed"):
            data.load_tasks(self.root)


class ReferenceReleaseAuditTests(unittest.TestCase):
    """Synthetic reference text never leaves the test process."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.evidence = self.root / "evidence/01"
        self.tasks = [{"task_id": f"toy-{n}", "question": "Synthetic question"} for n in range(1, 6)]
        self.started = "2026-01-01T00:00:03+00:00"
        self.seal = {"block": 1, "source_arm": "adapted", "predictions_sealed": True,
                     "reference_scope": "completed_batch_only",
                     "sealed_utc": "2026-01-01T00:00:01+00:00",
                     "reference_released_utc": "2026-01-01T00:00:02+00:00", "episodes": []}
        self.manifest = {"episodes": []}
        for n, task in enumerate(self.tasks, 1):
            original = self.root / "runs/adapted" / f"{n:03}"
            ref = self.evidence / f"{n:03}/reference_answer.json"
            self.save(original / "task.json", {"ordinal": n, **task})
            self.save(original / "prediction.json", {"task_id": task["task_id"], "answer": "toy prediction"})
            self.save(original / "records/result.json", {"status": "completed", "finished_utc": "2026-01-01T00:00:00+00:00"})
            self.save(ref, {"task_id": task["task_id"], "official_answer": "SYNTHETIC_REFERENCE_NOT_FOR_OUTPUT"})
            self.seal["episodes"].append({"id": task["task_id"], "ordinal": n,
                "task_sha256": audit.sha(original / "task.json"),
                "prediction_sha256": audit.sha(original / "prediction.json"),
                "reference_answer_sha256": audit.sha(ref)})
            self.manifest["episodes"].append({"id": task["task_id"], "ordinal": n, "directory": f"{n:03}"})
        self.reseal()

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def reseal(self):
        self.receipt = self.root / "private-learning-seals/01.json"
        self.save(self.receipt, self.seal)
        self.manifest["batch_seal"] = {"file": "01.json", "sha256": audit.sha(self.receipt)}

    def check_release(self):
        instance = audit.Audit(self.root)
        instance.guard("reference-test", lambda: instance.reference_release(
            1, self.manifest, self.evidence, self.tasks, self.started))
        self.assertNotIn("SYNTHETIC_REFERENCE_NOT_FOR_OUTPUT", json.dumps(instance.checks))
        return instance.checks

    def test_correct_completed_batch_seal_passes_without_exporting_reference(self):
        self.assertTrue(all(row["passed"] for row in self.check_release()))

    def test_reference_mutation_breaks_receipt_binding(self):
        self.save(self.evidence / "001/reference_answer.json",
                  {"task_id": "toy-1", "official_answer": "changed synthetic reference"})
        self.assertFalse(all(row["passed"] for row in self.check_release()))

    def test_future_reference_or_extra_source_hint_is_rejected(self):
        self.save(self.evidence / "001/reference_answer.json",
                  {"task_id": "future-toy-6", "official_answer": "toy", "source_files": "oracle.txt"})
        self.assertFalse(all(row["passed"] for row in self.check_release()))

    def test_reference_release_before_prediction_seal_is_rejected(self):
        self.seal["reference_released_utc"] = "2025-12-31T23:59:59+00:00"
        self.reseal()
        self.assertFalse(all(row["passed"] for row in self.check_release()))

    def test_prediction_modified_after_seal_is_rejected(self):
        self.save(self.root / "runs/adapted/001/prediction.json",
                  {"task_id": "toy-1", "answer": "rewritten prediction"})
        self.assertFalse(all(row["passed"] for row in self.check_release()))

    def test_private_master_answer_key_is_refused(self):
        path = self.root / "prepared/private/answers.jsonl"
        self.save(path, {"toy": "never open"})
        with self.assertRaisesRegex(ValueError, "private master-key"):
            audit.file(path)


if __name__ == "__main__":
    unittest.main()
