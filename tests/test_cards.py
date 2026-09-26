"""Cards architecture and evolver block algorithm, without model calls."""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
from harnessadapter.adapter.loader import Adapter
from harnessadapter.paths import ARCHITECTURES as ARCH, resolve_architecture
from harnessadapter.hosts.codex import prepare as codex_prepare

CARDS = ARCH / "cards"
_spec = importlib.util.spec_from_file_location("cards_block_under_test", ROOT / "harnessadapter/evolvers/cards/block.py")
block = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(block)
schema, render = block.core()

GOOD = {"id": "C001", "scope": "fiscal year, annual row, later issue",
        "claim": "When the same annual row is printed in several issues, use the latest issue that prints it.",
        "unless": "The question names a specific issue or date.", "support": [3, 8]}


def released(n=10, wrong=(1, 3, 8)):
    return {o: {"question": f"Question number {o} about the fiscal year annual row in a later issue" if o in (3, 8, 5) else f"Question number {o} about something else",
                "submitted": "x", "reference": "y", "strict": 0 if o in wrong else 1, "status": "completed"} for o in range(1, n + 1)}


class SchemaTests(unittest.TestCase):
    def test_round_trip_and_render(self):
        text = schema.dumps({**GOOD, "admitted": 2})
        card = schema.parse(text)
        self.assertEqual(schema.validate(card), [])
        self.assertEqual(schema.support_list(card["support"]), [3, 8])
        self.assertEqual(int(card["admitted"]), 2)
        rendered = schema.render(card)
        self.assertIn("### C001 — applies when: fiscal year, annual row, later issue", rendered)
        self.assertIn("Not when: The question names a specific issue or date.", rendered)
        self.assertNotIn("support", rendered)

    def test_validation_rejects_bad_cards(self):
        self.assertIn("id must look like C001", schema.validate({**GOOD, "id": "card1"}))
        self.assertTrue(any("stopword" in e for e in schema.validate({**GOOD, "scope": "year"})))
        self.assertTrue(any("too short" in e for e in schema.validate({**GOOD, "scope": "tax"})))
        self.assertTrue(any("exceeds" in e for e in schema.validate({**GOOD, "claim": "x" * 601})))
        self.assertEqual(schema.validate({**GOOD, "support": [3]}), [])
        self.assertTrue(any("support" in e for e in schema.validate({**GOOD, "support": []})))
        self.assertTrue(any("unknown" in e for e in schema.validate({**GOOD, "recipe": "no"})))

    def test_matches_whole_words(self):
        self.assertEqual(schema.matches("What is the Fiscal Year total?", "fiscal year, annual"), ["fiscal year"])
        self.assertEqual(schema.matches("annually reported", "annual"), [])


class RenderTests(unittest.TestCase):
    def deck(self, cards):
        d = Path(tempfile.mkdtemp())
        shutil.copytree(CARDS, d / "adapter", ignore=shutil.ignore_patterns("__pycache__"))
        for card in cards:
            (d / "adapter/cards" / f"{card['id']}.md").write_text(schema.dumps(card))
        return d / "adapter"

    def test_empty_deck_renders_preamble_only(self):
        text = render.compile(CARDS)
        self.assertTrue(text.startswith("# Learned cards"))
        self.assertIn(render.EMPTY.strip(), text)
        self.assertEqual(Adapter(CARDS).context(), text)
        self.assertEqual(Adapter(CARDS).tools, {})

    def test_only_admitted_cards_in_block_then_support_order(self):
        root = self.deck([{**GOOD, "id": "C001"}, {**GOOD, "id": "C002", "admitted": 3, "support": [1, 2, 3]},
                          {**GOOD, "id": "C003", "admitted": 2}, {**GOOD, "id": "C004", "admitted": 3}])
        layout = render.layout(root)
        self.assertEqual(layout["rendered"], ["C003", "C002", "C004"])
        self.assertNotIn("C001", layout["text"])
        self.assertLessEqual(layout["bytes"], render.CARDS_MAX)
        self.assertEqual(layout["bytes"], len(layout["text"].encode()))

    def test_budget_drops_newest_cards_whole(self):
        cards = [{**GOOD, "id": f"C{i:03}", "admitted": i, "claim": "w" * 590} for i in range(1, 40)]
        layout = render.layout(self.deck(cards))
        self.assertTrue(layout["overflow"])
        self.assertEqual(layout["rendered"] + layout["overflow"], [c["id"] for c in cards])
        self.assertLessEqual(layout["bytes"], render.CARDS_MAX)

    def test_invalid_card_file_is_an_error(self):
        root = self.deck([])
        (root / "cards/C009.md").write_text("id: C009\nscope: year\nclaim: x\nsupport: 1\n")
        with self.assertRaises(ValueError):
            render.compile(root)


