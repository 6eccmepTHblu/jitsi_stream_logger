"""Регрессионные тесты критических сценариев потери данных и авторизации."""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


# Аппаратные рекордеры не нужны этим тестам. Подменяем только тяжёлые модули,
# чтобы проверки безопасности работали без аудиоустройств и Windows Capture.
audio_stub = types.ModuleType("app.recorder.audio")
audio_stub.AudioRecorder = object
video_stub = types.ModuleType("app.recorder.video")
video_stub.VideoRecorder = object
ws_stub = types.ModuleType("app.ws_server")
ws_stub.WsServer = object
sys.modules.setdefault("app.recorder.audio", audio_stub)
sys.modules.setdefault("app.recorder.video", video_stub)
sys.modules.setdefault("app.ws_server", ws_stub)

from app import transcribe
from app.core import App
from app.session import ActiveCall, SessionManager
from app.storage import Storage


class RecoverySafetyTests(unittest.TestCase):
    def test_recovery_snapshot_is_taken_before_websocket_start(self) -> None:
        source = inspect.getsource(App._amain)
        self.assertLess(source.index("stale_rows = self.storage.stale_calls()"),
                        source.index("await ws.start()"))
        self.assertIn("self._startup_recovery(stale_rows)", source)

    def test_snapshot_does_not_grow_with_new_calls(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            now = time.time()
            old_id = storage.create_call(
                room="old", url="https://meet.jit.si/old", tab_id=1,
                started_ts=now - 10, recorded=True, call_dir="old")
            snapshot = storage.stale_calls()
            new_id = storage.create_call(
                room="live", url="https://meet.jit.si/live", tab_id=2,
                started_ts=now, recorded=True, call_dir="live")
            self.assertEqual([row["id"] for row in snapshot], [old_id])
            self.assertNotIn(new_id, [row["id"] for row in snapshot])
        finally:
            storage.close()

    def test_interrupted_processing_is_released_after_restart(self) -> None:
        storage = Storage(Path(":memory:"))
        try:
            now = time.time()
            call_id = storage.create_call(
                room="done", url="", tab_id=1, started_ts=now,
                recorded=True, call_dir="done")
            storage.set_call_status(call_id, "done")
            self.assertEqual(
                storage.begin_processing(call_id, "transcribing"), "done")
            self.assertEqual(storage.reset_interrupted_processing(), 1)
            self.assertEqual(storage.get_call(call_id)["status"], "done")
        finally:
            storage.close()


class RetentionSafetyTests(unittest.TestCase):
    def test_retention_deletes_only_owned_finished_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records = base / "records"
            records.mkdir()
            owned = records / "owned"
            unrelated = records / "unrelated"
            outside = base / "outside"
            for path in (owned, unrelated, outside):
                path.mkdir()

            storage = Storage(Path(":memory:"))
            try:
                started = time.time() - 3 * 86400
                owned_id = storage.create_call(
                    room="owned", url="", tab_id=1, started_ts=started,
                    recorded=True, call_dir=str(owned))
                storage.finish_call(owned_id, started + 60, started, "left")
                storage.set_call_status(owned_id, "done")
                outside_id = storage.create_call(
                    room="outside", url="", tab_id=2, started_ts=started,
                    recorded=True, call_dir=str(outside))
                storage.finish_call(outside_id, started + 60, started, "left")
                storage.set_call_status(outside_id, "done")

                app = App(SimpleNamespace(records_dir=records))
                app.storage = storage
                app.sm = SimpleNamespace(call=None)

                app._run_retention(0)
                self.assertTrue(owned.exists())
                previous = storage.begin_processing(owned_id, "transcribing")
                self.assertEqual(previous, "done")
                app._run_retention(1)
                self.assertTrue(owned.exists())
                storage.finish_processing(
                    owned_id, "transcribing", previous)
                app._run_retention(1)
                self.assertFalse(owned.exists())
                self.assertTrue(unrelated.exists())
                self.assertTrue(outside.exists())
            finally:
                storage.close()


class QueueSafetyTests(unittest.TestCase):
    def test_audio_format_setting_is_respected(self) -> None:
        call_dir = Path("call")
        self.assertEqual(
            transcribe.pick_audio(
                SimpleNamespace(tr_upload="ogg"), call_dir).name,
            "mix.ogg")
        self.assertEqual(
            transcribe.pick_audio(
                SimpleNamespace(tr_upload="wav"), call_dir).name,
            "mix_16k_mono.wav")

    def test_ack_preserves_tasks_added_after_peek(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"APPDATA": tmp}):
                transcribe.queue_path().parent.mkdir(parents=True, exist_ok=True)
                transcribe.enqueue(1, "stt")
                first = transcribe.peek_task()
                self.assertEqual(first, {"call_id": 1, "action": "stt"})
                transcribe.enqueue(2, "summary")
                transcribe.ack_task(first)
                self.assertEqual(
                    transcribe.peek_task(),
                    {"call_id": 2, "action": "summary"})


class ShutdownSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_running_finalization(self) -> None:
        sm = SessionManager(SimpleNamespace(), object())
        call = ActiveCall(
            call_id=1, tab_id=1, room="room", url="", title="",
            started_ts=time.time(), call_dir=None, recorded=False)
        sm.call = call
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_finalize(_call, _reason):
            started.set()
            await release.wait()

        sm._finalize = fake_finalize
        sm._schedule_finalize(call, "left")
        await started.wait()

        shutdown = asyncio.create_task(sm.shutdown())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await asyncio.wait_for(shutdown, timeout=1)


class ExtensionOriginTests(unittest.TestCase):
    def test_manifest_key_matches_server_extension_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (root / "extension" / "manifest.json").read_text(encoding="utf-8"))
        public_key = base64.b64decode(manifest["key"])
        digest = hashlib.sha256(public_key).digest()[:16]
        extension_id = "".join(
            chr(ord("a") + nibble)
            for byte in digest
            for nibble in (byte >> 4, byte & 0x0F))

        tree = ast.parse(
            (root / "app" / "ws_server.py").read_text(encoding="utf-8"))
        server_source = (
            root / "app" / "ws_server.py").read_text(encoding="utf-8")
        self.assertIn("if origin != EXTENSION_ORIGIN:", server_source)
        self.assertNotIn('origin.startswith("chrome-extension://")',
                         server_source)
        server_id = None
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "EXTENSION_ID"
                            for target in node.targets)):
                server_id = ast.literal_eval(node.value)
                break
        self.assertEqual(server_id, extension_id)

    def test_fake_extension_uses_allowed_origin(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "tools" / "fake_extension.py").read_text(encoding="utf-8")
        self.assertIn("origin=EXTENSION_ORIGIN", source)
        self.assertNotIn("chrome-extension://faketest", source)

    def test_public_defaults_do_not_contain_internal_hosts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (
                "README.md", "app/config.py", "extension/manifest.json"))
        for marker in ("s" + "gaz", "test-" + "dell", "vks" + "03",
                       "cyan" + "kiwi"):
            self.assertNotIn(marker, text.lower())


if __name__ == "__main__":
    unittest.main()
