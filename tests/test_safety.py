"""Регрессионные тесты критических сценариев потери данных и авторизации."""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
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

from app import records, transcribe
from app.core import App
from app.records import CallLog
from app.session import ActiveCall, SessionManager


def _write_call(call_dir: Path, *, status: str, started_ts: float,
                ended_ts: float | None = None, recorded: bool = True) -> None:
    """Кладёт в папку meta.json нужного статуса (как это делает CallLog)."""
    call_dir.mkdir(parents=True, exist_ok=True)
    log = CallLog(room=call_dir.name, started_ts=started_ts,
                  call_dir=call_dir, recorded=recorded, status=status)
    if ended_ts is not None:
        log.finish(ended_ts, started_ts, "left")
    if not log.write():
        raise OSError(f"Не удалось создать тестовый журнал в {call_dir}")


class FileJournalSafetyTests(unittest.TestCase):
    def test_scan_skips_foreign_directories_and_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records_dir = Path(tmp)
            (records_dir / "stray").mkdir()
            foreign = records_dir / "foreign"
            foreign.mkdir()
            (foreign / "meta.json").write_text(
                json.dumps({
                    "call": {
                        "started_at": "2020-01-01T00:00:00+00:00",
                        "status": "done",
                    },
                }),
                encoding="utf-8")
            _write_call(
                records_dir / "owned", status="done",
                started_ts=time.time() - 10, ended_ts=time.time())

            rows = records.scan_records(records_dir)

            self.assertEqual([Path(row["dir"]).name for row in rows], ["owned"])
            self.assertTrue(rows[0]["owned"])

    def test_legacy_meta_is_migrated_only_with_full_old_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / "legacy"
            call_dir.mkdir()
            call = CallLog(
                room="legacy", started_ts=time.time(), call_dir=call_dir,
                recorded=True)
            legacy = call.to_meta()
            legacy.pop("format")
            (call_dir / "meta.json").write_text(
                json.dumps(legacy), encoding="utf-8")

            rows = records.scan_records(Path(tmp))
            migrated = json.loads(
                (call_dir / "meta.json").read_text(encoding="utf-8"))

            self.assertEqual(len(rows), 1)
            self.assertTrue(records.is_owned_meta(migrated))

    def test_failed_replace_keeps_previous_meta_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / "call"
            call_dir.mkdir()
            call = CallLog(
                room="room", started_ts=time.time(), call_dir=call_dir,
                recorded=True)
            self.assertTrue(call.write())
            before = records.read_meta(call_dir)
            call.add_event(time.time(), "new_event", None)

            with (
                patch("app.records.os.replace", side_effect=OSError("disk")),
                patch.object(records.log, "exception"),
            ):
                self.assertFalse(call.write())

            self.assertEqual(records.read_meta(call_dir), before)
            self.assertFalse(list(call_dir.glob(".meta.json.*.tmp")))

    def test_session_updates_are_persisted_before_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / "call"
            call_dir.mkdir()
            active = ActiveCall(
                call_id=1, tab_id=1, room="room", url="", title="",
                started_ts=time.time(), call_dir=call_dir, recorded=True)
            active.log.add_event(time.time(), "conference_joined", None)
            self.assertTrue(active.log.write())
            sm = SessionManager(SimpleNamespace())

            sm._update_call_details(active, {
                "title": "",
                "participants": [
                    {"id": "u1", "name": "Участник", "local": False},
                ],
                "audioMuted": True,
            }, time.time())
            persisted = records.read_meta(call_dir)

            self.assertEqual(persisted["participants"][0]["jitsi_id"], "u1")
            self.assertIn(
                "mic_muted",
                {event["type"] for event in persisted["events"]})


class JournalOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_record_false_keeps_persistent_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(auto_record=False, records_dir=Path(tmp))
            sm = SessionManager(cfg)
            now = time.time()

            await sm._on_joined(1, {
                "room": "journal",
                "url": "https://meet.jit.si/journal",
                "title": "",
                "via": "test",
            }, now)
            call_dir = sm.call.call_dir
            self.assertIsNotNone(call_dir)
            self.assertTrue((call_dir / "meta.json").exists())

            task = sm._schedule_finalize(sm.call, "left")
            await task
            rows = records.scan_records(Path(tmp))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "done")
            self.assertFalse(
                any(path.name.endswith((".ogg", ".mp4", ".wav"))
                    for path in call_dir.iterdir()))


