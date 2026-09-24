"""Lemma board architecture: tool semantics and host delivery, without Lean or model calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

from harnessadapter.adapter.loader import Adapter
from harnessadapter.hosts.codex import prepare as codex_prepare
from harnessadapter.paths import ARCHITECTURES as ARCH, resolve_architecture

BOARD = ARCH / "lemma_board"

PROBLEM = """import Mathlib

namespace Demo

/-- helper already here -/
private lemma one_le_two : (1 : ℕ) ≤ 2 := by norm_num

/-- The target. -/
@[category research open, AMS 5]
theorem target_thm : (1 : ℕ) + 1 = 2 := by
  sorry

end Demo
"""


class LemmaBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.board = root / "board"
        self.work = root / "A1"
        self.work.mkdir()
        (self.work / "Problem.lean").write_text(PROBLEM)
        self.env = {"LEMMA_BOARD_DIR": str(self.board), "LEMMA_BOARD_AGENT": "A1",
                    "LEMMA_BOARD_WORKSPACE": str(self.work), "LEMMA_BOARD_TARGET": "target_thm"}
        os.environ.update(self.env)
        self.adapter = Adapter(BOARD)

    def tearDown(self):
        for k in self.env:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def call(self, tool, **args):
        return asyncio.run(self.adapter.call(tool, args))

    def as_agent(self, agent):
        os.environ["LEMMA_BOARD_AGENT"] = agent

    def test_resolves_as_builtin(self):
        self.assertEqual(resolve_architecture("lemma-board"), BOARD)
        self.assertEqual(set(self.adapter.tools), {"board_list", "board_post", "board_claim", "board_done", "board_blocked", "board_pull"})

    def test_post_claim_submit_and_ownership(self):
        r = self.call("board_post", name="two_eq", statement="private lemma two_eq : (2 : ℕ) = 1 + 1")
        self.assertEqual(r["status"], "claimed")
        with self.assertRaises(ValueError):
            self.call("board_post", name="two_eq", statement="private lemma two_eq : (2 : ℕ) = 1 + 1")
        with self.assertRaises(ValueError):   # a statement must not carry a proof placeholder
            self.call("board_post", name="bad", statement="lemma bad : True := by sorry")
        self.as_agent("A2")
        with self.assertRaises(ValueError):   # owned by A1
            self.call("board_claim", name="two_eq")
        with self.assertRaises(ValueError):   # not the owner
            self.call("board_done", name="two_eq", proof="private lemma two_eq : (2 : ℕ) = 1 + 1 := by norm_num")
        self.as_agent("A1")
        with self.assertRaises(ValueError):   # sorry is refused before it reaches the verifier
            self.call("board_done", name="two_eq", proof="private lemma two_eq : (2 : ℕ) = 1 + 1 := by sorry")
        r = self.call("board_done", name="two_eq", proof="private lemma two_eq : (2 : ℕ) = 1 + 1 := by norm_num")
        self.assertEqual(r["status"], "submitted")
        self.assertTrue((self.board / "lemmas" / "two_eq.lean").read_text().startswith("private lemma two_eq"))
        board = json.loads((self.board / "board.json").read_text())
        self.assertEqual(board["lemmas"][0]["status"], "submitted")
        listing = self.call("board_list")
        self.assertEqual(listing["counts"]["submitted"], 1)
        self.assertEqual(listing["lemmas"][0]["owner"], "A1")

    def test_blocked_releases_and_pull_inserts_before_target(self):
        self.call("board_post", name="two_eq", statement="private lemma two_eq : (2 : ℕ) = 1 + 1")
        self.call("board_blocked", name="two_eq", reason="norm_num loops")
        self.as_agent("A2")
        self.assertEqual(self.call("board_claim", name="two_eq")["status"], "claimed")
        with self.assertRaises(ValueError):   # not done yet
            self.call("board_pull", name="two_eq")
        # the verifier accepts it (its job, simulated here)
        proof = "private lemma two_eq : (2 : ℕ) = 1 + 1 := by norm_num\n"
        (self.board / "verified" / "two_eq.lean").write_text(proof)
        board = json.loads((self.board / "board.json").read_text())
        board["lemmas"][0].update(status="done", done_time=1.0)
        (self.board / "board.json").write_text(json.dumps(board))
        self.as_agent("A1")
        r = self.call("board_pull", name="two_eq")
        self.assertEqual(r["inserted"], ["two_eq"])
        src = (self.work / "Problem.lean").read_text()
        self.assertLess(src.index("private lemma two_eq"), src.index("/-- The target. -/"))
        self.assertGreater(src.index("private lemma two_eq"), src.index("one_le_two"))
        self.assertEqual(src.count("private lemma two_eq"), 1)
        r = self.call("board_pull", name="two_eq")
        self.assertEqual(r["inserted"], [])   # already declared

    def test_pull_brings_earlier_dependencies_in_order(self):
        board = {"lemmas": []}
        for i, (nm, proof) in enumerate([("a_eq", "private lemma a_eq : (1 : ℕ) = 1 := rfl\n"),
                                         ("b_eq", "private lemma b_eq : (1 : ℕ) = 1 := a_eq\n"),
                                         ("c_eq", "private lemma c_eq : (1 : ℕ) = 1 := b_eq.trans a_eq\n")]):
            (self.board / "verified").mkdir(parents=True, exist_ok=True)
            (self.board / "verified" / f"{nm}.lean").write_text(proof)
            board["lemmas"].append({"name": nm, "statement": proof.split(":=")[0], "status": "done", "owner": "A2",
                                    "posted_by": "A2", "time": 0, "done_time": float(i + 1), "history": []})
        (self.board / "board.json").write_text(json.dumps(board))
        r = self.call("board_pull", name="c_eq")
        self.assertEqual(r["inserted"], ["a_eq", "b_eq", "c_eq"])
        src = (self.work / "Problem.lean").read_text()
        self.assertLess(src.index("lemma a_eq"), src.index("lemma b_eq"))
        self.assertLess(src.index("lemma b_eq"), src.index("lemma c_eq"))
        self.assertLess(src.index("lemma c_eq"), src.index("/-- The target. -/"))
        self.assertEqual(self.call("board_pull", name="b_eq")["inserted"], [])

    def test_post_reports_the_board_and_near_duplicates(self):
        r = self.call("board_post", name="rows_lt", statement="private lemma rows_lt (n m t : ℕ) (h : 0 < t) : (Finset.univ.filter (fun i => t < f i)).card < n")
        self.assertEqual(r["already_on_board"], [])
        self.as_agent("A2")
        r = self.call("board_post", name="large_rows_card_lt", statement="private lemma large_rows_card_lt (n m t : ℕ) (ht : 0 < t) : (Finset.univ.filter (fun i => t < f i)).card < n")
        self.assertEqual([e["name"] for e in r["already_on_board"]], ["rows_lt"])
        self.assertEqual(r["similar"], ["rows_lt"])
        r = self.call("board_post", name="unrelated", statement="private lemma unrelated (x : ℝ) (hx : 0 ≤ x) : Real.arctan x ≤ x")
        self.assertEqual(r["similar"], [])

    def test_codex_delivery_registers_the_board_server(self):
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "host"
            result = codex_prepare(BOARD, output, sys.executable)
            self.assertEqual(result["server_name"], "lemma_board")
            config = tomllib.loads((output / ".codex/config.toml").read_text())
            self.assertIn("lemma_board", config["mcp_servers"])
            self.assertIn("board_pull", (output / "AGENTS.md").read_text())


if __name__ == "__main__":
    unittest.main()
