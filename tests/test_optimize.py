"""Optimizer Evolvers (GEPA, SkillOpt) end to end without a model or a paid call.

The real upstream optimizers run here: gepa.optimize() and SkillOpt's
ReflACTTrainer, against the deterministic KeywordEnvironment, with a local
OpenAI-compatible server answering their prompts.
"""
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

from harnessadapter.evolution.definition import EvolverDefinition
from harnessadapter.optimize.environment import Rollout, Task, check_rollouts
from harnessadapter.optimize.llm import ChatLM
from harnessadapter.optimize.plugin import is_optimizer, load_optimizer
from harnessadapter.optimize.session import GuardedEnvironment, evaluate_adapters, optimize
from harnessadapter.optimize.surface import CandidateStore, TextSurface
from harnessadapter.optimize.testing import FakeChatServer, KeywordEnvironment
from harnessadapter.paths import ARCHITECTURES

PROMPT_ONLY = ARCHITECTURES / "prompt_only"
HAS_GEPA = importlib.util.find_spec("gepa") is not None
HAS_SKILLOPT = importlib.util.find_spec("skillopt") is not None


def fake_lm(server):
    os.environ.setdefault("HARNESSADAPTER_FAKE_KEY", "fake")
    return ChatLM("scripted", base_url=server.base_url, api_key_env="HARNESSADAPTER_FAKE_KEY", retries=1)


class SurfaceTests(unittest.TestCase):
    def test_default_surface_is_the_context_file_and_materializes_a_loadable_adapter(self):
        surface = TextSurface(PROMPT_ONLY)
        self.assertEqual(surface.components, ["ADAPTER.md"])
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore(surface, Path(d))
            first = store.path({"ADAPTER.md": "# Rules\n- one\n"})
            self.assertEqual(store.path({"ADAPTER.md": "# Rules\n- one\n"}), first)
            self.assertEqual((first / "ADAPTER.md").read_text(), "# Rules\n- one\n")
            self.assertTrue((first / "adapter.json").is_file())
            self.assertNotEqual(store.path({"ADAPTER.md": "other"}), first)
            self.assertEqual((PROMPT_ONLY / "ADAPTER.md").read_text().splitlines()[0], "# Adapter context")

    def test_surface_refuses_manifest_and_foreign_components(self):
        with self.assertRaises(ValueError):
            TextSurface(PROMPT_ONLY, ["adapter.json"])
        with self.assertRaises(ValueError):
            TextSurface(PROMPT_ONLY).check({"README.md": "x"})