class RecoverySafetyTests(unittest.TestCase):
    def test_recovery_snapshot_is_taken_before_websocket_start(self) -> None:
        source = inspect.getsource(App._amain)
        self.assertLess(
            source.index("stale_dirs = records.find_interrupted(self.cfg.records_dir)"),
            source.index("await ws.start()"))
        self.assertIn("self._startup_recovery(stale_dirs)", source)

    def test_find_interrupted_lists_only_active_recordings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records_dir = Path(tmp)
            now = time.time()
            _write_call(records_dir / "crashed", status="recording",
                        started_ts=now - 10)
            _write_call(records_dir / "finished", status="done",
                        started_ts=now - 20, ended_ts=now - 15)
            (records_dir / "stray").mkdir()  # чужая папка без meta.json
            interrupted = records.find_interrupted(records_dir)
            self.assertEqual([d.name for d in interrupted], ["crashed"])

    def test_find_interrupted_includes_unfinished_journal_only_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records_dir = Path(tmp)
            _write_call(
                records_dir / "journal", status="log_only",
                started_ts=time.time() - 10, recorded=False)

            interrupted = records.find_interrupted(records_dir)

            self.assertEqual([d.name for d in interrupted], ["journal"])

    def test_interrupted_processing_is_released_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records_dir = Path(tmp)
            now = time.time()
            _write_call(records_dir / "stuck", status="transcribing",
                        started_ts=now, ended_ts=now)
            self.assertEqual(
                records.reset_interrupted_processing(records_dir), 1)
            meta = records.read_meta(records_dir / "stuck")
            self.assertEqual(meta["call"]["status"], "done")


class RetentionSafetyTests(unittest.TestCase):
    def test_retention_deletes_only_finished_records_with_meta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records_dir = base / "records"
            records_dir.mkdir()
            owned = records_dir / "owned"
            stray = records_dir / "stray"       # чужая папка без meta.json
            stray.mkdir()

            started = time.time() - 3 * 86400
            _write_call(owned, status="done", started_ts=started,
                        ended_ts=started + 60)

            app = App(SimpleNamespace(records_dir=records_dir))
            app.sm = SimpleNamespace(call=None)

            app._run_retention(0)               # days=0 — ничего не удаляем
            self.assertTrue(owned.exists())

            records.try_acquire(owned)           # идёт обработка — не трогаем
            app._run_retention(1)
            self.assertTrue(owned.exists())
            records.release(owned)

            app._run_retention(1)                # теперь можно
            self.assertFalse(owned.exists())
            self.assertTrue(stray.exists())      # папку без журнала не трогаем

    def test_retention_skips_foreign_meta_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            records_dir = Path(tmp)
            foreign = records_dir / "foreign"
            foreign.mkdir()
            (foreign / "meta.json").write_text(
                json.dumps({
                    "call": {
                        "started_at": "2020-01-01T00:00:00+00:00",
                        "status": "done",
                    },
                }),
                encoding="utf-8")
            app = App(SimpleNamespace(records_dir=records_dir))
            app.sm = SimpleNamespace(call=None)

            app._run_retention(1)

            self.assertTrue(foreign.exists())


class ProcessingLockTests(unittest.TestCase):
    def test_lock_is_visible_to_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            call_dir = Path(tmp) / "call"
            call_dir.mkdir()
            self.assertTrue(records.try_acquire(call_dir))
            script = (
                "import sys; from pathlib import Path; from app import records; "
                "p=Path(sys.argv[1]); ok=records.try_acquire(p); "
                "print(int(ok)); records.release(p) if ok else None"
            )
            try:
                result = subprocess.run(
                    [sys.executable, "-c", script, str(call_dir)],
                    cwd=Path(__file__).resolve().parents[1],
                    check=True, capture_output=True, text=True)
            finally:
                records.release(call_dir)

            self.assertEqual(result.stdout.strip(), "0")


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
                transcribe.enqueue("rec/a", "stt")
                first = transcribe.peek_task()
                self.assertEqual(first, {"dir": "rec/a", "action": "stt"})
                transcribe.enqueue("rec/b", "summary")
                transcribe.ack_task(first)
                self.assertEqual(
                    transcribe.peek_task(),
                    {"dir": "rec/b", "action": "summary"})

    def test_legacy_queue_is_migrated_or_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"APPDATA": tmp}):
                qp = transcribe.queue_path()
                qp.parent.mkdir(parents=True, exist_ok=True)
                qp.write_text(json.dumps([
                    {"call_id": 7, "action": "stt"},
                    {"call_id": 8, "action": "summary"},
                ]), encoding="utf-8")
                db_path = qp.parent / "calls.db"
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("CREATE TABLE calls(id INTEGER, dir TEXT)")
                    conn.execute(
                        "INSERT INTO calls(id, dir) VALUES(?, ?)",
                        (7, "records/call-7"))
                    conn.commit()
                finally:
                    conn.close()

                migrated, archived = transcribe.migrate_legacy_queue(db_path)

                self.assertEqual((migrated, archived), (1, 1))
                self.assertEqual(
                    transcribe.peek_task(),
                    {"dir": "records/call-7", "action": "stt"})
                legacy = json.loads(
                    transcribe.legacy_queue_path().read_text(encoding="utf-8"))
                self.assertEqual(legacy, [{"call_id": 8, "action": "summary"}])


class ShutdownSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_running_finalization(self) -> None:
        sm = SessionManager(SimpleNamespace())
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
