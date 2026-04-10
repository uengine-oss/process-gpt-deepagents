from __future__ import annotations

import unittest


class FormPrefillFromStructuredOutputsTests(unittest.TestCase):
    def test_prefill_maps_trade_date_to_record_date(self) -> None:
        from app.executor.form_prefill import prefill_form_data_from_structured_outputs

        field_keys = ["stock_code", "stock_name", "record_date", "closing_price"]
        structured = {
            "job-1": {
                "sandbox_write_and_run": [
                    {
                        "command": "python3 produce_result.py",
                        "parsed_stdout": {
                            "stock_name": "삼성전자",
                            "stock_code": "005930",
                            "trade_date": "2026-04-09",
                            "closing_price": 204000,
                            "currency": "KRW",
                        },
                    }
                ]
            }
        }
        current = {"stock_code": "", "stock_name": "", "record_date": "", "closing_price": ""}
        out = prefill_form_data_from_structured_outputs(
            current,
            field_keys=field_keys,
            structured_domain_outputs_by_job=structured,
        )
        self.assertEqual(out["stock_code"], "005930")
        self.assertEqual(out["stock_name"], "삼성전자")
        self.assertEqual(out["record_date"], "2026-04-09")
        self.assertEqual(out["closing_price"], 204000)

    def test_prefill_does_not_overwrite_existing_values(self) -> None:
        from app.executor.form_prefill import prefill_form_data_from_structured_outputs

        field_keys = ["stock_code", "record_date"]
        structured = {
            "job-1": {
                "x": [
                    {
                        "parsed_stdout": {
                            "stock_code": "005930",
                            "trade_date": "2026-04-09",
                        }
                    }
                ]
            }
        }
        current = {"stock_code": "EXISTING", "record_date": ""}
        out = prefill_form_data_from_structured_outputs(
            current,
            field_keys=field_keys,
            structured_domain_outputs_by_job=structured,
        )
        self.assertEqual(out["stock_code"], "EXISTING")
        self.assertEqual(out["record_date"], "2026-04-09")


if __name__ == "__main__":
    unittest.main()

