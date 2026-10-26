"""AdapterHost (the ported CL-Bench host) driven by a fake run_action_turn: the adapter's context is the system
prompt, its tools are offered beside the task's, wm calls go through Session with the cap, turns are recorded in the
native shape, the environment's answers land in the records and the conversation, cost.jsonl is written. With an
adapter that declares a context tool (the reharness template) the context is computed once per instance, from its
first prompt, and the tool's result is kept in context_info; a context tool that fails leaves ADAPTER.md alone. The
venue agents record the context tool's result in RunOutput.extra['context_tool'] only when the runner reports one (a
plain-Python test, needing neither clbench nor litellm)."""
from pathlib import Path
import json
import os
import shutil
import sys
import tempfile
import types
import importlib.util
import unittest

TASK = Path(__file__).resolve().parents[2]
FRAMEWORK = TASK.parents[1]
CLBENCH = Path(os.environ.get("MWM_CLBENCH", str(Path.home() / "code/clbench")))
TEMPLATE = FRAMEWORK / "hermit/algorithms/templates/mwm"
REHARNESS = FRAMEWORK / "hermit/algorithms/templates/reharness"


@unittest.skipUnless((CLBENCH / "src/interface.py").is_file() and importlib.util.find_spec("litellm") is not None,
                     "clbench checkout and its environment (litellm) not available: run under <clbench>/.venv/bin/python")
