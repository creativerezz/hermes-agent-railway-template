"""End-to-end API behavior for template-managed manual backup jobs."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx


ROOT = Path(__file__).resolve().parents[1]


class ManualBackupJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="manual-backup-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        cli = self.bin / "hermes"
        cli.write_text(f"#!{sys.executable}\n" + '''
import os, sys, time, zipfile
if '--version' in sys.argv:
    print('v2026.9.24')
    sys.exit(0)
out = sys.argv[sys.argv.index('-o') + 1]
print('Scanning files ...', flush=True)
print('Backing up 2 files ...', flush=True)
time.sleep(float(os.environ.get('FAKE_BACKUP_SLEEP', '0')))
with zipfile.ZipFile(out, 'w') as zf:
    zf.writestr('config.yaml', 'model: test')
    if os.environ.get('FAKE_BACKUP_SKIP_DB') != '1':
        zf.writestr('state.db', b'database')
print('2/2 files ...', flush=True)
sys.exit(int(os.environ.get('FAKE_BACKUP_EXIT', '0')))
''', encoding="utf-8")
        cli.chmod(0o755)
        env = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home), "ADMIN_PASSWORD": "test-password",
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        })
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(f"backup_job_test_{uuid.uuid4().hex}", ROOT / "server.py")
        assert spec and spec.loader
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.server.MANUAL_BACKUP_TMP = root / "prepared"
        self.transport = httpx.ASGITransport(app=self.server.app)

    async def api(self):
        return httpx.AsyncClient(transport=self.transport, base_url="http://test")

    async def wait_for_job(self, client, job_id, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            response = await client.get("/setup/api/backup/jobs")
            self.assertEqual(response.status_code, 200, response.text)
            job = next(j for j in response.json()["jobs"] if j["id"] == job_id)
            if job["status"] != "running":
                return job
            await asyncio.sleep(.04)
        self.fail("backup job never completed")

    async def test_create_returns_while_running_then_download_survives_reload(self):
        self.server.guard = lambda _request: None
        with patch.dict(os.environ, {"FAKE_BACKUP_SLEEP": ".3"}):
            async with await self.api() as client:
                started = await client.post("/setup/api/backup/jobs")
                self.assertEqual(started.status_code, 202)
                job_id = started.json()["job"]["id"]
                self.assertEqual(started.json()["job"]["status"], "running")
                # A second click gets the same job, never a duplicate CLI run.
                again = await client.post("/setup/api/backup/jobs")
                self.assertEqual(again.json()["job"]["id"], job_id)
                # A fresh client has no browser state and still sees progress.
                async with await self.api() as fresh_client:
                    current = (await fresh_client.get("/setup/api/backup/jobs")).json()["jobs"][0]
                    self.assertEqual(current["id"], job_id)
                    self.assertIn(current["phase"], {"preparing", "scanning", "archiving", "verifying"})
                    ready = await self.wait_for_job(fresh_client, job_id)
                    self.assertEqual(ready["status"], "ready")
                    self.assertGreater(ready["size_bytes"], 0)
                    self.assertTrue(ready["download_url"])
                    downloaded = await fresh_client.get(ready["download_url"])
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.headers["content-type"], "application/zip")
                    with zipfile.ZipFile(self.server._manual_backup_paths(job_id)[1]) as zf:
                        self.assertIn("template_manifest.json", zf.namelist())
                    self.assertEqual(downloaded.content[:4], b"PK\x03\x04")

    async def test_partial_archive_warns_and_remains_downloadable(self):
        self.server.guard = lambda _request: None
        (self.home / "state.db").write_bytes(b"live database")
        with patch.dict(os.environ, {"FAKE_BACKUP_EXIT": "1", "FAKE_BACKUP_SKIP_DB": "1"}):
            async with await self.api() as client:
                job_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
                ready = await self.wait_for_job(client, job_id)
                self.assertEqual(ready["status"], "ready")
                self.assertIn("state.db", ready["warning"])
                response = await client.get(ready["download_url"])
                self.assertEqual(response.status_code, 200)
                self.assertIn("incomplete", response.headers["x-backup-warning"])

    async def test_failure_restart_and_expiration_are_visible(self):
        self.server.guard = lambda _request: None
        with patch.dict(os.environ, {"FAKE_BACKUP_EXIT": "2"}):
            async with await self.api() as client:
                job_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
                failed = await self.wait_for_job(client, job_id)
                self.assertEqual(failed["status"], "failed")
                self.assertNotIn("download_url", failed)
                self.assertFalse(self.server._manual_backup_paths(job_id)[0].exists())

        # A container restart loses /tmp but keeps the volume job record.
        interrupted_id = "a" * 24
        jobs = self.server._manual_backup_jobs_read()
        jobs.insert(0, {"id": interrupted_id, "status": "running", "started_at": time.time(),
                        "finished_at": None, "phase": "archiving"})
        self.server._manual_backup_jobs_write(jobs)
        async with await self.api() as client:
            result = (await client.get("/setup/api/backup/jobs")).json()["jobs"][0]
            self.assertEqual(result["status"], "failed")
            self.assertIn("restart", result["error"])

        with patch.dict(os.environ, {"FAKE_BACKUP_EXIT": "0"}):
            async with await self.api() as client:
                new_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
                ready = await self.wait_for_job(client, new_id)
                self.assertEqual(ready["status"], "ready")
                jobs = self.server._manual_backup_jobs_read()
                next(j for j in jobs if j["id"] == new_id)["finished_at"] = time.time() - 7 * 3600
                self.server._manual_backup_jobs_write(jobs)
                expired = (await client.get("/setup/api/backup/jobs")).json()["jobs"][0]
                self.assertEqual(expired["status"], "expired")
                self.assertFalse(self.server._manual_backup_paths(new_id)[1].exists())

    async def test_remove_ready_backup_deletes_zip_but_not_restore_snapshot(self):
        self.server.guard = lambda _request: None
        async with await self.api() as client:
            job_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
            ready = await self.wait_for_job(client, job_id)
            self.assertEqual(ready["status"], "ready")
            archive = self.server._manual_backup_paths(job_id)[1]
            self.assertTrue(archive.exists())
            snapshot = self.home / "backups" / "pre-restore-123-abcd1234.zip"
            snapshot.write_bytes(b"untouched snapshot")

            removed = await client.delete(f"/setup/api/backup/jobs/{job_id}")
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertTrue(removed.json()["ok"])
            self.assertFalse(archive.exists())
            self.assertEqual((await client.get("/setup/api/backup/jobs")).json()["jobs"], [])
            self.assertEqual(snapshot.read_bytes(), b"untouched snapshot")
            self.assertEqual((await client.get(ready["download_url"])).status_code, 404)

    async def test_remove_expired_record_and_protect_running_or_downloaded_backup(self):
        self.server.guard = lambda _request: None
        old_id = "b" * 24
        self.server._manual_backup_jobs_write([{
            "id": old_id, "status": "expired", "phase": "expired",
            "started_at": time.time() - 3600, "finished_at": time.time() - 3500,
            "error": "Prepared backup expired", "warning": None,
        }])
        async with await self.api() as client:
            self.assertEqual((await client.delete(f"/setup/api/backup/jobs/{old_id}")).status_code, 200)
            self.assertEqual((await client.get("/setup/api/backup/jobs")).json()["jobs"], [])
            with patch.dict(os.environ, {"FAKE_BACKUP_SLEEP": ".3"}):
                job_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
                running = await client.delete(f"/setup/api/backup/jobs/{job_id}")
                self.assertEqual(running.status_code, 409)
                self.assertIn("finish", running.json()["error"])
                ready = await self.wait_for_job(client, job_id)
            self.assertEqual(ready["status"], "ready")
            self.server._manual_backup_downloads.add(job_id)
            try:
                downloading = await client.delete(f"/setup/api/backup/jobs/{job_id}")
                self.assertEqual(downloading.status_code, 409)
                self.assertIn("downloaded", downloading.json()["error"])
            finally:
                self.server._manual_backup_downloads.discard(job_id)
            self.assertEqual((await client.delete(f"/setup/api/backup/jobs/{job_id}")).status_code, 200)

    async def test_remove_failure_does_not_hide_undeleted_zip(self):
        self.server.guard = lambda _request: None
        async with await self.api() as client:
            job_id = (await client.post("/setup/api/backup/jobs")).json()["job"]["id"]
            self.assertEqual((await self.wait_for_job(client, job_id))["status"], "ready")
            with patch.object(self.server.shutil, "rmtree", return_value=None):
                response = await client.delete(f"/setup/api/backup/jobs/{job_id}")
            self.assertEqual(response.status_code, 500)
            self.assertEqual((await client.get("/setup/api/backup/jobs")).json()["jobs"][0]["id"], job_id)
            self.assertTrue(self.server._manual_backup_paths(job_id)[1].exists())

    async def test_auth_and_backup_lock(self):
        async with await self.api() as client:
            for method, url in (("GET", "/setup/api/backup/jobs"),
                                ("POST", "/setup/api/backup/jobs"),
                                ("DELETE", "/setup/api/backup/jobs/" + "a" * 24),
                                ("GET", "/setup/api/backup/jobs/" + "a" * 24 + "/download")):
                response = await client.request(method, url)
                self.assertEqual(response.status_code, 401)
        self.server.guard = lambda _request: None
        async with await self.api() as client:
            await self.server.backup_lock.acquire()
            try:
                busy = await client.post("/setup/api/backup/jobs")
                self.assertEqual(busy.status_code, 409)
            finally:
                self.server.backup_lock.release()
            bad_id = await client.get("/setup/api/backup/jobs/bad/download")
            self.assertEqual(bad_id.status_code, 404)
            self.assertEqual((await client.delete("/setup/api/backup/jobs/bad")).status_code, 404)
            self.assertEqual((await client.delete("/setup/api/backup/jobs/" + "a" * 24)).status_code, 404)
