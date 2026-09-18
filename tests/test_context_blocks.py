"""Check preservation and repeatability of the shared adapter context helper."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from adapter_context import START, END, append_context


class ContextBlockTests(unittest.TestCase):
    def test_update_preserves_project_text_and_is_idempotent(self):
        original = "# My project\nKeep this exact text.\n"
        first = append_context(original, "Adapter v1")
        suffix = "\nAdditional user instructions.\n"
        updated = append_context(first + suffix, "Adapter v2")
        self.assertTrue(updated.startswith(original))
        self.assertTrue(updated.endswith(suffix))
        self.assertNotIn("Adapter v1", updated)
        self.assertEqual(updated.count(START), 1)
        self.assertEqual(append_context(updated, "Adapter v2"), updated)

    def test_ambiguous_markers_fail_instead_of_losing_text(self):
        for existing in (START, END, END + START, START + START + END):
            with self.subTest(existing=existing), self.assertRaises(ValueError):
                append_context(existing, "Adapter")


if __name__ == "__main__":
    unittest.main()
