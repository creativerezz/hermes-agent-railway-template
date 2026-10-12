"""Admin Status reports Hermes' storage check without changing liveness."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]


class StorageStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-v24-storage-")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / ".hermes"
        home.mkdir()
        self.home = home
        env = patch.dict(os.environ, {
            "HERMES_HOME": str(home), "ADMIN_PASSWORD": "test-password",
            "HERMES_REF": "v2026.9.24",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(
            f"hermes_storage_test_{uuid.uuid4().hex}", ROOT / "server.py"
        )
        assert spec and spec.loader
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.server.guard = lambda _request: None
        self.client = Mock()
        self.client.get = AsyncMock()
        self.server.get_http_client = lambda: self.client

    def upstream(self, payload, status_code=200):
        response = Mock(status_code=status_code)
        response.json.return_value = payload
        self.client.get.return_value = response

    async def test_admin_status_reports_only_storage_status_and_known_reason(self):
        self.upstream({
            "components": {"storage": {"status": "degraded", "reason": "corrupt"}},
            "gateway_pid": 1234,
            "hermes_home": "/private/path",
        })

        response = await self.server.api_status(None)
        payload = json.loads(response.body)

        self.assertEqual(payload["storage"], {"status": "degraded", "reason": "corrupt"})
        self.assertEqual(payload["gateway"]["state"], "stopped")
        self.assertNotIn("gateway_pid", payload)
        self.assertNotIn("hermes_home", payload)
        args, kwargs = self.client.get.await_args
        self.assertEqual(args, ("http://127.0.0.1:9119/api/status",))
        self.assertEqual(set(kwargs), {"timeout"})  # Upstream route is public; no second auth.
        self.assertLessEqual(kwargs["timeout"].read, 3.0)

    async def test_storage_ok_and_unrecognized_reason_is_not_forwarded(self):
        self.upstream({"components": {"storage": {"status": "ok", "reason": "not initialized"}}})
        self.assertEqual(await self.server._dashboard_storage_status(), {"status": "ok"})
        self.upstream({"components": {"storage": {"status": "ok", "reason": "corrupt"}}})
        self.assertEqual(await self.server._dashboard_storage_status(), {"status": "ok"})
        self.upstream({"components": {"storage": {"status": "degraded", "reason": "private/path"}}})
        self.assertEqual(await self.server._dashboard_storage_status(), {"status": "degraded"})

    async def test_dashboard_failures_are_unknown_and_health_stays_live(self):
        self.upstream({}, status_code=503)
        self.assertEqual(await self.server._dashboard_storage_status(), {"status": "unknown"})
        self.client.get.side_effect = httpx.ConnectError("dashboard unavailable")
        response = await self.server.api_status(None)
        self.assertEqual(json.loads(response.body)["storage"], {"status": "unknown"})
        health = await self.server.route_health(None)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(json.loads(health.body)["status"], "ok")

    async def test_missing_or_malformed_upstream_storage_is_unknown(self):
        for payload in ({}, {"components": {}}, {"components": {"storage": {"status": "bad"}}}, []):
            with self.subTest(payload=payload):
                self.upstream(payload)
                self.assertEqual(await self.server._dashboard_storage_status(), {"status": "unknown"})
        self.client.get.return_value.json.side_effect = ValueError("invalid JSON")
        self.assertEqual(await self.server._dashboard_storage_status(), {"status": "unknown"})

    async def test_hindsight_notice_tracks_root_and_live_named_profiles(self):
        self.upstream({})
        self.assertFalse(json.loads((await self.server.api_status(None)).body)["hindsight_configured"])

        profiles = self.home / "profiles"
        work = profiles / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
        self.assertTrue(json.loads((await self.server.api_status(None)).body)["hindsight_configured"])

        tombstones = profiles / ".deleted"
        tombstones.mkdir()
        (tombstones / "work").write_text("deleted\n", encoding="utf-8")
        self.assertFalse(json.loads((await self.server.api_status(None)).body)["hindsight_configured"])

        (self.home / "config.yaml").write_text("memory:\n  provider: hindsight\n", encoding="utf-8")
        self.assertTrue(json.loads((await self.server.api_status(None)).body)["hindsight_configured"])


if __name__ == "__main__":
    unittest.main()
