"""AdapterHost (the ported CL-Bench host) driven by a fake run_action_turn: the adapter's context is the system
prompt, its tools are offered beside the task's, wm calls go through Session with the cap, turns are recorded in the
native shape, the environment's answers land in the records and the conversation, cost.jsonl is written."""
from pathlib import Path
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

TASK = Path(__file__).resolve().parents[2]
FRAMEWORK = TASK.parents[1]
CLBENCH = Path(os.environ.get("MWM_CLBENCH", str(Path.home() / "code/clbench")))
TEMPLATE = FRAMEWORK / "hermit/algorithms/templates/mwm"


@unittest.skipUnless((CLBENCH / "src/interface.py").is_file(), "clbench checkout not available")
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

    def test_wm_cap(self):
        self.system._wm_calls = self.host.CALLS_PER_EPISODE
        self.assertEqual(self.system._adapter_tool("wm", {"name": "list", "args": {}}), self.host.CAP_NOTICE)
        self.assertEqual(self.host.wm_text({"text": "page", "eof": False, "next_offset": 10, "path": "p"}),
                         'page\n[More text available; continue when useful: wm(name="read", args={"path": "p", "offset": 10, "limit": 4000})]')


if __name__ == "__main__":
    unittest.main()
