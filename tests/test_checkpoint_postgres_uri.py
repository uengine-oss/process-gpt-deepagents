"""checkpoint_postgres_uri tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.executor.postgres_checkpointer import checkpoint_postgres_uri


class CheckpointPostgresUriTests(unittest.TestCase):
    def test_composes_from_db_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DB_USER": "u",
                "DB_PASSWORD": "p@ss:word",
                "DB_HOST": "localhost",
                "DB_PORT": "54322",
                "DB_NAME": "mydb",
            },
            clear=False,
        ):
            self.assertEqual(
                checkpoint_postgres_uri(),
                "postgresql://u:p%40ss%3Aword@localhost:54322/mydb",
            )

    def test_empty_when_db_incomplete(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DB_HOST": "",
                "DB_NAME": "x",
            },
            clear=False,
        ):
            self.assertEqual(checkpoint_postgres_uri(), "")

    def test_default_user_and_port(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DB_USER": "",
                "DB_PASSWORD": "",
                "DB_HOST": "h",
                "DB_NAME": "n",
                "DB_PORT": "",
            },
            clear=False,
        ):
            self.assertEqual(checkpoint_postgres_uri(), "postgresql://postgres@h:5432/n")


if __name__ == "__main__":
    unittest.main()
