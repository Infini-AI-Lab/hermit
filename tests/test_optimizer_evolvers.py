"""Optimizer Evolvers (GEPA, SkillOpt, a third-party plugin) end to end
without a model or a paid call.

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
import sys
import tempfile
import unittest

from harnessadapter.environment import Rollout, Task, check_rollouts, evaluate_adapters, run_tasks
from harnessadapter.evolution.definition import EvolverDefinition
from harnessadapter.evolution.launch import compile_launch
from harnessadapter.evolution.llm import ChatLM
from harnessadapter.evolution.optimizer import (Context, TextSurface, check_resume, is_optimizer, load_optimizer,
                                                optimize, split_fingerprint)
from harnessadapter.paths import ARCHITECTURES
from harnessadapter.testing import FakeChatServer, KeywordEnvironment

PROMPT_ONLY = ARCHITECTURES / "prompt_only"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/greedy_reflection"
HAS_GEPA = importlib.util.find_spec("gepa") is not None
HAS_SKILLOPT = importlib.util.find_spec("skillopt") is not None
ALL_RULES = "RULE_ALPHA RULE_BETA RULE_GAMMA RULE_DELTA"


def fake_model(server):
    os.environ.setdefault("HARNESSADAPTER_FAKE_KEY", "fake")
    return ChatLM("scripted", base_url=server.base_url, api_key_env="HARNESSADAPTER_FAKE_KEY", retries=1)


def make_context(env, d, settings=None, model=None):
    d = Path(d)
    return Context(TextSurface(PROMPT_ONLY), env, model=model, settings=settings or {}, state_dir=d / "state",
                   work_dir=d / "work", record=d / "control/evaluations.jsonl", log=lambda m: None, pause=0)


def plugin_module(name):
    return sys.modules[load_optimizer(name).run.__module__]


class SurfaceTests(unittest.TestCase):
    def test_default_surface_is_the_context_file_and_writes_a_loadable_adapter(self):
        surface = TextSurface(PROMPT_ONLY)
        self.assertEqual(surface.components, ["ADAPTER.md"])
        with tempfile.TemporaryDirectory() as d:
            written = surface.write({"ADAPTER.md": "# Rules\n- one\n"}, Path(d) / "a")
            self.assertEqual((written / "ADAPTER.md").read_text(), "# Rules\n- one\n")
            self.assertTrue((written / "adapter.json").is_file())
            with self.assertRaises(ValueError):
                surface.write({"ADAPTER.md": "x"}, written)
        self.assertEqual((PROMPT_ONLY / "ADAPTER.md").read_text().splitlines()[0], "# Adapter context")

    def test_surface_refuses_manifest_and_foreign_components(self):
        with self.assertRaises(ValueError):
            TextSurface(PROMPT_ONLY, ["adapter.json"])
        with self.assertRaises(ValueError):
            TextSurface(PROMPT_ONLY).check({"README.md": "x"})


class ContextTests(unittest.TestCase):
    def test_evaluate_resolves_tasks_by_id_so_test_cannot_be_relabeled(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            context = make_context(env, d)
            self.assertFalse(hasattr(context, "test"))
            test_task = env.tasks("test")[0]
            with self.assertRaises(PermissionError):
                context.evaluate(context.seed, [test_task], label="x")
            disguised = Task(id=test_task.id, split="train", prompt=test_task.prompt, kind=test_task.kind)
            with self.assertRaises(PermissionError):
                context.evaluate(context.seed, [disguised], label="x")
            val_task = env.tasks("val")[0]
            relabeled = Task(id=val_task.id, split="train", prompt=val_task.prompt, kind=val_task.kind)
            [rollout] = context.evaluate(context.seed, [relabeled], label="y")
            self.assertEqual(rollout.split, "val")                      # the registered task was evaluated
            self.assertNotIn("RULE_", rollout.feedback)
            self.assertIsNone(rollout.record)                           # plugins get no record locations
            log = [json.loads(line) for line in (Path(d) / "control/evaluations.jsonl").read_text().splitlines()]
            self.assertEqual([entry["splits"] for entry in log], [["val"]])
        # The environment itself decides from the registered split, too.
        [direct] = env.evaluate(None, [relabeled], label="z")
        self.assertNotIn("RULE_", direct.feedback)

    def test_a_candidate_is_materialized_once_and_counted(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            context = make_context(env, d)
            good = {"ADAPTER.md": ALL_RULES}
            self.assertEqual([r.score for r in context.evaluate(good, context.val)], [1.0] * 4)
            context.evaluate(good, context.train[:2])
            self.assertEqual(len(list((Path(d) / "work/candidates").iterdir())), 1)
            self.assertEqual((context.calls, context.rollouts, context.infra_lost), (2, 6, 0))
            self.assertEqual(context.seed, TextSurface(PROMPT_ONLY).read())

    def test_infra_losses_are_rerun_then_reported(self):
        class Down(KeywordEnvironment):
            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                for r in rollouts:
                    r.infra, r.status, r.score = True, "transport_lost", 0.0
                return rollouts
        env = Down()
        with tempfile.TemporaryDirectory() as d:
            context = make_context(env, d)
            [rollout] = context.evaluate(context.seed, context.val[:1])
        self.assertTrue(rollout.infra)
        self.assertEqual(len(env.calls), 3)                              # the call and two re-runs
        self.assertEqual((context.rollouts, context.infra_lost), (3, 1))

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


class EvaluateTests(unittest.TestCase):
    def test_evaluate_adapters_leaves_infra_losses_out_of_the_mean(self):
        class Flaky(KeywordEnvironment):
            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                rollouts[0].infra, rollouts[0].status, rollouts[0].score = True, "transport_lost", 0.0
                return rollouts
        env = Flaky()
        with tempfile.TemporaryDirectory() as d:
            good = TextSurface(PROMPT_ONLY).write({"ADAPTER.md": ALL_RULES}, Path(d) / "good")
            results = evaluate_adapters(env, {"good": good, "none": None}, "test", output=Path(d), pause=0)
            saved = json.loads((Path(d) / "test-scores.json").read_text())
        self.assertEqual(results["good"]["mean"], 1.0)
        self.assertEqual(results["good"]["runs"][0]["scored"], 3)
        self.assertEqual(results["good"]["runs"][0]["infra_lost"], [env.tasks("test")[0].id])
        self.assertEqual(saved["paired_tasks"], 3)                      # tasks every adapter scored
        self.assertEqual((results["good"]["paired_mean"], results["none"]["paired_mean"]), (1.0, 0.0))
        self.assertEqual(len([c for c in env.calls if "infra-rerun" in c["label"]]), 4)   # 2 reruns x 2 adapters

    def test_run_tasks_recovers_a_transient_loss(self):
        class Once(KeywordEnvironment):
            lost = True

            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                if self.lost:
                    self.lost = False
                    rollouts[0].infra, rollouts[0].score = True, 0.0
                return rollouts
        env = Once()
        rollouts, extra = run_tasks(env, None, env.tasks("val")[:2], "x", pause=0)
        self.assertEqual((extra, [r.infra for r in rollouts]), (1, [False, False]))


class ResumeTests(unittest.TestCase):
    def test_resume_is_refused_when_the_split_assignment_changed(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "harnessadapter-splits.json").write_text(json.dumps(split_fingerprint(KeywordEnvironment())))
            check_resume(state, split_fingerprint(KeywordEnvironment()))
            with self.assertRaisesRegex(ValueError, "assignment differs"):
                check_resume(state, split_fingerprint(KeywordEnvironment(train=6, val=6)))
            with self.assertRaisesRegex(ValueError, "test split differs"):
                check_resume(state, split_fingerprint(KeywordEnvironment(test=5)))
            (state / "harnessadapter-splits.json").unlink()
            with self.assertRaisesRegex(ValueError, "records no split"):
                check_resume(state, split_fingerprint(KeywordEnvironment()))


class RegistryTests(unittest.TestCase):
    def test_bundled_optimizers_and_learner_evolver_separation(self):
        self.assertTrue(is_optimizer("gepa"))
        self.assertTrue(is_optimizer("skillopt"))
        self.assertTrue(is_optimizer(str(EXAMPLE)))
        self.assertFalse(is_optimizer("basic"))
        self.assertFalse(is_optimizer("no-such-evolver"))
        with self.assertRaisesRegex(ValueError, "optimizer Evolver"):
            EvolverDefinition("gepa")
        with self.assertRaisesRegex(ValueError, "not an optimizer"):
            load_optimizer("basic")
        with self.assertRaisesRegex(ValueError, "Unknown gepa settings"):
            load_optimizer("gepa").merged({"no_such_setting": 1})

    def test_a_file_plugin_must_stay_inside_its_definition(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "definition").mkdir()
            (Path(d) / "definition/evolver.toml").write_text('kind = "optimizer"\nplugin = "../outside.py:run"\n')
            (Path(d) / "outside.py").write_text("def run(context): pass\n")
            with self.assertRaisesRegex(ValueError, "inside"):
                load_optimizer(str(Path(d) / "definition"))


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
            model = fake_model(server)
            model.retries = 2
            import harnessadapter.evolution.llm as llm_module
            sleep = llm_module.time.sleep
            llm_module.time.sleep = lambda s: None
            try:
                text = model("I provided an assistant with the following instructions\n```\nx\n```")
            finally:
                llm_module.time.sleep = sleep
                server.httpd.RequestHandlerClass.do_POST = original
        self.assertTrue(text.startswith("```"))
        self.assertEqual(model.usage["calls"], 1)
        self.assertEqual(text.strip().finish_reason, "stop")            # GEPA's LMOutput contract

    def test_a_truncated_completion_is_never_a_proposal(self):
        with FakeChatServer(lambda prompt: "```\n# Rules\n```bash\nrg x\n```\n- cut") as server:
            server.finish_reason = "length"
            text = fake_model(server)("anything")
        self.assertEqual((str(text), text.finish_reason), ("", "length"))


class ThirdPartyPluginTests(unittest.TestCase):
    """examples/greedy_reflection: an optimizer Evolver outside the package."""

    def test_the_example_plugin_runs_as_an_evolver(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server:
            model = fake_model(server)
            result = optimize(PROMPT_ONLY, env, str(EXAMPLE), model=model, output=Path(d), log=lambda m: None)
            session = Path(result["session"])
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual((result["plugin"]["seed_val_score"], result["plugin"]["selected_val_score"]), (0.0, 1.0))
            self.assertEqual(result["changed_components"], ["ADAPTER.md"])
            self.assertEqual(Path(result["candidate"]), session / "adapter")
            self.assertTrue(result["candidate_commit"])
            self.assertIn("RULE_DELTA", (session / "adapter/ADAPTER.md").read_text())
            self.assertEqual(result["usage"]["calls"], len(server.requests))
            self.assertTrue(list((session / "control/model-calls").glob("*.json")))
            test_ids = {t.id for t in env.tasks("test")}
            self.assertFalse(any(set(c["tasks"]) & test_ids for c in env.calls))
            self.assertNotIn("RULE_", (PROMPT_ONLY / "ADAPTER.md").read_text())

    def test_a_plugin_error_leaves_a_failed_record(self):
        with tempfile.TemporaryDirectory() as d:
            definition = Path(d) / "broken"
            definition.mkdir()
            (definition / "evolver.toml").write_text('kind = "optimizer"\nplugin = "plugin.py:run"\n')
            (definition / "plugin.py").write_text("def run(context):\n    return {'ADAPTER.md': 'x'}\n")
            with self.assertRaisesRegex(TypeError, "not an Outcome"):
                optimize(PROMPT_ONLY, KeywordEnvironment(), str(definition), model=None, output=Path(d) / "out",
                         log=lambda m: None)
            [record] = (Path(d) / "out").glob("evolve-broken-*/control/result.json")
            self.assertEqual(json.loads(record.read_text())["status"], "failed")


@unittest.skipUnless(HAS_GEPA, "gepa is not installed")
class GEPATests(unittest.TestCase):
    def test_no_evidence_means_no_proposal(self):
        from gepa import EvaluationBatch
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d:
            adapter = plugin_module("gepa").HarnessAdapterGEPAAdapter(make_context(env, d), trace_chars=1000)
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
            result = optimize(PROMPT_ONLY, env, "gepa", model=fake_model(server), output=Path(d),
                              settings={"max_metric_calls": 40}, log=lambda m: None)
            session = Path(result["session"])
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual(result["plugin"]["seed_val_score"], 0.0)
            self.assertEqual(result["plugin"]["selected_val_score"], 1.0)
            text = (session / "adapter/ADAPTER.md").read_text()
            for kind in ("ALPHA", "BETA", "GAMMA", "DELTA"):
                self.assertIn(f"RULE_{kind}", text)
            self.assertTrue((session / "training_state/gepa_state.bin").is_file())
            self.assertTrue((session / "adapter/.git").is_dir())
            self.assertNotIn("RULE_", (PROMPT_ONLY / "ADAPTER.md").read_text())
            test_ids = {t.id for t in env.tasks("test")}
            self.assertFalse(any(set(c["tasks"]) & test_ids for c in env.calls))
            prompts = "\n".join(r["messages"][0]["content"] for r in server.requests)
            self.assertIn("requires RULE_", prompts)          # train feedback reaches the reflection
            self.assertIn("[action] read context", prompts)   # so does the agent's trajectory
            scores = evaluate_adapters(env, {"w0": PROMPT_ONLY, "gepa": session / "adapter"}, "test",
                                       output=Path(d) / "eval")
            self.assertEqual(scores["w0"]["mean"], 0.0)
            self.assertEqual(scores["gepa"]["mean"], 1.0)

    def test_gepa_resumes_from_its_training_state(self):
        env = KeywordEnvironment()
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server, redirect_stdout(io.StringIO()):
            first = optimize(PROMPT_ONLY, env, "gepa", model=fake_model(server), output=Path(d),
                             settings={"max_metric_calls": 12}, log=lambda m: None)
            state = Path(first["session"]) / "training_state"
            second = optimize(PROMPT_ONLY, env, "gepa", model=fake_model(server), output=Path(d),
                              settings={"max_metric_calls": 60}, training_state=state, log=lambda m: None)
            self.assertEqual(second["resumed_from"], str(state))
            self.assertGreaterEqual(second["plugin"]["total_metric_calls"], first["plugin"]["total_metric_calls"])
            self.assertEqual(second["plugin"]["selected_val_score"], 1.0)
            with self.assertRaisesRegex(ValueError, "assignment differs"):
                optimize(PROMPT_ONLY, KeywordEnvironment(train=6), "gepa", model=fake_model(server), output=Path(d),
                         training_state=state, log=lambda m: None)


@unittest.skipUnless(HAS_SKILLOPT, "skillopt is not installed")
class SkillOptTests(unittest.TestCase):
    def test_skillopt_trainer_accepts_gated_edits(self):
        env = KeywordEnvironment()
        settings = {"num_epochs": 2, "batch_size": 4, "minibatch_size": 2, "merge_batch_size": 2,
                    "analyst_workers": 2, "slow_update_samples": 4}
        with tempfile.TemporaryDirectory() as d, FakeChatServer() as server, redirect_stdout(io.StringIO()):
            model = fake_model(server)
            model.reasoning_effort = "medium"
            result = optimize(PROMPT_ONLY, env, "skillopt", model=model, output=Path(d), settings=settings,
                              log=lambda m: None)
            # SkillOpt's own client reached the model through the session's
            # endpoint: same settings, same ledger, no key in its environment.
            self.assertTrue(all(r.get("reasoning_effort") == "medium" for r in server.requests))
            self.assertEqual(model.usage["calls"], len(server.requests))
            self.assertEqual(os.environ.get("OPTIMIZER_OPENAI_COMPATIBLE_API_KEY"), "local-endpoint")
            session = Path(result["session"])
            self.assertTrue(list((session / "control/model-calls").glob("*.json")))
            self.assertEqual(result["status"], "candidate_ready")
            self.assertEqual(result["plugin"]["seed_val_score"], 0.0)
            self.assertEqual(result["plugin"]["selected_val_score"], 1.0)
            self.assertGreaterEqual(result["plugin"]["total_accepts"], 1)
            self.assertIn("RULE_", (session / "adapter/ADAPTER.md").read_text())
            conversations = list((session / "training_state/steps").rglob("conversation.json"))
            self.assertTrue(conversations)
            self.assertIn("cat AGENTS.md", conversations[0].read_text())
            self.assertGreater(result["usage"]["calls"], 0)
            config = json.loads((session / "training_state/config.json").read_text())
            self.assertTrue(config["eval_test"])                         # upstream's final val promotion stays on
            self.assertEqual(config["reasoning_effort"], "medium")       # records the effort actually sent
            self.assertIsNotNone(result["plugin"]["final_val_score"])
            test_ids = {t.id for t in env.tasks("test")}
            self.assertFalse(any(set(c["tasks"]) & test_ids for c in env.calls))
            for request in server.requests:
                self.assertEqual(request["model"], "scripted")


@unittest.skipUnless(HAS_SKILLOPT, "skillopt is not installed")
class SkillOptPieceTests(unittest.TestCase):
    def _env_adapter(self, env, d):
        context = make_context(env, d, settings=load_optimizer("skillopt").settings)
        return plugin_module("skillopt").make_env_adapter(context, "ADAPTER.md")

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

    def test_a_stop_keeps_finished_episodes_and_the_resume_runs_only_the_lost(self):
        class Partial(KeywordEnvironment):
            down = True

            def evaluate(self, adapter, tasks, *, label):
                rollouts = super().evaluate(adapter, tasks, label=label)
                if self.down:
                    rollouts[0].infra, rollouts[0].status, rollouts[0].score = True, "transport_lost", 0.0
                return rollouts
        env = Partial()
        items = [{"id": t.id} for t in env.tasks("val")[:3]]
        with tempfile.TemporaryDirectory() as d:
            adapter = self._env_adapter(env, d)
            with self.assertRaisesRegex(RuntimeError, "resume the session"):
                adapter.rollout(items, "x", str(Path(d) / "sel"))
            env.down = False
            results = adapter.rollout(items, "x", str(Path(d) / "sel"))
        self.assertEqual([r["id"] for r in results], [i["id"] for i in items])
        self.assertEqual(env.calls[-1]["tasks"], [items[0]["id"]])          # only the lost episode ran again

    def test_resume_points_runtime_state_into_the_copied_state(self):
        relocate = plugin_module("skillopt").relocate_runtime_state
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "new/training_state"
            (state / "skills").mkdir(parents=True)
            (state / "runtime_state.json").write_text(json.dumps({
                "last_completed_step": 3, "current_score": 0.5,
                "current_skill_path": "/old/session/training_state/skills/skill_v0003.md",
                "best_skill_path": "/old/session/training_state/best_skill.md"}))
            relocate(state)
            runtime = json.loads((state / "runtime_state.json").read_text())
        self.assertEqual(runtime["current_skill_path"], str(state / "skills/skill_v0003.md"))
        self.assertEqual(runtime["best_skill_path"], str(state / "best_skill.md"))
        self.assertEqual(runtime["current_score"], 0.5)


class CommandLineTests(unittest.TestCase):
    def _evolve(self, argv):
        from harnessadapter.cli import main
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as stopped:
            main(["evolve", *argv])
        return stopped.exception.code, out.getvalue()

    def test_evolve_runs_an_optimizer_evolver_and_evaluate_scores_it(self):
        from harnessadapter.cli import main
        with tempfile.TemporaryDirectory() as d:
            code, out = self._evolve(["prompt-only", "--evolver", str(EXAMPLE), "--scripted-model",
                                      "--environment", "harnessadapter.testing:KeywordEnvironment",
                                      "--set", "rounds=3", "--output", d])
            self.assertEqual(code, 0)
            self.assertIn("candidate_ready", out)
            session = next(Path(d).glob("evolve-greedy_reflection-*"))
            self.assertEqual(json.loads((session / "control/result.json").read_text())["settings"]["rounds"], 3)
            with redirect_stdout(io.StringIO()):
                code = main(["evaluate", "--environment", "harnessadapter.testing:KeywordEnvironment",
                             "--adapter", "none=", "--adapter", f"evolved={session / 'adapter'}",
                             "--output", str(Path(d) / "eval")])
            self.assertEqual(code, 0)
            scores = json.loads((Path(d) / "eval/test-scores.json").read_text())["results"]
            self.assertEqual((scores["none"]["mean"], scores["evolved"]["mean"]), (0.0, 1.0))

    def test_each_kind_refuses_the_other_kinds_options(self):
        code, _ = self._evolve(["prompt-only", "--evolver", "gepa", "--scripted-model",
                                "--environment", "harnessadapter.testing:KeywordEnvironment",
                                "--learning-material", "records"])
        self.assertEqual(code, 2)
        code, _ = self._evolve(["prompt-only", "--evolver", "gepa", "--scripted-model"])
        self.assertEqual(code, 2)                                          # no --environment
        code, _ = self._evolve(["prompt-only", "--evolver", "basic", "--learning-material", "records",
                                "--environment", "harnessadapter.testing:KeywordEnvironment"])
        self.assertEqual(code, 2)
        code, _ = self._evolve(["prompt-only", "--evolver", "basic"])
        self.assertEqual(code, 2)                                          # no --learning-material

    def test_launch_yaml_compiles_an_optimizer_run(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "gepa.yaml"
            config.write_text("action: evolve\narchitecture: prompt-only\nevolver: gepa\n"
                              "environment: env.py:make\nenvironment_config: env.yaml\nmodel: m\n"
                              "provider_base_url: http://localhost:1/v1\nprovider_env_key: KEY\n"
                              "settings: {max_metric_calls: 30, seed: 1}\ntraining_state: state\n")
            command = compile_launch(config)
            root = Path(d).resolve()
            self.assertEqual(command[command.index("--environment") + 1], f"{root / 'env.py'}:make")
            self.assertEqual(command[command.index("--environment-config") + 1], str(root / "env.yaml"))
            self.assertEqual(command[command.index("--training-state") + 1], str(root / "state"))
            self.assertIn("max_metric_calls=30", command)
            self.assertNotIn("--learning-material", command)
            config.write_text("action: evolve\narchitecture: prompt-only\nevolver: gepa\nmodel: m\n")
            with self.assertRaisesRegex(ValueError, "requires environment"):
                compile_launch(config)
            config.write_text("action: evolve\narchitecture: mwm\nlearning_material: r\nenvironment: x:y\n")
            with self.assertRaisesRegex(ValueError, "optimizer Evolvers"):
                compile_launch(config)
            for field in ("provider_name: p", "backend: codex", "learning_material: r", "timeout: 60"):
                config.write_text(f"action: evolve\narchitecture: prompt-only\nevolver: gepa\nenvironment: x:y\n{field}\n")
                with self.assertRaisesRegex(ValueError, "Not valid for an optimizer"):
                    compile_launch(config)

    def test_launch_settings_reach_the_session_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "run.yaml"
            config.write_text("action: evolve\narchitecture: prompt-only\nevolver: gepa\n"
                              "environment: harnessadapter.testing:KeywordEnvironment\n"
                              f"output: {d}\nsettings: {{max_metric_calls: 20, perfect_score: 1.0e-05,"
                              " reflection_prompt_template: \"\\u00e9t\\u00e9 \\U0001F600 <curr_param> <side_info>\"}\n")
            code, _ = self._evolve([*compile_launch(config)[1:], "--scripted-model"])
            self.assertEqual(code, 0)
            [record] = Path(d).glob("evolve-gepa-*/control/result.json")
            settings = json.loads(record.read_text())["settings"]
        self.assertEqual(settings["perfect_score"], 1e-05)
        self.assertEqual(settings["reflection_prompt_template"], "\u00e9t\u00e9 \U0001F600 <curr_param> <side_info>")
        self.assertEqual(settings["max_metric_calls"], 20)


if __name__ == "__main__":
    unittest.main()
