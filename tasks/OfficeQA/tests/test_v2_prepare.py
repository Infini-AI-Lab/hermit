"""OfficeQA Pro v2 rendering: parsed JSON elements become the v1 TXT shape."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import v2_prepare  # noqa: E402


class TableRendering(unittest.TestCase):
    def test_plain_table_gets_numeric_header_row(self):
        text = v2_prepare.table_to_markdown("<table><tr><th>Class</th><th>Receipts</th></tr><tr><td>Ordinary</td><td>$3,781</td></tr></table>")
        self.assertEqual(text.splitlines(), ["| 0 | 1 |", "| --- | --- |", "| Class | Receipts |", "| Ordinary | $3,781 |"])

    def test_spans_keep_columns_aligned(self):
        text = v2_prepare.table_to_markdown('<table><tr><th rowspan="2">Year</th><th colspan="2">Amount</th></tr>'
                                            '<tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>')
        self.assertEqual(text.splitlines()[2:], ["| Year | Amount |  |", "|  | a | b |", "| 1 | 2 | 3 |"])

    def test_fenced_and_plain_text_tables(self):
        self.assertEqual(v2_prepare.table_to_markdown("```html\n<table><tr><td>Carried</td><td>203</td></tr></table>\n```").splitlines()[-1], "| Carried | 203 |")
        self.assertEqual(v2_prepare.table_to_markdown("N/A"), "N/A")
        self.assertEqual(v2_prepare.table_to_markdown("<table></table>"), "")

    def test_pipes_inside_cells_are_escaped(self):
        self.assertIn("a \\| b", v2_prepare.table_to_markdown("<table><tr><td>a | b</td></tr></table>"))


class DocumentRendering(unittest.TestCase):
    def test_elements_in_order_separated_by_blank_lines(self):
        document = {"document": {"elements": [
            {"type": "title", "content": "COMBINED STATEMENT", "bbox": [{"coord": [0, 0, 1, 1], "page_id": 0}]},
            {"type": "table", "content": "<table><tr><td>x</td></tr></table>", "bbox": [{"coord": [0, 0, 1, 1], "page_id": 0}]},
            {"type": "text", "content": "  ", "bbox": [{"coord": [0, 0, 1, 1], "page_id": 1}]},
            {"type": "text", "content": "Total &amp; balance", "bbox": [{"coord": [0, 0, 1, 1], "page_id": 1}]},
        ], "pages": [{"id": 0}, {"id": 1}]}}
        self.assertEqual(v2_prepare.render_document(document),
                         "COMBINED STATEMENT\n\n| 0 |\n| --- |\n| x |\n\nTotal & balance\n")


if __name__ == "__main__":
    unittest.main()