class EnvironmentContractTests(unittest.TestCase):
    def test_guard_resolves_tasks_by_id_so_test_cannot_be_relabeled(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            guarded = GuardedEnvironment(env, Path(d) / "log.jsonl")
            test_task = env.tasks("test")[0]
            disguised = Task(id=test_task.id, split="train", prompt=test_task.prompt, kind=test_task.kind)
            with self.assertRaises(PermissionError):
                guarded.evaluate(None, [disguised], label="x")
            val_task = env.tasks("val")[0]
            relabeled = Task(id=val_task.id, split="train", prompt=val_task.prompt, kind=val_task.kind)
            [rollout] = guarded.evaluate(None, [relabeled], label="y")
            self.assertEqual(rollout.split, "val")                     # the registered task was evaluated
            self.assertNotIn("RULE_", rollout.feedback)
            self.assertFalse(hasattr(guarded, "inner"))
        # The environment itself decides from the registered split, too.
        [direct] = env.evaluate(None, [relabeled], label="z")
        self.assertNotIn("RULE_", direct.feedback)

    def test_evaluate_adapters_leaves_infra_losses_out_of_the_mean(self):
        class Flaky(KeywordEnvironment):
            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                rollouts[0].infra, rollouts[0].status, rollouts[0].score = True, "transport_lost", 0.0
                return rollouts
        env = Flaky()
        with tempfile.TemporaryDirectory() as d:
            store = CandidateStore(TextSurface(PROMPT_ONLY), Path(d) / "c")
            good = store.path({"ADAPTER.md": "RULE_ALPHA RULE_BETA RULE_GAMMA RULE_DELTA"})
            results = evaluate_adapters(env, {"good": good, "none": None}, "test", output=Path(d), infra_pause=0)
            saved = json.loads((Path(d) / "test-scores.json").read_text())
        result = results["good"]
        self.assertEqual(result["mean"], 1.0)
        self.assertEqual(result["runs"][0]["scored"], 3)
        self.assertEqual(result["runs"][0]["infra_lost"], [env.tasks("test")[0].id])
        self.assertEqual(saved["paired_tasks"], 3)                     # tasks every adapter scored
        self.assertEqual((results["good"]["paired_mean"], results["none"]["paired_mean"]), (1.0, 0.0))
        self.assertEqual(len([c for c in env.calls if "infra-rerun" in c["label"]]), 4)   # 2 reruns x 2 adapters

    def test_guard_hides_the_test_split(self):
        with tempfile.TemporaryDirectory() as d:
            guarded = GuardedEnvironment(KeywordEnvironment(), Path(d) / "log.jsonl")
            with self.assertRaises(PermissionError):
                guarded.tasks("test")
            test_task = KeywordEnvironment().tasks("test")[0]
            with self.assertRaises(PermissionError):
                guarded.evaluate(None, [test_task], label="x")
            self.assertEqual(len(guarded.evaluate(None, guarded.tasks("val")[:2], label="val")), 2)

    def test_rollouts_must_match_tasks(self):
        task = Task(id="a", split="train", prompt="p")
        with self.assertRaises(ValueError):
            check_rollouts([task], [Rollout(task_id="b", split="train", score=1.0)])
        with self.assertRaises(ValueError):
            check_rollouts([task], [Rollout(task_id="a", split="train", score=2.0)])

    def test_val_feedback_never_names_the_answer(self):
        env = KeywordEnvironment()
        train = env.evaluate(None, env.tasks("train")[:1], label="t")[0]
        val = env.evaluate(None, env.tasks("val")[:1], label="v")[0]
        self.assertIn("requires RULE_", train.feedback)
        self.assertNotIn("RULE_", val.feedback)


class ResumeTests(unittest.TestCase):
    def test_resume_is_refused_when_the_split_assignment_changed(self):
        from harnessadapter.optimize.session import check_resume_splits, split_fingerprint
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "harnessadapter-splits.json").write_text(json.dumps(split_fingerprint(KeywordEnvironment())))
            check_resume_splits(state, split_fingerprint(KeywordEnvironment()))
            with self.assertRaisesRegex(ValueError, "assignment differs"):
                check_resume_splits(state, split_fingerprint(KeywordEnvironment(train=6, val=6)))
            (state / "harnessadapter-splits.json").unlink()
            with self.assertRaisesRegex(ValueError, "records no split"):
                check_resume_splits(state, split_fingerprint(KeywordEnvironment()))


class RegistryTests(unittest.TestCase):
    def test_bundled_optimizers_and_session_evolver_separation(self):
        self.assertTrue(is_optimizer("gepa"))
        self.assertTrue(is_optimizer("skillopt"))
        self.assertFalse(is_optimizer("basic"))
        with self.assertRaisesRegex(ValueError, "harnessadapter optimize"):
            EvolverDefinition("gepa")
        with self.assertRaisesRegex(ValueError, "harnessadapter evolve"):
            load_optimizer("basic")
        with self.assertRaisesRegex(ValueError, "Unknown gepa settings"):
            load_optimizer("gepa").merged({"no_such_setting": 1})