class HostDrivenByAFakeTurn(unittest.TestCase):
    def setUp(self):
        for p in (str(CLBENCH), str(TASK / "utils" / "agent")):
            if p not in sys.path:
                sys.path.insert(0, p)
        import host
        from src.interface import Observation, Query
        from src.systems.utils import native_tools
        self.host, self.native_tools, self.Query, self.Observation = host, native_tools, Query, Observation
        self.tmp = Path(tempfile.mkdtemp())
        self.adapter = self.tmp / "adapter"
        shutil.copytree(TEMPLATE, self.adapter, ignore=shutil.ignore_patterns("__pycache__"))
        (self.adapter / "ADAPTER.md").write_text("CONTEXT FOR THE HOST\n")
        from hermit.algorithms.evolvers.mwm import deployment as _dep
        deployment = _dep.host_settings(TASK / "utils/task-info/mwm")
        from hermit import Session
        session = Session(self.adapter, evidence=self.tmp / "evidence", deployment=deployment)
        self.system = host.AdapterHost(session, model="m", workspace=self.tmp / "ws", venue="database")
        self.seen = []
        self.real_turn = native_tools.run_action_turn

    def tearDown(self):
        self.native_tools.run_action_turn = self.real_turn
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_turn(self, replies):
        """A stand-in for clbench's run_action_turn: plays scripted assistant replies, serves extra tools through
        the handler as the real one does, ends on the first environment tool call."""
        outer = self

        def run_action_turn(model, prefix, context, env_tools, schema, *, extra_tools=(), extra_handler=None,
                            on_usage=None, on_reply=None, **kw):
            outer.seen.append({"prefix": list(prefix), "tools": [t["function"]["name"] for t in [*env_tools, *extra_tools]]})
            env_names = {t["function"]["name"] for t in env_tools}
            answered, pending = {}, None
            for reply in replies:
                context.append(reply)
                on_usage(types.SimpleNamespace(input_tokens=10, output_tokens=5, cached_input_tokens=2))
                on_reply(reply, "thought")
                for c in reply.get("tool_calls") or []:
                    name, args = c["function"]["name"], json.loads(c["function"]["arguments"])
                    if name in env_names:
                        pending = c
                    else:
                        answered[c["id"]] = extra_handler(name, args)
                        context.append(outer.native_tools.tool_result_message(c["id"], answered[c["id"]]))
                if pending:
                    break
            return outer.native_tools.TurnResult(action={"query": "SELECT 1"}, pending_call_id=pending["id"] if pending else None,
                                                 assistant_record="", reasoning=None, extra_calls=[], answered=answered)
        self.native_tools.run_action_turn = run_action_turn

    def test_one_question(self):
        call = lambda cid, name, args: {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        self.fake_turn([
            {"role": "assistant", "content": "", "tool_calls": [call("w1", "wm", {"name": "list", "args": {}})]},
            {"role": "assistant", "content": "", "tool_calls": [call("q1", "QUERY", {"query": "SELECT 1"})]},
        ])
        env_tools = [{"type": "function", "function": {"name": "QUERY", "parameters": {"type": "object"}}},
                     {"type": "function", "function": {"name": "ANSWER", "parameters": {"type": "object"}}}]
        query = self.Query(prompt="Question 1", response_schema=None, instance_id="q-1", tools=env_tools,
                           metadata={"db_path": "/secret.db", "difficulty": "hard", "kept": 1, "question_num": 1, "question_id": "q-1"})
        response = self.system.respond(query)
        self.assertIsNone(self.system.context_info)            # the mwm adapter declares no context tool
        self.assertEqual(response.action, {"query": "SELECT 1"})
        self.assertEqual(self.seen[0]["prefix"], [{"role": "system", "content": "CONTEXT FOR THE HOST\n"}])
        self.assertEqual(self.seen[0]["tools"], ["QUERY", "ANSWER", "wm"])
        # the wm call went through Session to the template's `list`
        wm_turn, env_turn = self.system._turns
        self.assertTrue(wm_turn["channel"] and wm_turn["response"]["action"]["name"] == "list")
        self.assertIsNotNone(wm_turn["observation"]) and self.assertIn("content", wm_turn["observation"])
        self.assertEqual(wm_turn["query"]["metadata"], {"kept": 1, "question_num": 1, "question_id": "q-1"})   # db_path and difficulty hidden
        self.assertEqual(env_turn["response"]["action"]["action"], "QUERY")
        self.assertEqual(self.system._wm_calls, 1)
        # the environment answers the pending call, then ends the question
        self.system.observe(self.Observation(content="rows: 1", instance_complete=False))
        self.assertEqual(env_turn["observation"], {"content": "rows: 1"})
        self.assertEqual(self.system._messages[-1], self.native_tools.tool_result_message("q1", "rows: 1"))
        self.system.observe(self.Observation(content="correct", instance_complete=True))
        self.assertEqual(len(self.system._pending), 2) and self.assertEqual(self.system._turns, [])
        cost = json.loads((self.tmp / "ws/cost.jsonl").read_text().splitlines()[-1])
        self.assertEqual((cost["role"], cost["input_tokens"], cost["output_tokens"], cost["cached_input_tokens"]), ("host", 20, 10, 4))
        # the MWM evidence writer turns the host's records into the learner's files
        from hermit.algorithms.evolvers.mwm.evidence import clbench as clbench_evidence, readme
        qs = clbench_evidence.group_questions(self.system._pending)
        self.assertEqual(len(qs), 1)
        paths = clbench_evidence.write_raw(qs, str(self.tmp / "evidence"), 1)
        entry = clbench_evidence.layer1(qs[0], paths[qs[0]["question_num"]])
        self.assertTrue((self.tmp / "evidence" / "block001").is_dir() and "/evidence/block001/" in entry)
        readme.write(self.tmp / "evidence", "EVIDENCE TEXT")
        self.assertTrue((self.tmp / "evidence/README.md").read_text().startswith("EVIDENCE TEXT\n\nSub-agents:"))

    def test_context_tool_once_per_instance(self):
        """The reharness adapter: the reflections retrieved for the instance's first prompt sit below ADAPTER.md in the
        system message of every turn of the instance (a later prompt retrieves nothing new), context_info names them,
        the reader is not among the model's tools, and the next instance retrieves afresh."""
        adapter = self.tmp / "reharness"
        shutil.copytree(REHARNESS, adapter, ignore=shutil.ignore_patterns("__pycache__"))
        if str(REHARNESS) not in sys.path:
            sys.path.insert(0, str(REHARNESS))
        from reharness_core import bank
        bank.write_index(adapter, {"k": 1, "retrieval": "bm25", "embedding_model": None, "dim": None})
        bank.save_reflection(adapter, bank.Reflection(id="m0", task="m0", step=1, query="how many products are in stock",
                                                      reflection="- count the rows of products"))
        bank.save_reflection(adapter, bank.Reflection(id="m1", task="m1", step=2, query="which supplier ships fastest",
                                                      reflection="- join suppliers with shipments"))
        from hermit import Session
        system = self.host.AdapterHost(Session(adapter), model="m", workspace=self.tmp / "ws2", venue="database")
        system.reset()
        self.assertIsNone(system.context_info)
        call = lambda cid, name, args: {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        self.fake_turn([{"role": "assistant", "content": "", "tool_calls": [call("q1", "QUERY", {"query": "SELECT 1"})]}])
        env_tools = [{"type": "function", "function": {"name": "QUERY", "parameters": {"type": "object"}}}]
        query = lambda prompt, iid: self.Query(prompt=prompt, response_schema=None, instance_id=iid, tools=env_tools, metadata={})
        system.respond(query("Question 1: how many products are in stock?", "q-1"))
        prefix = self.seen[-1]["prefix"]
        self.assertEqual(len(prefix), 1)
        self.assertTrue(prefix[0]["content"].startswith((adapter / "ADAPTER.md").read_text()))
        self.assertIn("[RANKED REFLECTIONS]\n[RANK 1 OF 1 REFLECTION]\n- count the rows of products", prefix[0]["content"])
        self.assertEqual(self.seen[-1]["tools"], ["QUERY"])                                 # the reader is the harness's
        self.assertEqual([r["id"] for r in system.context_info["retrieved"]], ["m0"])
        self.assertEqual(system.context_info["query"], "Question 1: how many products are in stock?")
        # the next turn of the same question keeps the context, though its prompt would retrieve the other reflection
        system.observe(self.Observation(content="rows: 1", instance_complete=False))
        system.respond(query("which supplier ships fastest", "q-1"))
        self.assertEqual(self.seen[-1]["prefix"], prefix)
        # a new question retrieves for its own first prompt
        system.observe(self.Observation(content="correct", instance_complete=True))
        system.respond(query("Question 2: which supplier ships fastest?", "q-2"))
        self.assertIn("- join suppliers with shipments", self.seen[-1]["prefix"][0]["content"])
        self.assertEqual([r["id"] for r in system.context_info["retrieved"]], ["m1"])

    def test_context_tool_failure_degrades_to_adapter_md(self):
        """A reader that fails leaves ADAPTER.md alone as the system prompt; context_info records the error, so the
        Evolver revises nothing for the instance and the stream goes on."""
        adapter = self.tmp / "reharness"
        shutil.copytree(REHARNESS, adapter, ignore=shutil.ignore_patterns("__pycache__"))
        from hermit import Session
        session = Session(adapter)
        real = session.context

        def failing(query=None, task=None):            # the fallback `context()` without a query must still work
            if query is not None or task is not None:
                raise RuntimeError("embeddings HTTP 502 from http://127.0.0.1:1/x: upstream down")
            return real()

        session.context = failing
        system = self.host.AdapterHost(session, model="m", workspace=self.tmp / "ws3", venue="database",
                                       task_row={"id": "q-1"})
        system.reset()
        call = lambda cid, name, args: {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        self.fake_turn([{"role": "assistant", "content": "", "tool_calls": [call("q1", "QUERY", {"query": "SELECT 1"})]}])
        env_tools = [{"type": "function", "function": {"name": "QUERY", "parameters": {"type": "object"}}}]
        system.respond(self.Query(prompt="Question 1", response_schema=None, instance_id="q-1", tools=env_tools, metadata={}))
        self.assertEqual(self.seen[-1]["prefix"], [{"role": "system", "content": (adapter / "ADAPTER.md").read_text()}])
        self.assertEqual(system.context_info["retrieved"], [])
        self.assertTrue(system.context_info["retrieval"].startswith("error: RuntimeError: embeddings HTTP 502"))
        self.assertEqual(system.context_info["query"], "Question 1")

    def test_wm_cap(self):
        self.system._wm_calls = self.host.CALLS_PER_EPISODE
        self.assertEqual(self.system._adapter_tool("wm", {"name": "list", "args": {}}), self.host.CAP_NOTICE)
        self.assertEqual(self.host.wm_text({"text": "page", "eof": False, "next_offset": 10, "path": "p"}),
                         'page\n[More text available; continue when useful: wm(name="read", args={"path": "p", "offset": 10, "limit": 4000})]')



class AgentRecordsTheContextTool(unittest.TestCase):
    """agent.py of both venues: the runner's `context_tool` (the host's context_info) goes into RunOutput.extra as
    the Trainer keeps it (pipeline_extra); a runner that reports null, or none, records nothing."""
    DB = {"answer": "42", "success": True, "verdict": "CORRECT", "queries_used": 1, "drifted": False, "question_num": 1,
          "entry": "e", "tokens": {"input_tokens": 3, "output_tokens": 2}}
    POKER = {"delta": 1.5, "reward_bb": 1.5, "settled_without_host": [], "settled_reward_bb": 0.0, "hand_num": 1,
             "entry": "e", "tokens": {}}
    CT = {"text": "[RANKED REFLECTIONS]...", "retrieved": [{"id": "m0"}], "query": "q", "k": 1, "retrieval": "bm25",
          "bank_size": 1}

    @staticmethod
    def load(venue):
        # both venues' files are agent.py and put their own directory on sys.path: load each under its own name
        spec = importlib.util.spec_from_file_location(f"{venue}_agent", FRAMEWORK / "examples" / venue / "utils/agent/agent.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def check(self, venue, cls, base, output_key):
        module = self.load(venue)
        agent = getattr(module, cls)({})
        for ct in (None, self.CT):
            module.run_host = lambda runner, task, adapter, params, ct=ct: ({**base, "context_tool": ct}, Path("/nowhere"), 0.0)
            out = agent.run({"id": "t"}, Path("/nowhere/adapter"))
            self.assertEqual(out.output, base[output_key])
            self.assertEqual("context_tool" in out.extra, ct is not None)
            if ct is not None:
                self.assertEqual(out.extra["context_tool"], self.CT)
        module.run_host = lambda runner, task, adapter, params: (dict(base), Path("/nowhere"), 0.0)   # an older runner
        self.assertNotIn("context_tool", agent.run({"id": "t"}, Path("/nowhere/adapter")).extra)

    def test_database(self):
        self.check("clbench_database", "CLBenchDatabaseAgent", self.DB, "answer")

    def test_poker(self):
        self.check("clbench_poker", "CLBenchPokerAgent", self.POKER, "delta")


if __name__ == "__main__":
    unittest.main()
