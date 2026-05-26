import json
import unittest

from remote_control.work_move import (
    format_import_status,
    parse_external_imports,
    resolve_direction,
)


class ResolveDirectionTest(unittest.TestCase):
    def test_defaults_to_opposite(self):
        self.assertEqual(resolve_direction("codex", None), ("codex", "claude"))
        self.assertEqual(resolve_direction("claude", None), ("claude", "codex"))

    def test_explicit_to(self):
        self.assertEqual(resolve_direction("codex", "claude"), ("codex", "claude"))

    def test_same_engine_rejected(self):
        with self.assertRaises(ValueError):
            resolve_direction("codex", "codex")

    def test_bad_from(self):
        with self.assertRaises(ValueError):
            resolve_direction("gemini", None)

    def test_bad_to(self):
        with self.assertRaises(ValueError):
            resolve_direction("codex", "gemini")


class ParseExternalImportsTest(unittest.TestCase):
    def _text(self, *records):
        return json.dumps({"records": list(records)})

    def test_maps_source_path_to_record(self):
        text = self._text(
            {"source_path": "/p/a.jsonl", "imported_thread_id": "019e-aaa",
             "imported_at": 1779040389},
            {"source_path": "/p/b.jsonl", "imported_thread_id": "019e-bbb",
             "imported_at": 1779040400},
        )
        out = parse_external_imports(text)
        self.assertEqual(set(out), {"/p/a.jsonl", "/p/b.jsonl"})
        self.assertEqual(out["/p/a.jsonl"]["imported_thread_id"], "019e-aaa")

    def test_empty_or_garbage_is_empty(self):
        self.assertEqual(parse_external_imports(""), {})
        self.assertEqual(parse_external_imports("{not json"), {})
        self.assertEqual(parse_external_imports(json.dumps({})), {})

    def test_skips_records_without_source_path(self):
        text = self._text({"imported_thread_id": "x"}, {"source_path": "/p/c.jsonl"})
        self.assertEqual(list(parse_external_imports(text)), ["/p/c.jsonl"])


class FormatImportStatusTest(unittest.TestCase):
    def test_imported_shows_thread_and_date(self):
        rec = {"imported_thread_id": "019e-aaa", "imported_at": 1779040389}
        s = format_import_status("/p/a.jsonl", rec)
        self.assertIn("already imported", s)
        self.assertIn("019e-aaa", s)
        self.assertIn("/p/a.jsonl", s)

    def test_not_imported_gives_app_instruction(self):
        s = format_import_status("/p/a.jsonl", None)
        self.assertIn("not yet imported", s)
        self.assertIn("Codex app", s)
        self.assertIn("/p/a.jsonl", s)

    def test_bad_epoch_does_not_crash(self):
        rec = {"imported_thread_id": "x", "imported_at": "nope"}
        self.assertIn("?", format_import_status("/p/a.jsonl", rec))


if __name__ == "__main__":
    unittest.main()
