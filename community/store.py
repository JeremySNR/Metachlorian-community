"""Postgres in production, SQLite for offline development/tests. Parameterised queries throughout."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .protocol import Contribution, youtube_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, contribution TEXT NOT NULL,
    digest TEXT NOT NULL, richness INTEGER NOT NULL, checked_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, kind TEXT NOT NULL, start_s DOUBLE PRECISION NOT NULL,
    end_s DOUBLE PRECISION NOT NULL, body TEXT NOT NULL, fields TEXT NOT NULL,
    PRIMARY KEY (video_id, idx)
);
CREATE TABLE IF NOT EXISTS suppressed (video_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rate_limits (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
"""


class Store:
    def __init__(self, url: str):
        self.url = url
        self.postgres = url.startswith(("postgres://", "postgresql://"))
        if not self.postgres and not url.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must be a Postgres URL (or sqlite:///path for development)")

    @contextlib.contextmanager
    def connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            c = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        else:
            path = self.url.removeprefix("sqlite:///")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(path, timeout=20)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def execute(self, c, sql, params=()):
        return c.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def init(self):
        with self.connect() as c:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)
            if self.postgres:
                c.execute("CREATE INDEX IF NOT EXISTS documents_search ON documents USING GIN (to_tsvector('simple', body))")

    def rate_limit(self, key: str, maximum: int, seconds: int = 60):
        stamp = time.time()
        # Buckets are fixed windows; the caller supplies a one-way hash, never a persisted client IP.
        bucket = f"{key}:{int(stamp // seconds)}"
        with self.connect() as c:
            self.execute(c, "DELETE FROM rate_limits WHERE expires_at<?", (stamp,))
            r = self.execute(c, "INSERT INTO rate_limits(bucket,count,expires_at) VALUES(?,1,?) ON CONFLICT(bucket)"
                             " DO UPDATE SET count=rate_limits.count+1 RETURNING count", (bucket, stamp + seconds)).fetchone()
            return r["count"] <= maximum

    def suppressed(self, video_id: str) -> bool:
        with self.connect() as c:
            return bool(self.execute(c, "SELECT 1 FROM suppressed WHERE video_id=?", (video_id,)).fetchone())

    def remove(self, video_id: str, permanent: bool = False):
        with self.connect() as c:
            if self.postgres:
                self.execute(c, "SELECT pg_advisory_xact_lock(hashtext(?))", (video_id,))
            self.execute(c, "DELETE FROM videos WHERE video_id=?", (video_id,))
            if permanent:
                self.execute(c, "INSERT INTO suppressed(video_id) VALUES(?) ON CONFLICT(video_id) DO NOTHING", (video_id,))

    def save(self, data: Contribution, metadata: dict) -> str:
        encoded = data.model_dump_json()
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        richness = sum(len(s.fields) for s in data.shots) + len(data.fields) + len(data.moments)
        ts = time.time()
        with self.connect() as c:
            if self.postgres:
                self.execute(c, "SELECT pg_advisory_xact_lock(hashtext(?))", (data.video_id,))
            if self.execute(c, "SELECT 1 FROM suppressed WHERE video_id=?", (data.video_id,)).fetchone():
                return "suppressed"
            # Lock the video row in Postgres so two contributions cannot interleave document replacement.
            suffix = " FOR UPDATE" if self.postgres else ""
            old = self.execute(c, "SELECT digest, richness FROM videos WHERE video_id=?" + suffix, (data.video_id,)).fetchone()
            if old and (old["digest"] == digest or old["richness"] > richness):
                self.execute(c, "UPDATE videos SET metadata=?, checked_at=? WHERE video_id=?", (json.dumps(metadata), ts, data.video_id))
                return "duplicate" if old["digest"] == digest else "retained_richer"
            self.execute(c, "INSERT INTO videos VALUES(?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET"
                         " metadata=excluded.metadata, contribution=excluded.contribution, digest=excluded.digest,"
                         " richness=excluded.richness, checked_at=excluded.checked_at, updated_at=excluded.updated_at",
                         (data.video_id, json.dumps(metadata), encoded, digest, richness, ts, ts))
            self.execute(c, "DELETE FROM documents WHERE video_id=?", (data.video_id,))
            shared = " ".join([metadata["title"], metadata["channel"], text_fields(data.fields)])
            for i, entry in enumerate([*data.shots, *data.moments]):
                shot = hasattr(entry, "fields")
                fields = {k: v.model_dump() for k, v in entry.fields.items()} if shot else {}
                body = shared + " " + (text_fields(entry.fields) if shot else entry.text)
                self.execute(c, "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
                             (data.video_id, i, "shot" if shot else entry.kind, entry.start_s, entry.end_s, body, json.dumps(fields)))
        return "stored"

    def candidates(self, query: str, limit: int, offset: int) -> list[dict]:
        words = re.findall(r"[\w]+", query, flags=re.UNICODE)[:16]
        where, params = [], []
        if words:
            if self.postgres:
                where.append("to_tsvector('simple', d.body) @@ plainto_tsquery('simple', ?)")
                params.append(" ".join(words))
            else:
                for word in words:
                    where.append("lower(d.body) LIKE ? ESCAPE '\\'")
                    params.append("%" + word.lower().replace("_", "\\_") + "%")
        with self.connect() as c:
            sql = "SELECT d.*, v.metadata, v.checked_at FROM documents d JOIN videos v ON v.video_id=d.video_id"
            sql += (" WHERE " + " AND ".join(where)) if where else ""
            sql += " ORDER BY v.updated_at DESC, d.video_id, d.idx LIMIT ? OFFSET ?"
            return [dict(r) for r in self.execute(c, sql, (*params, limit, offset)).fetchall()]

    def checked(self, video_id: str, metadata: dict):
        with self.connect() as c:
            self.execute(c, "UPDATE videos SET checked_at=?, metadata=? WHERE video_id=?", (time.time(), json.dumps(metadata), video_id))


def text_fields(fields) -> str:
    parts = []
    for signal in fields.values():
        value = signal.value
        parts.extend(value if isinstance(value, list) else [str(value)])
    return " ".join(parts)
