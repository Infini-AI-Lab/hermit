"""Deterministic boundary tests; no model calls and no external data."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "examples/minimal_shop/adapter_tools.py"
SPEC = importlib.util.spec_from_file_location("demo_tools", PATH)
tools = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tools)


class MinimalAdapterTests(unittest.TestCase):
    def test_memories_have_stable_sources(self):
        self.assertEqual({m["id"] for m in tools.list_memories()["memories"]},
                         {"returns", "shipping", "rounding"})
        memory = tools.read_memory("returns")
        self.assertEqual(memory["source"], "memory/policies.json#returns")
        self.assertEqual(memory["memory"]["fee_rate"], "0.05")

    def test_memory_id_is_not_a_file_path(self):
        with self.assertRaises(ValueError):
            tools.read_memory("../../README.md")

    def test_round_fee_before_subtraction(self):
        result = tools.calculate_refund("0.10", "0.05")
        self.assertEqual(result["fee"], "0.01")
        self.assertEqual(result["refund"], "0.09")

    def test_demo_calculation(self):
        self.assertEqual(tools.calculate_refund("200.00", "0.05")["refund"], "190.00")

    def test_invalid_money_and_rates(self):
        for subtotal, rate in [("-1", "0.05"), ("2.675", "0.05"), ("NaN", "0.05"),
                               ("Infinity", "0.05"), ("10", "-0.1"), ("10", "1.1"),
                               ("1000001", "0"), ("bad", "0"), (10, "0.05")]:
            with self.subTest(subtotal=subtotal, rate=rate), self.assertRaises(ValueError):
                tools.calculate_refund(subtotal, rate)


if __name__ == "__main__":
    unittest.main()
