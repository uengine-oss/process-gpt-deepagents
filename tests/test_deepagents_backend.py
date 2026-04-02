from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from deepagents_backend import make_deepagent_backend_factory, skills_route_prefix


class DeepagentsBackendTests(unittest.TestCase):
    def test_skills_route_prefix(self) -> None:
        route, root = skills_route_prefix("/app/skills")
        self.assertEqual(root, "/app/skills")
        self.assertEqual(route, "/app/skills/")

    def test_factory_returns_composite(self) -> None:
        try:
            from deepagents.backends.composite import CompositeBackend
        except Exception as exc:  # pragma: no cover
            self.skipTest(str(exc))
            return

        factory = make_deepagent_backend_factory("/tmp/skills_x")
        rt = MagicMock()
        backend = factory(rt)
        self.assertIsInstance(backend, CompositeBackend)