class DeployTests(unittest.TestCase):
    def test_codex_deploy_serves_preamble_without_server(self):
        self.assertEqual(resolve_architecture("cards"), CARDS)
        with tempfile.TemporaryDirectory() as d:
            result = codex_prepare(CARDS, Path(d) / "host", sys.executable)
            self.assertIsNone(result["server"])
            self.assertEqual(result["roles"], {})
            agents = (Path(d) / "host/AGENTS.md").read_text()
            self.assertIn("Learned cards", agents)
            self.assertIn(render.EMPTY.strip(), agents)
            self.assertNotIn("mcp_servers", tomllib.loads((Path(d) / "host/.codex/config.toml").read_text()))


class BlockTests(unittest.TestCase):
    def test_pool_and_right(self):
        rel = released()
        self.assertEqual(block.pool(rel, claimed={"3": "C001"}), [1, 8])
        self.assertEqual(block.right(rel, excluded=(2,)), [4, 5, 6, 7, 9, 10])
        rel[8]["transport_failed"] = True
        self.assertEqual(block.pool(rel, claimed={}), [1, 3])

    def test_screen_rules(self):
        rel = released()
        ok = block.screen(dict(GOOD), [1, 3, 8], rel, [], [])
        self.assertTrue(ok["ok"], ok["reasons"])
        self.assertTrue(any("outside the pool" in r for r in block.screen({**GOOD, "support": [3, 4]}, [1, 3, 8], rel, [], [])["reasons"]))
        self.assertTrue(any("leak - corpus file name" in r for r in block.screen({**GOOD, "claim": "See treasury_bulletin_2016_09.txt"}, [1, 3, 8], rel, [], [])["reasons"]))
        self.assertTrue(any("large number" in r for r in block.screen({**GOOD, "claim": "The value is 131973 in the table."}, [1, 3, 8], rel, [], [])["reasons"]))
        self.assertTrue(block.screen({**GOOD, "claim": "Use 2019 minus 2011 = 8 years and 365/7 = 52.14 weeks."}, [1, 3, 8], rel, [], [])["ok"])
        self.assertTrue(any("copies a question" in r for r in block.screen({**GOOD, "claim": "question number 3 about the fiscal year annual row in a later issue"}, [1, 3, 8], rel, [], [])["reasons"]))
        self.assertTrue(block.screen({**GOOD, "claim": "an annual row in a later issue is revised"}, [1, 3, 8], rel, [], [])["ok"])
        self.assertTrue(block.screen({**GOOD, "support": [3]}, [1, 3, 8], rel, [], [])["ok"])
        self.assertTrue(any("already rejected" in r for r in block.screen(dict(GOOD), [1, 3, 8], rel, [ok["sha"]], [])["reasons"]))
        self.assertTrue(any("already exists" in r for r in block.screen(dict(GOOD), [1, 3, 8], rel, [], ["C001"])["reasons"]))
        self.assertTrue(any("controller only" in r for r in block.screen({**GOOD, "admitted": 1}, [1, 3, 8], rel, [], [])["reasons"]))

    def test_choose_keep_prefers_scope_matches_and_never_support(self):
        rel = released()
        keep = block.choose_keep(block.right(rel), GOOD["scope"], rel, exclude=[3, 8])
        self.assertEqual(keep, [5, 10])
        self.assertEqual(block.choose_keep([5], GOOD["scope"], rel, exclude=[5]), [])

    def test_judge(self):
        def run(q, role, k, strict, tokens=1000, excluded=False):
            return {"q": q, "role": role, "k": k, "strict": strict, "status": "completed", "out_tokens": tokens, "excluded": excluded}
        originals = {3: 1000, 8: 1000, 5: 1000, 10: 1000}
        good = [run(3, "support", 1, 1), run(3, "support", 2, 1), run(8, "support", 1, 1), run(8, "support", 2, 0),
                run(5, "keep", 1, 1), run(5, "keep", 2, 0), run(10, "keep", 1, 1), run(10, "keep", 2, 1)]
        verdict = block.judge(good, originals)
        self.assertTrue(verdict["admitted"], verdict["reason"])
        two_misses = good[:3] + [run(8, "support", 2, 0), run(3, "support", 1, 0)] + good[4:]
        self.assertFalse(block.judge(two_misses, originals)["support_ok"])
        keep_lost = good[:4] + [run(5, "keep", 1, 0), run(5, "keep", 2, 0)] + good[6:]
        self.assertFalse(block.judge(keep_lost, originals)["keep_ok"])
        self.assertEqual(block.judge(keep_lost, originals)["lost"], [5])
        expensive = [dict(r, out_tokens=4000) for r in good]
        self.assertFalse(block.judge(expensive, originals)["cost_ok"])
        self.assertTrue(block.judge(expensive, {})["cost_ok"])
        excluded = [dict(r, excluded=True) if r["q"] == 8 else r for r in good]
        verdict = block.judge(excluded, originals)
        self.assertFalse(verdict["admitted"])
        self.assertEqual(verdict["inconclusive"], [8])
        single_keep_wrong = good[:4] + [run(5, "keep", 1, 0), dict(run(5, "keep", 2, 0), excluded=True)] + good[6:]
        self.assertTrue(block.judge(single_keep_wrong, originals)["keep_ok"])
        one_support = [run(3, "support", 1, 1), run(3, "support", 2, 1)] + good[4:]
        self.assertTrue(block.judge(one_support, originals)["admitted"])
        one_support_miss = [run(3, "support", 1, 1), run(3, "support", 2, 0)] + good[4:]
        self.assertFalse(block.judge(one_support_miss, originals)["support_ok"])
        three_support = good[:4] + [run(1, "support", 1, 0), run(1, "support", 2, 0)] + good[4:]
        self.assertFalse(block.judge(three_support, originals)["support_ok"])

    def test_assemble_adds_only_admitted_cards(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "next"
            written = block.assemble(CARDS, target, [dict(GOOD)], block=4)
            self.assertEqual(written, ["C001.md"])
            layout = render.layout(target)
            self.assertEqual(layout["rendered"], ["C001"])
            self.assertEqual(int(schema.parse((target / "cards/C001.md").read_text())["admitted"]), 4)
            for name in ("adapter.json", "PREAMBLE.md", "interface.py", "cards_core/schema.py"):
                self.assertEqual((target / name).read_bytes(), (CARDS / name).read_bytes())
            with self.assertRaises(FileExistsError):
                block.assemble(CARDS, target, [], block=5)

    def test_status_text_mentions_pool_and_deck(self):
        rel = released()
        text = block.status_text(2, [1, 3], [2, 4], rel, [{**GOOD, "admitted": 1}], {"C001": [[9, 1], [10, 0]]},
                                 [{"t": 1, "id": "C001", "action": "add", "admitted": True, "reason": "support 4/4"}])
        self.assertIn("pool q001", text)
        self.assertIn("1 right / 1 wrong", text)
        self.assertIn("ADMITTED", text)


if __name__ == "__main__":
    unittest.main()