class ChatLMTests(unittest.TestCase):
    def test_gateway_page_with_status_200_is_retried(self):
        answers = iter(["<html>gateway</html>", None])
        with FakeChatServer() as server:
            original = server.httpd.RequestHandlerClass.do_POST

            def flaky(handler):
                if next(answers, None):
                    body = b"<html>gateway</html>"
                    handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
                    handler.send_response(200)
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)
                    return
                original(handler)
            server.httpd.RequestHandlerClass.do_POST = flaky
            lm = fake_lm(server)
            lm.retries = 2
            import harnessadapter.optimize.llm as llm_module
            sleep = llm_module.time.sleep
            llm_module.time.sleep = lambda s: None
            try:
                text = lm("I provided an assistant with the following instructions\n```\nx\n```")
            finally:
                llm_module.time.sleep = sleep
                server.httpd.RequestHandlerClass.do_POST = original
        self.assertTrue(text.startswith("```"))
        self.assertEqual(lm.usage["calls"], 1)
        self.assertEqual(text.strip().finish_reason, "stop")        # GEPA's LMOutput contract


@unittest.skipUnless(HAS_GEPA, "gepa is not installed")
class GEPATests(unittest.TestCase):
    def test_no_evidence_means_no_proposal(self):
        from gepa import EvaluationBatch
        from harnessadapter.optimize.plugin import OptimizeContext
        from harnessadapter.optimize.plugins.gepa_plugin import HarnessAdapterGEPAAdapter
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            context = OptimizeContext(surface=TextSurface(PROMPT_ONLY), environment=env, lm=None, state_dir=Path(d),
                                      work_dir=Path(d), settings={}, log=lambda m: None)
            adapter = HarnessAdapterGEPAAdapter(context, env.tasks("train"), 1000)
            lost = [Rollout(task_id=t.id, split="train", score=0.0, infra=True, status="transport_lost")
                    for t in env.tasks("train")[:3]]
            batch = EvaluationBatch(outputs=[{}] * 3, scores=[0.0] * 3, trajectories=lost)
            self.assertEqual(adapter.make_reflective_dataset({}, batch, ["ADAPTER.md"]), {"ADAPTER.md": []})
            mixed = lost[:2] + [Rollout(task_id=env.tasks("train")[2].id, split="train", score=1.0)]
            batch = EvaluationBatch(outputs=[{}] * 3, scores=[0.0, 0.0, 1.0], trajectories=mixed)
            self.assertEqual(adapter.make_reflective_dataset({}, batch, ["ADAPTER.md"]), {"ADAPTER.md": []})
            mixed[2].score = 0.0
            self.assertEqual(len(adapter.make_reflective_dataset({}, batch, ["ADAPTER.md"])["ADAPTER.md"]), 1)

    def test_gepa_finds_the_rules_and_leaves_test_untouched(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server, redirect_stdout(io.StringIO()):
            result = optimize(PROMPT_ONLY, env, "gepa", lm=fake_lm(server), output=Path(d),
                              settings={"max_metric_calls": 40}, log=lambda m: None)
            session = Path(result["session"])
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual(result["plugin"]["seed_val_score"], 0.0)
            self.assertEqual(result["plugin"]["selected_val_score"], 1.0)
            text = (session / "candidate/ADAPTER.md").read_text()
            for kind in ("ALPHA", "BETA", "GAMMA", "DELTA"):
                self.assertIn(f"RULE_{kind}", text)
            self.assertTrue((session / "training_state/gepa_state.bin").is_file())
            self.assertTrue((session / "candidate/.git").is_dir())
            self.assertIn("ADAPTER.md", (PROMPT_ONLY / "adapter.json").read_text())
            self.assertNotIn("RULE_", (PROMPT_ONLY / "ADAPTER.md").read_text())
            test_ids = {t.id for t in env.tasks("test")}
            self.assertFalse(any(set(c["tasks"]) & test_ids for c in env.calls))
            prompts = "\n".join(r["messages"][0]["content"] for r in server.requests)
            self.assertIn("requires RULE_", prompts)          # train feedback reaches the reflection
            self.assertIn("[action] read context", prompts)   # so does the agent's trajectory
            scores = evaluate_adapters(env, {"w0": PROMPT_ONLY, "gepa": session / "candidate"}, "test", output=Path(d) / "eval")
            self.assertEqual(scores["w0"]["mean"], 0.0)
            self.assertEqual(scores["gepa"]["mean"], 1.0)

    def test_gepa_resumes_from_its_training_state(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server, redirect_stdout(io.StringIO()):
            first = optimize(PROMPT_ONLY, env, "gepa", lm=fake_lm(server), output=Path(d), settings={"max_metric_calls": 12},
                             log=lambda m: None)
            state = Path(first["session"]) / "training_state"
            second = optimize(PROMPT_ONLY, env, "gepa", lm=fake_lm(server), output=Path(d), settings={"max_metric_calls": 60},
                              training_state=state, log=lambda m: None)
            self.assertEqual(second["resumed_from"], str(state))
            self.assertGreaterEqual(second["plugin"]["total_metric_calls"], first["plugin"]["total_metric_calls"])
            self.assertEqual(second["plugin"]["selected_val_score"], 1.0)


@unittest.skipUnless(HAS_SKILLOPT, "skillopt is not installed")
class SkillOptTests(unittest.TestCase):
    def test_skillopt_trainer_accepts_gated_edits(self):
        env = KeywordEnvironment()
        settings = {"num_epochs": 2, "batch_size": 4, "minibatch_size": 2, "merge_batch_size": 2,
                    "analyst_workers": 2, "slow_update_samples": 4}
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server, redirect_stdout(io.StringIO()):
            result = optimize(PROMPT_ONLY, env, "skillopt", lm=fake_lm(server), output=Path(d), settings=settings,
                              log=lambda m: None)
            session = Path(result["session"])
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual(result["plugin"]["seed_val_score"], 0.0)
            self.assertEqual(result["plugin"]["selected_val_score"], 1.0)
            self.assertGreaterEqual(result["plugin"]["total_accepts"], 1)
            self.assertIn("RULE_", (session / "candidate/ADAPTER.md").read_text())
            conversations = list((session / "training_state/steps").rglob("conversation.json"))
            self.assertTrue(conversations)
            self.assertIn("cat AGENTS.md", conversations[0].read_text())
            self.assertGreater(result["optimizer_usage"]["calls"], 0)       # SkillOpt's own token tracker
            config = json.loads((session / "training_state/config.json").read_text())
            self.assertFalse(config["eval_test"])
            test_ids = {t.id for t in env.tasks("test")}
            self.assertFalse(any(set(c["tasks"]) & test_ids for c in env.calls))
            for request in server.requests:
                self.assertEqual(request["model"], "scripted")


@unittest.skipUnless(HAS_SKILLOPT, "skillopt is not installed")
class SkillOptPieceTests(unittest.TestCase):
    def _env_adapter(self, env, d):
        from harnessadapter.optimize.plugin import OptimizeContext, load_optimizer
        from harnessadapter.optimize.plugins.skillopt_plugin import make_env_adapter
        guarded = GuardedEnvironment(env, Path(d) / "log.jsonl", infra_pause=0)
        context = OptimizeContext(surface=TextSurface(PROMPT_ONLY), environment=guarded, lm=None, state_dir=Path(d),
                                  work_dir=Path(d) / "work", settings={}, log=lambda m: None)
        return make_env_adapter(context, load_optimizer("skillopt").settings, "ADAPTER.md")

    def test_rollout_resumes_paid_episodes_for_the_same_skill_only(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            adapter = self._env_adapter(env, d)
            items = [{"id": t.id} for t in env.tasks("train")[:4]]
            first = adapter.rollout(items[:2], "RULE_ALPHA", str(Path(d) / "step/rollout"))
            again = adapter.rollout(items, "RULE_ALPHA", str(Path(d) / "step/rollout"))
            self.assertEqual([r["id"] for r in again], [i["id"] for i in items])
            self.assertEqual(again[:2], first)
            self.assertEqual([c["tasks"] for c in env.calls], [[i["id"] for i in items[:2]], [i["id"] for i in items[2:]]])
            adapter.rollout(items, "RULE_BETA", str(Path(d) / "step/rollout"))          # another skill: all re-run
            self.assertEqual(len(env.calls[-1]["tasks"]), 4)
            self.assertIn("predicted_answer", again[0])

    def test_persistent_infra_loss_stops_instead_of_scoring_zero(self):
        class Down(KeywordEnvironment):
            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                for r in rollouts:
                    r.infra, r.status, r.score = True, "transport_lost", 0.0
                return rollouts
        env = Down()
        with tempfile.TemporaryDirectory() as d:
            adapter = self._env_adapter(env, d)
            with self.assertRaisesRegex(RuntimeError, "resume the session"):
                adapter.rollout([{"id": env.tasks("val")[0].id}], "x", str(Path(d) / "sel"))
            self.assertEqual(len(env.calls), 3)                      # the call and two session re-runs

    def test_resume_points_runtime_state_into_the_copied_state(self):
        from harnessadapter.optimize.plugins.skillopt_plugin import relocate_runtime_state
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "new/training_state"
            (state / "skills").mkdir(parents=True)
            (state / "runtime_state.json").write_text(json.dumps({
                "last_completed_step": 3, "current_score": 0.5,
                "current_skill_path": "/old/session/training_state/skills/skill_v0003.md",
                "best_skill_path": "/old/session/training_state/best_skill.md"}))
            relocate_runtime_state(state)
            runtime = json.loads((state / "runtime_state.json").read_text())
        self.assertEqual(runtime["current_skill_path"], str(state / "skills/skill_v0003.md"))
        self.assertEqual(runtime["best_skill_path"], str(state / "best_skill.md"))
        self.assertEqual(runtime["current_score"], 0.5)

    def test_trajectory_budget_keeps_both_ends(self):
        from harnessadapter.optimize.plugins.skillopt_plugin import budget_steps
        steps = [{"type": "tool_call", "cmd": f"step {i}", "obs": "x" * 500} for i in range(40)]
        self.assertEqual(budget_steps(steps, 0), steps)
        kept = budget_steps(steps, 5000)
        self.assertEqual(kept[0]["cmd"], "step 0")
        self.assertEqual(kept[-1]["cmd"], "step 39")
        self.assertIn("left out", next(s["content"] for s in kept if s.get("role") == "system"))
        self.assertLess(len(json.dumps(kept)), 6000)


@unittest.skipUnless(HAS_GEPA, "gepa is not installed")
class CommandLineTests(unittest.TestCase):
    def test_optimize_and_evaluate_commands(self):
        from harnessadapter.cli import main
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()) as out:
            code = main(["optimize", "prompt-only", "--evolver", "gepa", "--fake-optimizer",
                         "--environment", "harnessadapter.optimize.testing:KeywordEnvironment",
                         "--set", "max_metric_calls=30", "--output", d])
            self.assertEqual(code, 0)
            session = next(Path(d).glob("optimize-gepa-*"))
            code = main(["evaluate", "--environment", "harnessadapter.optimize.testing:KeywordEnvironment",
                         "--adapter", "none=", "--adapter", f"gepa={session / 'candidate'}", "--output", str(Path(d) / "eval")])
            self.assertEqual(code, 0)
            scores = json.loads((Path(d) / "eval/test-scores.json").read_text())["results"]
            self.assertEqual(scores["none"]["mean"], 0.0)
            self.assertEqual(scores["gepa"]["mean"], 1.0)
        self.assertIn("candidate_ready", out.getvalue())


if __name__ == "__main__":
    unittest.main()
