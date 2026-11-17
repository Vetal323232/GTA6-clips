"""SQLite storage with numbered schema migrations.

Each migration runs once, in order, inside a transaction; the applied version
is kept in `PRAGMA user_version`. Add new migrations to the end of MIGRATIONS,
never edit an applied one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        path TEXT NOT NULL,
        filename TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        duration REAL,
        has_video INTEGER NOT NULL DEFAULT 1,
        has_audio INTEGER NOT NULL DEFAULT 1,
        width INTEGER, height INTEGER, fps REAL,
        game TEXT,
        attribution TEXT,
        usage_notes TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        imported_at REAL NOT NULL,
        -- platform metadata (nullable, filled by Milestone 2 discovery)
        platform TEXT, platform_clip_id TEXT, original_url TEXT, channel TEXT,
        creator TEXT, title TEXT, platform_created_at TEXT, platform_duration REAL,
        language TEXT, view_count INTEGER, view_count_at TEXT
    );
    CREATE INDEX sources_path ON sources(path);

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        source_id INTEGER REFERENCES sources(id),
        candidate_id INTEGER,
        dedupe_key TEXT NOT NULL,
        params TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        progress REAL NOT NULL DEFAULT 0,
        message TEXT,
        error TEXT,
        result TEXT,
        created_at REAL NOT NULL,
        started_at REAL, finished_at REAL,
        elapsed REAL,
        hardware TEXT,
        signals TEXT,
        peak_rss_mb REAL
    );
    CREATE INDEX jobs_dedupe ON jobs(dedupe_key, status);

    CREATE TABLE analyses (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        fingerprint TEXT NOT NULL,
        cache_key TEXT NOT NULL UNIQUE,
        detector_version TEXT NOT NULL,
        params TEXT NOT NULL,
        signals TEXT NOT NULL,
        features_path TEXT,
        job_id INTEGER,
        created_at REAL NOT NULL
    );

    CREATE TABLE transcripts (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        fingerprint TEXT NOT NULL,
        cache_key TEXT NOT NULL UNIQUE,
        model TEXT, device TEXT, compute_type TEXT,
        language TEXT, language_probability REAL,
        status TEXT NOT NULL,
        message TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE transcript_segments (
        id INTEGER PRIMARY KEY,
        transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
        start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL,
        avg_logprob REAL, no_speech_prob REAL
    );
    CREATE INDEX transcript_segments_t ON transcript_segments(transcript_id, start);

    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY,
        analysis_id INTEGER NOT NULL REFERENCES analyses(id),
        source_id INTEGER NOT NULL REFERENCES sources(id),
        rank INTEGER NOT NULL,
        start REAL NOT NULL, end REAL NOT NULL, anchor REAL NOT NULL,
        auto_start REAL NOT NULL, auto_end REAL NOT NULL,
        score REAL NOT NULL,
        contributions TEXT NOT NULL,
        explanation TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        reviewed_at REAL,
        edited INTEGER NOT NULL DEFAULT 0,
        flags TEXT,
        layout TEXT,
        crop TEXT,
        burn_subtitles INTEGER
    );
    CREATE INDEX candidates_analysis ON candidates(analysis_id, rank);

    CREATE TABLE exports (
        id INTEGER PRIMARY KEY,
        candidate_id INTEGER NOT NULL REFERENCES candidates(id),
        export_key TEXT NOT NULL,
        path TEXT NOT NULL,
        srt_path TEXT,
        status TEXT NOT NULL,
        forced INTEGER NOT NULL DEFAULT 0,
        settings TEXT NOT NULL,
        start REAL, end REAL,
        duration REAL, width INTEGER, height INTEGER, vcodec TEXT, acodec TEXT,
        error TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX exports_key ON exports(export_key, status);

    CREATE TABLE marked_intervals (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        start REAL NOT NULL, end REAL NOT NULL,
        note TEXT,
        created_at REAL NOT NULL
    );

    CREATE TABLE evaluations (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES sources(id),
        analysis_id INTEGER,
        report TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    """,
    # 2: analyses can be incomplete (an optional signal was requested but failed for a
    # fixable reason, e.g. missing speech model) and are then retried, not reused.
    """
    ALTER TABLE analyses ADD COLUMN complete INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE analyses ADD COLUMN incomplete_reason TEXT;
    ALTER TABLE analyses ADD COLUMN superseded_by INTEGER;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    """Thread-safe wrapper: one connection guarded by a lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"База данных создана более новой версией программы (схема {version}, "
                    f"поддерживается {SCHEMA_VERSION})"
                )
            for number in range(version, SCHEMA_VERSION):
                self.conn.execute("BEGIN")
                try:
                    for statement in _split_sql(MIGRATIONS[number]):
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version = {number + 1}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def insert(self, table: str, values: dict[str, Any]) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._lock:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))

    def transaction(self):
        return _Tx(self)

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False


def _split_sql(script: str) -> list[str]:
    parts, buf = [], []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


def now() -> float:
    return time.time()
