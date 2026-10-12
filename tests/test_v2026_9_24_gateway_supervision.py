"""Gateway supervisor exit verdicts for native Dashboard Stop and restart."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]


class FinishedProcess:
    pid = 1234

    def __init__(self, returncode: int):
        self.returncode = returncode
        self.stdout = self._output()

    @staticmethod
    async def _output():
        if False:
            yield b""

    async def wait(self):
        return self.returncode


class DelayedOutputProcess:
    """A terminated child whose stdout drain lags behind wait()."""

    pid = 4321

    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.output_done = asyncio.Event()
        self.stdout = self._output()

    async def _output(self):
        await self.output_done.wait()
        if False:
            yield b""

    def terminate(self):
        # Hermes exits 1 after the wrapper's SIGTERM in the local image.
        self.returncode = 1
        self.exited.set()

    def kill(self):
        self.terminate()

    async def wait(self):
        await self.exited.wait()
        return self.returncode


class GatewayExitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-gateway-exit-")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / ".hermes"
        home.mkdir()
        env = patch.dict(os.environ, {
            "HERMES_HOME": str(home),
            "ADMIN_PASSWORD": "test-password",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(
            f"hermes_gateway_exit_test_{uuid.uuid4().hex}", ROOT / "server.py"
        )
        assert spec and spec.loader
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        self.server = server

    async def test_native_dashboard_clean_stop_stays_stopped(self):
        gateway = self.server.Gateway()
        process = FinishedProcess(0)
        gateway.proc = process
        gateway.state = "running"
        gateway.started_at = 123.0
        gateway._supervise_respawn = AsyncMock()

        await gateway._drain(process)
        await asyncio.sleep(0)

        self.assertEqual(gateway.state, "stopped")
        self.assertIsNone(gateway.started_at)
        gateway._supervise_respawn.assert_not_awaited()

    async def test_restart_and_unexpected_exits_are_still_supervised(self):
        for code in (75, 1, -9):
            with self.subTest(returncode=code):
                gateway = self.server.Gateway()
                process = FinishedProcess(code)
                gateway.proc = process
                gateway._supervise_respawn = AsyncMock()

                await gateway._drain(process)
                await asyncio.sleep(0)

                gateway._supervise_respawn.assert_awaited_once_with(process.pid)

    async def test_fatal_config_exit_and_wrapper_stop_do_not_respawn(self):
        fatal = self.server.Gateway()
        fatal_process = FinishedProcess(78)
        fatal.proc = fatal_process
        fatal._supervise_respawn = AsyncMock()
        await fatal._drain(fatal_process)
        self.assertEqual(fatal.state, "crashed")
        fatal._supervise_respawn.assert_not_awaited()

        deliberate = self.server.Gateway()
        deliberate_process = FinishedProcess(1)
        deliberate.proc = deliberate_process
        deliberate._stopping = True
        deliberate._supervise_respawn = AsyncMock()
        await deliberate._drain(deliberate_process)
        deliberate._supervise_respawn.assert_not_awaited()

    async def test_wrapper_restart_does_not_charge_old_exit_during_new_start(self):
        for already_exited in (False, True):
            with self.subTest(already_exited=already_exited):
                gateway = self.server.Gateway()
                old = DelayedOutputProcess()
                gateway.proc = old
                gateway.state = "running"
                gateway._supervise_respawn = AsyncMock()
                drain = asyncio.create_task(gateway._drain(old))
                spawn_entered = asyncio.Event()
                allow_spawn = asyncio.Event()
                if already_exited:
                    old.terminate()  # returncode published before stop() enters

                async def delayed_spawn(*args, **kwargs):
                    spawn_entered.set()
                    await allow_spawn.wait()
                    return FinishedProcess(0)

                with patch.object(self.server, "build_hermes_env", return_value={}), \
                     patch.object(self.server, "read_env", return_value={}), \
                     patch.object(self.server, "write_config_yaml"), \
                     patch.object(self.server.asyncio, "create_subprocess_exec", delayed_spawn):
                    restart = asyncio.create_task(gateway.restart())
                    await asyncio.wait_for(spawn_entered.wait(), timeout=1)
                    # stop() has completed, start() has reset _stopping, and the
                    # old process remains self.proc while the new spawn awaits.
                    self.assertFalse(gateway._stopping)
                    self.assertIs(gateway.proc, old)
                    old.output_done.set()
                    await asyncio.wait_for(drain, timeout=1)
                    gateway._supervise_respawn.assert_not_awaited()
                    self.assertNotIn(old, gateway._planned_stops)
                    self.assertFalse(any("supervising restart" in line for line in gateway.logs))
                    allow_spawn.set()
                    await asyncio.wait_for(restart, timeout=1)

                self.assertEqual(gateway.restarts, 1)
                self.assertEqual(gateway._recent_exits, [])
