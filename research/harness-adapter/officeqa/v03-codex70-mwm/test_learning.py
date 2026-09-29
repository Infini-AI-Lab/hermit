"""Synthetic learner safety/provenance tests. No API, gold data, or credentials."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import learning as L


def manifest(block=1):
    return {"block": block, "source_arm": "adapted", "episodes": [
                {"id": f"fixture-{i}", "directory": f"{i+1:03}"} for i in range(5)],
            "includes_gold": False, "includes_grades": False,
            "includes_future_questions": False, "includes_baseline_traces": False}


def feedback():
    return {"label": "glm_self_assessment_without_gold", "episodes": [
        {"id": f"fixture-{i}", "observations": "A fixture tool returned text.",
         "self_assessment": "This may support a reusable check.", "uncertainty": "No gold label is supplied.",
         "proposed_lesson": "Check tool failures before using their output."} for i in range(5)]}


def setup_session(root):
    for area in ("adapter", "evidence", "scratch", "history", "method", "environment", "control", "host"):
        (root / area).mkdir(parents=True)
    L.save(root / "evidence/manifest.json", manifest())
    (root / "evidence/README.md").write_text("Five synthetic episodes, no private data.\n")
    for episode in manifest()["episodes"]:
        directory = root / "evidence" / episode["directory"]
        directory.mkdir()
        L.save(directory / "task.json", {"id": episode["id"], "question": "Synthetic placeholder question."})
        L.save(directory / "prediction.json", {"status": "fixture", "answer": "Synthetic response."})
        (directory / "trajectory.jsonl").write_text(json.dumps({"role": "assistant", "content": "Synthetic observation."}) + "\n")
    L.save(root / "scratch/feedback.json", feedback())
    return root


class FakeBroker:
    """Only synthetic native-tool behavior, never an actual executable tool."""
    def __init__(self, session):
        self.session = session
        self.calls = []

    async def list_tools(self):
        specs = {
            "list_files": {"area": {"type": "string"}},
            "read_file": {"area": {"type": "string"}, "path": {"type": "string"}, "offset": {"type": "integer"}},
            "write_file": {"area": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}},
            "delete_path": {"area": {"type": "string"}, "path": {"type": "string"}},
            "move_path": {"area": {"type": "string"}, "source": {"type": "string"}, "destination": {"type": "string"}},
            "validate": {}, "get_policy": {}}
        return SimpleNamespace(tools=[SimpleNamespace(name=name, description=name,
            inputSchema={"type": "object", "properties": props}) for name, props in specs.items()])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "write_file":
            path = self.session / args["area"] / args["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
            value = {"written": args["path"]}
        elif name == "read_file":
            content = (self.session / args["area"] / args["path"]).read_text()
            offset = args.get("offset", 0)
            value = {"text": content[offset:offset+30000],
                     "next_offset": offset+30000 if len(content) > offset+30000 else None}
        else:
            value = {"ok": True}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(value))], isError=False)


def toolcall(name, args, number=1):
    return {"id": f"fixture-call-{number}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}


class FakeTransport:
    def __init__(self, messages, model=L.MODEL):
        self.messages = list(messages)
        self.model = model
        self.options = []

    def complete(self, key, messages, **options):
        self.options.append(options)
        message = self.messages.pop(0)
        choice = {"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}
        response = {"choices": [choice], "model": self.model}
        return response, {"http_status": 200, "response_model": self.model,
                          "usage": {"prompt_tokens": 20, "completion_tokens": 10}}


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.session = setup_session(self.base / "session")

    def test_approved_knowledge_paths_only(self):
        for name in ("memory/a.md", "memory/nested/a.md", "examples/a.md", "meta/cases/x.md",
                     "meta/journal.md", "meta/journal-archive.md", "consumption/brief.md"):
            self.assertTrue(L.allowed_candidate_path(name), name)
        for name in ("/memory/a.md", "memory/../a.md", "memory//a.md", "memory/.x.md", "memory/a.py",
                     "interface.py", "adapter.json", "agents/a.md", "tools/a.md", "core.md", "memory\\a.md", None):
            self.assertFalse(L.allowed_candidate_path(name), name)

    def test_write_gates_and_required_feedback(self):
        L.check_arguments(self.session, "write_file", {"area": "adapter", "path": "memory/x.md", "content": "ok"})
        cases = [("write_file", {"area": "evidence", "path": "x.md", "content": "bad"}),
                 ("write_file", {"area": "adapter", "path": "interface.py", "content": "bad"}),
                 ("write_file", {"area": "scratch", "path": "x.py", "content": "bad"}),
                 ("read_file", {"area": "control", "path": "policy.json"}),
                 ("run_command", {"command": "echo blocked"}),
                 ("fetch_url", {"url": "https://example.invalid/"}),
                 ("write_file", {"area": "adapter", "path": "consumption/brief.md", "content": "x" * 6001})]
        for name, args in cases:
            with self.subTest(name=name, args=list(args)), self.assertRaises(ValueError):
                L.check_arguments(self.session, name, args)
        (self.session / "scratch/feedback.json").unlink()
        with self.assertRaises(ValueError):
            L.check_arguments(self.session, "write_file", {"area": "adapter", "path": "memory/x.md", "content": "ok"})

    def test_symlinks_traversal_and_special_roots(self):
        (self.base / "outside.txt").write_text("fixture only")
        (self.session / "scratch/link.txt").symlink_to(self.base / "outside.txt")
        with self.assertRaises(ValueError): L.safe_file(self.session / "scratch", "link.txt")
        with self.assertRaises(ValueError): L.snapshot(self.session / "scratch")
        for path in ("../outside.txt", "/outside.txt", ".secret", "a/../../b"):
            with self.assertRaises(ValueError): L.safe_file(self.session / "scratch", path, existing=False)

    def test_evidence_contract_rejects_bad_arm_count_labels(self):
        before, _ = L.validate_evidence(self.session / "evidence", 1)
        self.assertIn("manifest.json", before)
        for key, value in (("source_arm", "baseline"), ("episodes", [{"id": "x"}]),
                           ("includes_gold", True), ("includes_grades", None), ("block", 2)):
            data = manifest()
            data[key] = value
            L.save(self.session / "evidence/manifest.json", data)
            with self.subTest(key=key), self.assertRaises(ValueError):
                L.validate_evidence(self.session / "evidence", 1)
        L.save(self.session / "evidence/manifest.json", manifest())
        L.save(self.session / "evidence/private.json", {"nested": {"gold_answer": "synthetic forbidden field"}})
        with self.assertRaises(ValueError): L.validate_evidence(self.session / "evidence", 1)

    def test_feedback_ids_label_nonempty_fields(self):
        doc = L.feedback_document(self.session)
        self.assertEqual(len(doc["episode_ids"]), 5)
        for mutate in (lambda d: d.update(label="human"),
                       lambda d: d["episodes"][0].update(id="future-fixture"),
                       lambda d: d["episodes"][0].update(uncertainty="")):
            data = feedback()
            mutate(data)
            L.save(self.session / "scratch/feedback.json", data)
            with self.assertRaises(ValueError): L.feedback_document(self.session)

    def test_broker_has_no_inherited_secrets(self):
        with patch.dict("os.environ", {"FIREWORKS_API_KEY": "synthetic", "HF_TOKEN": "synthetic", "EXTRA_SECRET": "synthetic"}):
            env = L.broker_environment(self.session)
        self.assertEqual(set(env), {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "HOME", "TMPDIR", "LANG"})
        self.assertEqual(env["HOME"], str(self.session / "host"))

    def test_prior_source_requires_lineage_and_first_block_generic(self):
        before, lineage = L.validate_source(L.GENERIC_SOURCE, 1)
        self.assertEqual(lineage["kind"], "generic_mwm_architecture")
        fake = self.base / "prior/sessions/evolve-fixture/adapter"
        shutil.copytree(L.GENERIC_SOURCE, fake, ignore=shutil.ignore_patterns("__pycache__"))
        with self.assertRaises(ValueError): L.validate_source(fake, 1)
        with self.assertRaises(ValueError): L.validate_source(fake, 2)
        L.save(fake.parents[2] / "result.json", {"status": "admitted", "activated": True,
            "candidate": str(fake), "revision": L.revision(L.snapshot(fake)), "block": 1,
            "model": L.MODEL, "admission_mode": "automatic_mechanical_only"})
        _, lineage = L.validate_source(fake, 2)
        self.assertEqual(lineage["kind"], "previous_glm_candidate")
        (fake / "interface.py").write_text("# unauthorized replacement\n")
        with self.assertRaises(ValueError): L.validate_source(fake, 2)

    def test_failed_source_retains_old_w_and_no_overwrite(self):
        source = self.base / "bad-source"
        source.mkdir()
        out = self.base / "attempt"
        result = L.run_learning("synthetic-key", source, self.session / "evidence", out, 1)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["candidate"], str(source))
        self.assertFalse(result["activated"])
        with self.assertRaises(ValueError):
            L.run_learning("synthetic-key", source, self.session / "evidence", out, 1)


class Conversations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = setup_session(Path(self.temp.name).resolve() / "session")
        self.broker = FakeBroker(self.session)

    def run_conversation(self, messages, *, model=L.MODEL):
        transport = FakeTransport(messages, model=model)
        state = asyncio.run(L._conversation("synthetic-key", self.broker, self.session,
            "synthetic initial prompt", time.monotonic() + 30, {}, transport))
        return state, transport

    def failed_transport_state(self, meta):
        # Remove setup's synthetic feedback so any unexpected generation is visible.
        (self.session / "scratch/feedback.json").unlink(missing_ok=True)
        observed = []

        def complete(key, messages, **options):
            checkpoint = json.loads((self.session / "control/fireworks-result.json").read_text())
            observed.append(checkpoint)
            return None, meta

        state = asyncio.run(L._conversation("synthetic-key", self.broker, self.session,
            "synthetic initial prompt", time.monotonic() + 30, {}, SimpleNamespace(complete=complete)))
        self.assertEqual(state["status"], "api_error")
        self.assertEqual(state["model_calls"], 1)
        self.assertEqual(observed[0]["in_flight_request"], 1)
        self.assertNotIn("in_flight_request", state)
        self.assertEqual(state["requested_tools"], 0)
        self.assertEqual(self.broker.calls, [])
        self.assertFalse((self.session / "scratch/feedback.json").exists())
        self.assertFalse((self.session / "control/fireworks-report.md").exists())
        self.assertEqual(state["expected_candidate_hashes"], {})
        self.assertEqual(state["expected_scratch_hashes"], {})
        persisted = json.loads((self.session / "control/fireworks-result.json").read_text())
        self.assertEqual(persisted, state)
        call = json.loads((self.session / "control/fireworks-calls/01.json").read_text())
        self.assertEqual(call["meta"], meta)
        self.assertNotIn("message", call)
        return state

    def test_failed_api_preserves_nonzero_known_usage_and_attempt_counts(self):
        state = self.failed_transport_state({
            "http_status": 502, "error": "Synthetic failure after metered generation",
            "usage": {"prompt_tokens": 321, "completion_tokens": 17,
                      "prompt_tokens_details": {"cached_tokens": 256}},
            "usage_complete": True, "wire_attempts": 3,
            "attempts": [{"status": "rate_refused", "wait_seconds": 15},
                         {"status": "rate_refused", "wait_seconds": 30},
                         {"status": "failed", "wait_seconds": 0}]})
        self.assertEqual(state["usage"], {"prompt_tokens": 321, "completion_tokens": 17, "cached_tokens": 256})
        self.assertTrue(state["usage_complete"])
        self.assertEqual(state["usage_unknown_calls"], [])
        self.assertEqual(state["wire_attempts"], 3)
        self.assertEqual(state["rate_refusals"], 2)
        self.assertEqual(state["scheduler_wait_seconds"], 45)

    def test_failed_api_missing_usage_is_unknown_not_claimed_zero_cost(self):
        state = self.failed_transport_state({"http_status": None, "error": "Synthetic response lost"})
        self.assertEqual(state["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
        self.assertFalse(state["usage_complete"])
        self.assertEqual(state["usage_unknown_calls"], [1])
        self.assertEqual(state["wire_attempts"], 1)

    def test_failed_api_partial_usage_retained_but_marked_incomplete(self):
        state = self.failed_transport_state({
            "http_status": None, "error": "Synthetic partial metering",
            "usage": {"prompt_tokens": 90, "completion_tokens": 4},
            "usage_complete": False, "wire_attempts": 1})
        self.assertEqual(state["usage"], {"prompt_tokens": 90, "completion_tokens": 4, "cached_tokens": 0})
        self.assertFalse(state["usage_complete"])
        self.assertEqual(state["usage_unknown_calls"], [1])

    def test_public_feedback_and_knowledge_have_exact_provenance(self):
        feedback_text = json.dumps(feedback())
        reads = [toolcall("read_file", {"area": "evidence", "path": path}, i)
                 for i, path in enumerate(L.required_evidence_files(self.session / "evidence"), 1)]
        calls = [toolcall("write_file", {"area": "scratch", "path": "feedback.json", "content": feedback_text}, 16),
                 toolcall("write_file", {"area": "adapter", "path": "memory/checks.md", "content": "# GLM fixture lesson\n"}, 17)]
        state, transport = self.run_conversation([
            {"role": "assistant", "content": "First read original records.", "tool_calls": reads},
            {"role": "assistant", "content": "My own fixture feedback.", "reasoning_content": "PRIVATE_REASONING_FIXTURE", "tool_calls": calls},
            {"role": "assistant", "content": "Finished; correctness unknown without gold."}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["model_calls"], 3)
        self.assertEqual(state["successful_tools"], 17)
        self.assertEqual(len(state["evidence_reads_delivered"]), 15)
        self.assertTrue(all(r["delivered_request"] == 2 for r in state["evidence_reads_delivered"]))
        self.assertEqual(state["expected_scratch_hashes"]["feedback.json"], L.digest(feedback_text.encode()))
        self.assertEqual(state["expected_candidate_hashes"]["memory/checks.md"], L.digest(b"# GLM fixture lesson\n"))
        self.assertEqual(state["response_models"], [L.MODEL])
        for path in (self.session / "control").rglob("*.json"):
            self.assertNotIn("PRIVATE_REASONING_FIXTURE", path.read_text())
        self.assertEqual(transport.options[0]["reasoning_effort"], 4096)
        self.assertEqual(transport.options[0]["max_tokens"], 32768)
        first = json.loads((self.session / "control/fireworks-calls/02.json").read_text())
        self.assertEqual(first["message"]["tool_calls"], calls)
        self.assertEqual(first["requested_model"], L.MODEL)

    def test_read_not_delivered_cannot_author_feedback_in_same_response(self):
        reads = [toolcall("read_file", {"area": "evidence", "path": path}, i)
                 for i, path in enumerate(L.required_evidence_files(self.session / "evidence"), 1)]
        reads.append(toolcall("write_file", {"area": "scratch", "path": "feedback.json", "content": json.dumps(feedback())}, 16))
        state, _ = self.run_conversation([
            {"role": "assistant", "content": "Try premature feedback", "tool_calls": reads},
            {"role": "assistant", "content": "Done"}])
        self.assertNotIn("feedback.json", state["expected_scratch_hashes"])
        record = json.loads((self.session / "control/fireworks-tool-results/016.json").read_text())
        self.assertFalse(record["ok"])
        self.assertIn("subsequent model request", record["response"])
        self.assertEqual(len(state["evidence_reads_delivered"]), 15)

    def test_contiguous_chunks_and_empty_files_need_successful_delivery(self):
        required = {"episode/trajectory.jsonl": {"id": "fixture", "characters": 60001, "sha256": "fixture-hash"}}
        records = [{"path": "episode/trajectory.jsonl", "file_sha256": "fixture-hash", "start": start, "end": end}
                   for start, end in ((0, 30000), (30000, 60000), (60000, 60001))]
        self.assertFalse(L.evidence_coverage(required, records[:1])["episode/trajectory.jsonl"]["complete"])
        self.assertFalse(L.evidence_coverage(required, records[::2])["episode/trajectory.jsonl"]["complete"])
        self.assertTrue(L.evidence_coverage(required, records)["episode/trajectory.jsonl"]["complete"])
        required["empty.txt"] = {"id": "fixture", "characters": 0, "sha256": "empty-hash"}
        self.assertFalse(L.evidence_coverage(required, records)["empty.txt"]["complete"])
        records.append({"path": "empty.txt", "file_sha256": "empty-hash", "start": 0, "end": 0})
        self.assertTrue(L.evidence_coverage(required, records)["empty.txt"]["complete"])

    def test_unknown_model_cannot_execute_tools(self):
        state, _ = self.run_conversation([{"role": "assistant", "content": "", "tool_calls": [
            toolcall("write_file", {"area": "adapter", "path": "memory/x.md", "content": "unauthorized"})]}], model="different-model")
        self.assertEqual(state["status"], "unexpected_response_model")
        self.assertEqual(self.broker.calls, [])
        self.assertFalse((self.session / "adapter/memory/x.md").exists())

    def test_shell_unknown_args_and_extra_tool_budget_are_rejected(self):
        calls = [toolcall("run_command", {"command": "not executed"}, 1),
                 toolcall("read_file", {"area": "evidence", "path": "README.md", "unexpected": True}, 2)]
        state, _ = self.run_conversation([{"role": "assistant", "tool_calls": calls}, {"role": "assistant", "content": "Done"}])
        self.assertEqual(state["successful_tools"], 0)
        self.assertEqual(self.broker.calls, [])
        with patch.object(L, "MAX_TOOLS", 1):
            state, _ = self.run_conversation([{"role": "assistant", "tool_calls": [
                toolcall("read_file", {"area": "evidence", "path": "README.md"}, 3),
                toolcall("read_file", {"area": "evidence", "path": "README.md"}, 4)]}])
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["successful_tools"], 1)


class Admission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.source, self.evidence = base / "source", base / "evidence"
        shutil.copytree(L.GENERIC_SOURCE, self.source, ignore=shutil.ignore_patterns("__pycache__"))
        self.evidence.mkdir()
        L.save(self.evidence / "manifest.json", manifest())
        (self.evidence / "README.md").write_text("Five synthetic records only.\n")
        self.session = base / "session"
        setup_session(self.session)
        for episode in manifest()["episodes"]:
            shutil.copytree(self.session / "evidence" / episode["directory"], self.evidence / episode["directory"])
        shutil.copytree(self.source, self.session / "adapter", dirs_exist_ok=True)
        shutil.copytree(self.source, self.session / "history", dirs_exist_ok=True)
        shutil.copytree(self.evidence, self.session / "evidence", dirs_exist_ok=True)
        if str(L.REPOSITORY) not in sys.path:
            sys.path.insert(0, str(L.REPOSITORY))
        from harnessadapter.evolution.workspace import fingerprint
        from harnessadapter.paths import PACKAGE_ROOT
        baseline = {"source": fingerprint(self.source),
                    "framework": fingerprint(PACKAGE_ROOT, exclude=("evolvers", "architectures"))}
        for area in ("evidence", "method", "environment", "history"):
            baseline[area] = fingerprint(self.session / area)
        L.save(self.session / "control/baseline.json", baseline)
        self.before = L.snapshot(self.source)
        self.evidence_before = L.snapshot(self.evidence)
        self.prepared_before = L.snapshot(self.session / "adapter")
        (self.session / "adapter/memory/fixture.md").write_text("# A synthetic GLM-authored reusable check\n")
        self.state = {"status": "completed", "successful_tools": 3, "evidence_reads": [{"path": "README.md"}],
                      "response_models": [L.MODEL],
                      "evidence_reads_delivered": [{"path": path, "file_sha256": entry["sha256"],
                          "start": 0, "end": entry["characters"], "delivered_request": 2}
                          for path, entry in L.required_evidence_files(self.evidence).items()],
                      "expected_candidate_hashes": {k: v["sha256"] for k, v in L.snapshot(self.session / "adapter").items()},
                      "expected_scratch_hashes": {"feedback.json": L.digest((self.session / "scratch/feedback.json").read_bytes())}}
        self.definition = SimpleNamespace(finalize=lambda *_: {"fixture": "No executable tools admitted"})

    def admit(self):
        return L.mechanical_admission(self.session, self.source, self.evidence, self.before,
            self.evidence_before, self.prepared_before, self.state, self.definition)

    def test_valid_glm_candidate_is_automatically_admitted_without_semantic_gate(self):
        report = self.admit()
        self.assertTrue(report["admitted"], report["failures"])
        self.assertFalse(report["human_semantic_gate"])
        self.assertEqual(report["admission_mode"], "automatic_mechanical_only")
        self.assertEqual(report["glm_changed_paths"], ["memory/fixture.md"])
        self.assertEqual(report["public_feedback"]["sha256"], self.state["expected_scratch_hashes"]["feedback.json"])

    def test_unlogged_manual_change_is_not_admitted(self):
        (self.session / "adapter/memory/fixture.md").write_text("# Manual mutation after GLM\n")
        report = self.admit()
        self.assertFalse(report["admitted"])
        self.assertIn("Candidate does not match successful GLM-requested operations", report["failures"])

    def test_unlogged_feedback_or_protected_input_change_is_not_admitted(self):
        altered = feedback()
        altered["episodes"][0]["self_assessment"] = "Manually changed fixture"
        L.save(self.session / "scratch/feedback.json", altered)
        (self.session / "evidence/README.md").write_text("Unauthorized edit")
        report = self.admit()
        self.assertFalse(report["admitted"])
        self.assertIn("Protected input changed", report["failures"])
        self.assertIn("Public feedback does not match successful GLM-requested file operations", report["failures"])


@unittest.skipUnless(os.environ.get("OFFICEQA_RUN_NATIVE_LEARNER_TEST") == "1",
                     "Native OS integration is opt-in; synthetic model transport, no paid calls")
class NativeIntegration(unittest.TestCase):
    def test_native_sandbox_large_lossless_reads_and_automatic_admission(self):
        import client
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            fixture = setup_session(base / "fixture")
            # Bigger than native serialized stdout's original 30,000-byte cap.
            (fixture / "evidence/001/trajectory.jsonl").write_text(
                json.dumps({"role": "assistant", "content": "Synthetic observation. " * 1800}) + "\n")
            reads = [toolcall("read_file", {"area": "evidence", "path": path}, i)
                     for i, path in enumerate(L.required_evidence_files(fixture / "evidence"), 1)]
            transport = FakeTransport([
                {"role": "assistant", "content": "Read synthetic evidence.", "tool_calls": reads},
                {"role": "assistant", "content": "Finish the large read.", "tool_calls": [
                    toolcall("read_file", {"area": "evidence", "path": "001/trajectory.jsonl", "offset": 30000}, 16)]},
                {"role": "assistant", "content": "Fixture self-assessment without gold.", "tool_calls": [
                    toolcall("write_file", {"area": "scratch", "path": "feedback.json", "content": json.dumps(feedback())}, 17),
                    toolcall("write_file", {"area": "adapter", "path": "memory/synthetic-check.md", "content": "# Synthetic reusable check\nCorrectness unknown without labels.\n"}, 18)]},
                {"role": "assistant", "content": "Synthetic test complete; no paid call."}])
            with patch.object(client, "complete", transport.complete):
                result = L.run_learning("offline-fixture-key", L.GENERIC_SOURCE, fixture / "evidence", base / "output", 1)
            self.assertEqual(result["status"], "admitted", result)
            self.assertEqual(result["model_calls"], 4)
            self.assertEqual(result["successful_tools"], 18)
            admission = json.loads(Path(result["admission"]).read_text())
            self.assertTrue(all(item["complete"] for item in admission["evidence_delivery_coverage"].values()))
            self.assertGreater(admission["evidence_delivery_coverage"]["001/trajectory.jsonl"]["characters"], 30000)
            self.assertTrue(admission["operation_provenance_matches"])
            prepared = json.loads(Path(result["native_prepare_result"]).read_text())
            self.assertEqual(prepared["status"], "prepared")
            self.assertTrue(prepared["preflight"]["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
