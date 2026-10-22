"""Table row views retain unlabelled cells in the leading header band."""
import unittest
from types import SimpleNamespace as NS
from raghub.indexing.chunks import table_units, ChunkingUnsupported


def make_table(rows, flags):
    cells, fields = [], {}
    for row, values in enumerate(rows):
        for col, text in enumerate(values):
            cell = NS(text=text, column_header=flags[row][col], start_row_offset_idx=row,
                      end_row_offset_idx=row+1, start_col_offset_idx=col,
                      end_col_offset_idx=col+1, row_span=1, col_span=1)
            pointer = "/tables/0/data/table_cells/" + str(len(cells)) + "/text"
            cells.append(cell)
            fields[pointer] = NS(field_pointer=pointer, cleaned_text=text)
    return NS(self_ref="#/tables/0", data=NS(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells)), fields


class LeadingTableContextTest(unittest.TestCase):
    def test_current_producer_policy_is_accepted_by_the_persisted_reader(self):
        from raghub.indexing.chunks import RULES
        from raghub.indexing.storage import verify_chunk_rules
        rules = {**RULES, "source_sha256": "a" * 64, "docling_core_version": "fixture"}
        self.assertEqual(verify_chunk_rules(rules), RULES["schema_version"])
        with self.assertRaises(ValueError):
            verify_chunk_rules({**rules, "schema_version": 999})

    def test_mixed_header_flags_keep_task_column_with_its_numbers(self):
        table, fields = make_table([["Task", "Train", "Test"], ["Natural Questions", "79169", "3611"]],
                                   [[False, True, True], [False, False, False]])
        units, ranges, header_end = table_units(table, fields)
        self.assertEqual(ranges, [(1, 2)])
        self.assertEqual(header_end, 1)
        self.assertIn("Task=Natural Questions", units[0].text)
        self.assertIn("Train=79169", units[0].text)
        consumed = {p["field_pointer"] for u in units for p in u.pieces if not p["constructed"]}
        self.assertEqual(consumed, set(fields))
        task = next(p for p in units[0].pieces if not p["constructed"] and p["field_pointer"].endswith("/0/text"))
        self.assertEqual(task["purpose"], "TABLE_LEADING_CONTEXT")
        self.assertFalse(table.data.table_cells[0].column_header)

    def test_leading_rows_remain_source_context_without_changing_flags(self):
        table, fields = make_table([["Scope A", "2026"], ["Item", "Amount"], ["One", "42"]],
                                   [[False, False], [True, True], [False, False]])
        units, _, _ = table_units(table, fields)
        self.assertIn("Scope A", units[0].text)
        self.assertIn("2026", units[0].text)
        consumed = {p["field_pointer"] for u in units for p in u.pieces if not p["constructed"]}
        self.assertEqual(consumed, set(fields))
        self.assertFalse(table.data.table_cells[0].column_header)

    def test_table_without_headers_retains_every_data_row(self):
        table, fields = make_table([["Left", "Right"], ["A", "B"]], [[False, False], [False, False]])
        units, ranges, end = table_units(table, fields)
        self.assertEqual((ranges, end), ([(0, 1), (1, 2)], 0))
        self.assertEqual({p["field_pointer"] for u in units for p in u.pieces if not p["constructed"]}, set(fields))

    def test_header_only_table_keeps_content_without_inventing_data_rows(self):
        table, fields = make_table([["Only", "Header"]], [[True, True]])
        units, ranges, end = table_units(table, fields)
        self.assertEqual((ranges, end), ([(0, 1)], 1))
        self.assertIn("无数据行", units[0].text)
        self.assertIn("（表头）：Only", units[0].text)
        self.assertIn("（表头）：Header", units[0].text)
        pieces = [p for p in units[0].pieces if not p["constructed"]]
        self.assertEqual({p["field_pointer"] for p in pieces}, set(fields))
        self.assertEqual({p["purpose"] for p in pieces}, {"HEADER"})
        self.assertNotIn("=", units[0].text)

    def test_header_only_table_retains_unlabelled_leading_cells(self):
        table, fields = make_table([["Scope", "2026"], ["Name", "Value"]],
                                  [[False, False], [True, True]])
        units, ranges, end = table_units(table, fields)
        self.assertEqual((ranges, end), ([(0, 2)], 2))
        self.assertEqual({p["field_pointer"] for p in units[0].pieces if not p["constructed"]}, set(fields))
        self.assertIn("（前置内容）：2026", units[0].text)


if __name__ == "__main__":
    unittest.main()
