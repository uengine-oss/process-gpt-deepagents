from __future__ import annotations

import unittest


class TodoIOTypesTests(unittest.TestCase):
    def test_extract_io_types_from_markers(self) -> None:
        from app.executor.todo_runner import _extract_io_types

        td = {"content": "[IN=pdf][OUT=json] PDF에서 본문 추출 결과 생성"}
        it, ot = _extract_io_types(td)
        self.assertEqual(it, "pdf")
        self.assertEqual(ot, "json")

    def test_extract_io_types_from_fields(self) -> None:
        from app.executor.todo_runner import _extract_io_types

        td = {"content": "x", "input_type": "bpmn", "output_type": "table"}
        it, ot = _extract_io_types(td)
        self.assertEqual(it, "bpmn")
        self.assertEqual(ot, "table")

    def test_extract_io_types_missing(self) -> None:
        from app.executor.todo_runner import _extract_io_types

        td = {"content": "타입이 없음"}
        it, ot = _extract_io_types(td)
        self.assertEqual(it, "")
        self.assertEqual(ot, "")

    def test_is_file_output_type(self) -> None:
        from app.executor.todo_runner import _is_file_output_type

        self.assertTrue(_is_file_output_type("xlsx"))
        self.assertTrue(_is_file_output_type("pdf"))
        self.assertTrue(_is_file_output_type("docx"))
        self.assertTrue(_is_file_output_type("csv"))
        self.assertFalse(_is_file_output_type("json"))
        self.assertFalse(_is_file_output_type(""))


if __name__ == "__main__":
    unittest.main()

