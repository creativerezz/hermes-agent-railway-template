"""Data-safety regressions for the Hermes v2026.9.24 template upgrade."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.datastructures import UploadFile
from starlette.responses import FileResponse


ROOT = Path(__file__).resolve().parents[1]


class HomeFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-v24-data-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        env = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home),
            "ADMIN_PASSWORD": "test-password",
            "HERMES_REF": "v2026.9.24",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(f"hermes_data_test_{uuid.uuid4().hex}", ROOT / "server.py")
        assert spec and spec.loader
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.server.guard = lambda _request: None

    def write_json(self, rel: str, data: dict) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def db(self, rel: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"SQLite placeholder")
        return path

    def archive(self, *paths: str) -> Path:
        path = Path(self.tmp.name) / "backup.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for rel in paths:
                zf.writestr(rel, b"SQLite placeholder")
        return path


class PairingFileTests(HomeFixture, unittest.TestCase):
    def test_symlinked_legacy_directory_does_not_delete_live_approvals(self):
        live = self.write_json("platforms/pairing/telegram-approved.json", {
            "42": {"user_name": "Ada", "approved_at": 1},
        })
        (self.home / "pairing").symlink_to(live.parent, target_is_directory=True)

        self.server._consolidate_pairing_dirs()

        self.assertEqual(json.loads(live.read_text())["42"]["user_name"], "Ada")

    def test_split_store_preserves_active_entry_and_removes_inactive_copy(self):
        active = self.write_json("pairing/telegram-approved.json", {"42": {"user_name": "current"}})
        stale = self.write_json("platforms/pairing/telegram-approved.json", {
            "42": {"user_name": "old"}, "99": {"user_name": "keep"},
        })

        self.server._consolidate_pairing_dirs()

        self.assertEqual(json.loads(active.read_text()), {
            "42": {"user_name": "current"}, "99": {"user_name": "keep"},
        })
        self.assertFalse(stale.exists())

    def test_atomic_write_failure_leaves_original_file_intact(self):
        path = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        original = path.read_bytes()
        with patch.object(self.server.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.server._wjson(path, {"99": {"user_name": "Grace"}})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".telegram-approved-*")), [])

    def test_consolidation_never_overwrites_malformed_active_file(self):
        active = self.home / "pairing" / "telegram-approved.json"
        active.parent.mkdir()
        active.write_text("{bad json", encoding="utf-8")
        stale = self.write_json("platforms/pairing/telegram-approved.json", {"99": {"user_name": "Grace"}})

        self.server._consolidate_pairing_dirs()

        self.assertEqual(active.read_text(), "{bad json")
        self.assertTrue(stale.exists())


class PairingEndpointTests(HomeFixture, unittest.IsolatedAsyncioTestCase):
    class Request:
        def __init__(self, **body):
            self.body = body

        async def json(self):
            return self.body

    def pending(self):
        return self.write_json("platforms/pairing/telegram-pending.json", {
            "request-id": {"user_id": "42", "user_name": "Ada", "created_at": 1},
        })

    async def test_approval_keeps_pending_when_grant_write_fails(self):
        pending = self.pending()
        with patch.object(self.server, "_wjson", side_effect=OSError("disk full")):
            response = await self.server.api_pairing_approve(self.Request(platform="telegram", code="request-id"))
        self.assertEqual(response.status_code, 500)
        self.assertIn("request-id", json.loads(pending.read_text()))

    async def test_approval_written_first_and_retry_cleans_pending(self):
        pending = self.pending()
        actual_write = self.server._wjson
        calls = 0

        def fail_pending_once(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("pending cleanup failed")
            return actual_write(path, data)

        with patch.object(self.server, "_wjson", side_effect=fail_pending_once):
            response = await self.server.api_pairing_approve(self.Request(platform="telegram", code="request-id"))
        self.assertEqual(response.status_code, 500)
        approved = self.home / "platforms/pairing/telegram-approved.json"
        self.assertIn("42", json.loads(approved.read_text()))
        self.assertIn("request-id", json.loads(pending.read_text()))

        retry = await self.server.api_pairing_approve(self.Request(platform="telegram", code="request-id"))
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(json.loads(pending.read_text()), {})

    async def test_revoke_refuses_separate_allowlist_grant_and_keeps_row(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        self.server.ENV_FILE.write_text("TELEGRAM_ALLOWED_USERS=42\n", encoding="utf-8")

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 409)
        self.assertIn("TELEGRAM_ALLOWED_USERS", json.loads(response.body)["error"])
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_revoke_refuses_railway_global_allowlist_grant(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        with patch.dict(os.environ, {"GATEWAY_ALLOWED_USERS": "42"}):
            response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))
        self.assertEqual(response.status_code, 409)
        self.assertIn("GATEWAY_ALLOWED_USERS", json.loads(response.body)["error"])
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_revoke_refuses_native_config_allowlist_grant(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        (self.home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n"
            "        allow_from: ['42']\n",
            encoding="utf-8",
        )

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 409)
        self.assertIn("config.yaml gateway.platforms.telegram.extra.allow_from", json.loads(response.body)["error"])
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_revoke_refuses_plugin_config_grant(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        (self.home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n"
            "        allowed_users: ['42']\n",
            encoding="utf-8",
        )

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 409)
        self.assertIn("config.yaml gateway.platforms.telegram.extra.allowed_users", json.loads(response.body)["error"])
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_revoke_refuses_templated_native_allowlist_without_guessing_value(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        (self.home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n"
            "        allow_from: ['${OWNER_ID}']\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"OWNER_ID": "42"}):
            response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 409)
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_group_policy_warns_without_blocking_dm_pairing_removal(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        self.server.ENV_FILE.write_text("TELEGRAM_GROUP_ALLOWED_CHATS=-100999\n", encoding="utf-8")
        (self.home / "config.yaml").write_text(
            "gateway:\n  platforms:\n    telegram:\n      extra:\n"
            "        group_allowed_chats: ['-100999']\n",
            encoding="utf-8",
        )

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["scope"], "pairing")
        self.assertIn("Other access rules may still apply", body["warning"])
        self.assertEqual(json.loads(approved.read_text()), {})

    async def test_unresolved_buzz_alias_warns_after_pairing_removal(self):
        approved = self.write_json("platforms/pairing/buzz-approved.json", {"hex-user": {"user_name": "Ada"}})
        self.server.ENV_FILE.write_text("BUZZ_ALLOWED_USERS=npub1example\n", encoding="utf-8")

        response = await self.server.api_pairing_revoke(self.Request(platform="buzz", user_id="hex-user"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("BUZZ_ALLOWED_USERS", json.loads(response.body)["warning"])
        self.assertEqual(json.loads(approved.read_text()), {})

    async def test_revoke_keeps_approval_when_native_config_is_unreadable(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        (self.home / "config.yaml").write_text("gateway: [unterminated\n", encoding="utf-8")

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 500)
        self.assertIn("42", json.loads(approved.read_text()))

    async def test_unrelated_allowlist_entry_does_not_block_pairing_removal(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})
        self.server.ENV_FILE.write_text("TELEGRAM_ALLOWED_USERS=99\n", encoding="utf-8")
        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(approved.read_text()), {})

    async def test_revoke_removes_pairing_only_when_no_known_static_grant(self):
        approved = self.write_json("platforms/pairing/telegram-approved.json", {"42": {"user_name": "Ada"}})

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["scope"], "pairing")
        self.assertEqual(json.loads(approved.read_text()), {})

    async def test_revoke_does_not_overwrite_malformed_approved_file(self):
        approved = self.home / "platforms/pairing/telegram-approved.json"
        approved.parent.mkdir(parents=True)
        approved.write_text("{bad json", encoding="utf-8")

        response = await self.server.api_pairing_revoke(self.Request(platform="telegram", user_id="42"))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(approved.read_text(), "{bad json")


class BackupCompletenessTests(HomeFixture, unittest.TestCase):
    def test_duplicate_database_basename_requires_both_full_paths(self):
        self.db("response_store.db")
        self.db("profiles/work/response_store.db")
        archive = self.archive("response_store.db")

        self.assertEqual(self.server._live_db_names(), {
            "response_store.db", "profiles/work/response_store.db",
        })
        self.assertIn("profiles/work/response_store.db", self.server._incomplete_backup_reason(archive))

    def test_scan_error_fails_closed_instead_of_certifying_archive(self):
        archive = self.archive("config.yaml")
        with patch.object(self.server.os, "walk", side_effect=OSError("permission denied")):
            reason = self.server._incomplete_backup_reason(archive)
        self.assertIn("could not inspect live databases", reason)

    def test_backup_selection_mirrors_profile_cache_and_nested_code_dirs(self):
        included = {"state.db", "profiles/work/cache/citations/evidence.db", "skills/x/hermes-agent/notes.db"}
        excluded = {"profiles/work/cache/terminal/job.db", "browser_profiles/Cookies.db", "hermes-agent/code.db"}
        for rel in included | excluded:
            self.db(rel)
        self.assertEqual(self.server._live_db_names(), included)


class BackupEndpointTests(HomeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_scan_error_keeps_manual_archive_with_warning(self):
        async def backup(*args, **_kwargs):
            with zipfile.ZipFile(Path(args[2]), "w") as zf:
                zf.writestr("config.yaml", "model: {}\n")
            return 0, "Backup complete"

        self.server._run_hermes_cli = backup
        self.server._hermes_version = AsyncMock(return_value="Hermes v2026.9.24")
        with patch.object(self.server.os, "walk", side_effect=OSError("permission denied")):
            response = await self.server.api_backup_download(object())

        self.assertIsInstance(response, FileResponse)
        self.assertIn("could not inspect live databases", response.headers["x-backup-warning"])
        await response.background()

    async def test_scan_error_aborts_restore_before_stopping_processes(self):
        incoming = io.BytesIO()
        with zipfile.ZipFile(incoming, "w") as zf:
            zf.writestr("config.yaml", "model: {}\n")
        upload = UploadFile(filename="backup.zip", file=io.BytesIO(incoming.getvalue()))

        class Request:
            async def form(self):
                return {"file": upload}

        async def backup(*args, **_kwargs):
            with zipfile.ZipFile(Path(args[2]), "w") as zf:
                zf.writestr("config.yaml", "model: {}\n")
            return 0, "Backup complete"

        self.server._run_hermes_cli = backup
        self.server.gw.stop = AsyncMock(side_effect=AssertionError("gateway must stay up"))
        self.server.dash.stop = AsyncMock(side_effect=AssertionError("dashboard must stay up"))
        self.server.BACKUP_DIR = self.home / "backups"

        with patch.object(self.server.os, "walk", side_effect=OSError("permission denied")):
            response = await self.server.api_backup_restore(Request())

        self.assertEqual(response.status_code, 500)
        self.assertIn("could not inspect live databases", json.loads(response.body)["error"])
        self.server.gw.stop.assert_not_awaited()
        self.server.dash.stop.assert_not_awaited()
