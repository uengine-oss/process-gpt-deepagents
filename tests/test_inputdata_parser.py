from __future__ import annotations

import unittest

from app.inputdata_parser import extract_inputdata_from_message, merge_inputdata_into_chat_payload


class TestInputDataParser(unittest.TestCase):
    def test_extract_inputdata_from_message_parses_json_and_strips_block(self) -> None:
        msg = (
            "첨부파일 내용 요약해줘\n\n"
            "[InputData]\n"
            '{"file":{"fileName":"a.pdf","fileType":"application/pdf","fileSize":1},'
            '"files":[{"fileName":"a.pdf"}],"fileCount":1}\n'
        )
        cleaned, obj = extract_inputdata_from_message(msg)
        self.assertEqual(cleaned, "첨부파일 내용 요약해줘")
        self.assertIsInstance(obj, dict)
        assert obj is not None
        self.assertIn("file", obj)
        self.assertIn("files", obj)

    def test_extract_inputdata_from_message_ignores_parse_failures(self) -> None:
        msg = "요약해줘\n\n[InputData]\n{not valid json"
        cleaned, obj = extract_inputdata_from_message(msg)
        self.assertEqual(cleaned, msg)
        self.assertIsNone(obj)

    def test_merge_inputdata_into_chat_payload_fills_missing_fields(self) -> None:
        payload = {"message": "x", "tenant_id": "t", "metadata": {}}
        inputdata = {
            "file": {"fileName": "a.pdf", "fileType": "application/pdf", "fileSize": 1},
            "files": [{"fileName": "a.pdf"}],
            "fileCount": 1,
            "metadata": {"k": "v"},
        }
        merged = merge_inputdata_into_chat_payload(payload, inputdata)
        self.assertIsInstance(merged.get("file"), dict)
        self.assertIsInstance(merged.get("files"), list)
        self.assertEqual(merged.get("file_count"), 1)
        self.assertIsInstance(merged.get("metadata"), dict)
        assert isinstance(merged.get("metadata"), dict)
        self.assertEqual(merged["metadata"].get("k"), "v")

    def test_merge_inputdata_metadata_payload_wins_on_conflict(self) -> None:
        payload = {"message": "x", "tenant_id": "t", "metadata": {"k": "payload"}}
        inputdata = {"metadata": {"k": "inputdata", "a": 1}}
        merged = merge_inputdata_into_chat_payload(payload, inputdata)
        self.assertEqual(merged["metadata"]["k"], "payload")
        self.assertEqual(merged["metadata"]["a"], 1)

