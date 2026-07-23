"""SQLite-хранилище журнала созвонов. Все вызовы — из одного потока (asyncio-цикла)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT,
    url TEXT,
    tab_id INTEGER,
    started_at TEXT,
    ended_at TEXT,
    duration_sec REAL,
    end_reason TEXT,
    status TEXT NOT NULL DEFAULT 'recording',
    -- recording|finalizing|transcribing|summarizing|done|log_only|error
    recorded INTEGER NOT NULL DEFAULT 0,
    dir TEXT,
    video_path TEXT,
    media_path TEXT,
    mic_path TEXT,
    speakers_path TEXT,
    asr_wav_path TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS participants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    jitsi_id TEXT,
    name TEXT,
    is_local INTEGER NOT NULL DEFAULT 0,
    joined_at TEXT,
    left_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_participants_call ON participants(call_id);
CREATE INDEX IF NOT EXISTS idx_events_call ON events(call_id);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts_iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts).astimezone().isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(calls)")]
        for col in ("transcript_path", "summary_path"):  # миграции ранних версий
            if col not in cols:
                self.conn.execute(f"ALTER TABLE calls ADD COLUMN {col} TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- calls ---

    def create_call(self, *, room: str, url: str, tab_id: int, started_ts: float,
                    recorded: bool, call_dir: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO calls(room, url, tab_id, started_at, status, recorded, dir) "
            "VALUES(?,?,?,?,?,?,?)",
            (room, url, tab_id, ts_iso(started_ts),
             "recording" if recorded else "log_only", int(recorded), call_dir),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_call_status(self, call_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE calls SET status=?, error=COALESCE(?, error) WHERE id=?",
                          (status, error, call_id))
        self.conn.commit()

    def begin_processing(self, call_id: int, status: str) -> str | None:
        """Атомарно захватывает завершённую запись для STT или резюме."""
        if status not in ("transcribing", "summarizing"):
            raise ValueError(f"Недопустимый статус обработки: {status}")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row["status"] not in ("done", "log_only", "error"):
                self.conn.rollback()
                return None
            previous = str(row["status"])
            self.conn.execute(
                "UPDATE calls SET status=? WHERE id=?", (status, call_id))
            self.conn.commit()
            return previous
        except Exception:
            self.conn.rollback()
            raise

    def finish_processing(self, call_id: int, status: str,
                          previous_status: str) -> None:
        """Возвращает прежний статус, только если захват всё ещё наш."""
        self.conn.execute(
            "UPDATE calls SET status=? WHERE id=? AND status=?",
            (previous_status, call_id, status))
        self.conn.commit()

    def reset_interrupted_processing(self) -> int:
        """После подтверждённого рестарта освобождает оборванные сетевые задачи."""
        cur = self.conn.execute(
            "UPDATE calls SET status='done' "
            "WHERE status IN ('transcribing','summarizing')")
        self.conn.commit()
        return int(cur.rowcount)

    def finish_call(self, call_id: int, ended_ts: float, started_ts: float, reason: str) -> None:
        self.conn.execute(
            "UPDATE calls SET ended_at=?, duration_sec=?, end_reason=? WHERE id=?",
            (ts_iso(ended_ts), round(max(0.0, ended_ts - started_ts), 1), reason, call_id),
        )
        self.conn.commit()

    def set_call_files(self, call_id: int, **paths: str | None) -> None:
        allowed = {"video_path", "media_path", "mic_path", "speakers_path",
                   "asr_wav_path", "transcript_path", "summary_path"}
        sets, vals = [], []
        for k, v in paths.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if sets:
            vals.append(call_id)
            self.conn.execute(f"UPDATE calls SET {', '.join(sets)} WHERE id=?", vals)
            self.conn.commit()

    def get_call(self, call_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()

    def stale_calls(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM calls WHERE status IN ('recording','finalizing')").fetchall()

    def retention_candidates(self) -> list[sqlite3.Row]:
        """Возвращает только завершённые записи с собственной папкой."""
        return self.conn.execute(
            "SELECT id, dir, started_at, ended_at, status FROM calls "
            "WHERE dir IS NOT NULL AND status IN ('done','log_only','error')"
        ).fetchall()

    # --- participants ---

    def participant_joined(self, call_id: int, jitsi_id: str, name: str,
                           is_local: bool, ts: float) -> None:
        row = self.conn.execute(
            "SELECT id, name FROM participants WHERE call_id=? AND jitsi_id=? AND left_at IS NULL",
            (call_id, jitsi_id)).fetchone()
        if row:
            if name and row["name"] != name:
                self.conn.execute("UPDATE participants SET name=? WHERE id=?", (name, row["id"]))
                self.conn.commit()
            return
        self.conn.execute(
            "INSERT INTO participants(call_id, jitsi_id, name, is_local, joined_at) VALUES(?,?,?,?,?)",
            (call_id, jitsi_id, name, int(is_local), ts_iso(ts)))
        self.conn.commit()

    def participant_left(self, call_id: int, jitsi_id: str, ts: float) -> None:
        self.conn.execute(
            "UPDATE participants SET left_at=? WHERE call_id=? AND jitsi_id=? AND left_at IS NULL",
            (ts_iso(ts), call_id, jitsi_id))
        self.conn.commit()

    def close_open_participants(self, call_id: int, ts: float) -> None:
        self.conn.execute(
            "UPDATE participants SET left_at=? WHERE call_id=? AND left_at IS NULL",
            (ts_iso(ts), call_id))
        self.conn.commit()

    def call_participants(self, call_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM participants WHERE call_id=? ORDER BY joined_at", (call_id,)).fetchall()

    # --- events ---

    def add_event(self, call_id: int | None, ts: float, etype: str, payload: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(call_id, ts, type, payload) VALUES(?,?,?,?)",
            (call_id, ts_iso(ts), etype, json.dumps(payload, ensure_ascii=False) if payload else None))
        self.conn.commit()

    def call_events(self, call_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE call_id=? ORDER BY id", (call_id,)).fetchall()
