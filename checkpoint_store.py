# -*- coding: utf-8 -*-
"""Durable per-job validation checkpoint store.

SQLite is used instead of rewriting one giant JSON object every N records.  The
store is deterministic, resumable and safe to interrupt between transactions.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable


SCHEMA_VERSION = 1


class CheckpointError(RuntimeError):
    """State is incompatible or unsafe; preserve it rather than silently reset."""


def checkpoint_path(base_dir: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or re.fullmatch(r"(?:[0-9a-f]{1,64}|default)", job_id) is None:
        raise CheckpointError("Identificador de checkpoint inválido.")
    return Path(base_dir) / f"validacion_checkpoint_{job_id}.sqlite3"


@contextmanager
def checkpoint_lock(path: Path, *, timeout: float = 30):
    """Serialize initialization/recovery before SQLite can create an empty file.

    The lock file persists; the OS lock is released when the handle closes,
    including process failure. Never unlink a lock another process may be using.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    for candidate in (lock_path, *lock_path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise CheckpointError("La ruta de estado no puede contener enlaces.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+b", buffering=0) as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CheckpointError("Otro proceso está inicializando este checkpoint.") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_checkpoint(conn: sqlite3.Connection, job_id: str) -> None:
    """Inspect identity/schema before enabling writes or accepting a snapshot."""
    try:
        if ([row[1] for row in conn.execute("PRAGMA table_info(metadata)")] != ["key", "value"] or
            [row[1] for row in conn.execute("PRAGMA table_info(results)")] != ["fingerprint", "result_json", "updated"]):
            raise CheckpointError("Esquema de checkpoint incompatible; conserve el archivo.")
        if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:
            raise CheckpointError("Objetos no admitidos en el checkpoint.")
        values = {key: json.loads(value) for key, value in conn.execute(
            "SELECT key,value FROM metadata WHERE key IN ('schema_version','job_id')")}
        if type(values.get("schema_version")) is not int or values["schema_version"] != SCHEMA_VERSION:
            raise CheckpointError("Versión de checkpoint incompatible; conserve el archivo.")
        if values.get("job_id") != job_id:
            raise CheckpointError("El checkpoint pertenece a otro trabajo; conserve el archivo.")
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CheckpointError("Checkpoint ilegible o incompatible; no se ha reiniciado.") from exc


class CheckpointStore:
    def __init__(self, base_dir: Path, job_id: str, *, legacy_json_path: Path | None = None,
                 initial_legacy_data: dict | None = None, allowed_legacy_job_ids: Iterable[str] = ()):
        self.base_dir = Path(base_dir)
        self.path = checkpoint_path(self.base_dir, job_id)
        self.job_id = job_id
        self.created = False
        self.legacy_json_path = legacy_json_path
        with checkpoint_lock(self.path):
            self._open(job_id, initial_legacy_data, allowed_legacy_job_ids)

    def _open(self, job_id, initial_legacy_data, allowed_legacy_job_ids):
        existed = self.path.exists()
        if self.path.is_symlink() or self.path.is_junction():
            raise CheckpointError("El checkpoint no puede ser un enlace.")
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                validate_checkpoint(self.conn, job_id)
            elif existed:
                raise CheckpointError("Checkpoint existente sin esquema; no se reemplazará.")
            else:
                self._init_schema(job_id)
                self.created = True
                if initial_legacy_data is not None:
                    self.import_legacy_json(initial_legacy_data, allowed_job_ids=allowed_legacy_job_ids)
                    if self.legacy_json_path is not None:
                        self.set_meta("legacy_checkpoint_source", str(self.legacy_json_path))
            self.conn.commit()
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise

    def _init_schema(self, job_id: str) -> None:
        self.conn.execute("""CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        self.conn.execute("""CREATE TABLE results (
            fingerprint TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            updated REAL NOT NULL
        )""")
        self.conn.execute("CREATE INDEX idx_results_updated ON results(updated)")
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.set_meta("job_id", job_id)
        self.set_meta("updated", time.time())

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def set_meta(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.conn.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, payload),
        )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM results").fetchone()
        return int(row[0] or 0)

    def has(self, fingerprint: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM results WHERE fingerprint=?", (fingerprint,)).fetchone()
        return row is not None

    def get(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT result_json FROM results WHERE fingerprint=?", (fingerprint,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def get_many(self, fingerprints: Iterable[str], chunk_size: int = 500) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        batch: list[str] = []
        for fp in fingerprints:
            batch.append(fp)
            if len(batch) >= chunk_size:
                self._load_batch(batch, result)
                batch.clear()
        if batch:
            self._load_batch(batch, result)
        return result

    def get_many_fresh(
        self,
        fingerprints: Iterable[str],
        *,
        success_ttl_seconds: float,
        failure_ttl_seconds: float,
        now: float | None = None,
        chunk_size: int = 500,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Load only remote-validation results that are still fresh.

        A checkpoint proves that a result was recorded; it does not prove that a
        remote stream is still available.  Successful observations can be reused
        briefly to resume an interrupted job, while failures have a shorter TTL
        because transient network faults should be retried sooner.
        """
        fresh: dict[str, dict[str, Any]] = {}
        expired = 0
        at = time.time() if now is None else float(now)
        batch: list[str] = []
        for fp in fingerprints:
            batch.append(fp)
            if len(batch) >= chunk_size:
                expired += self._load_fresh_batch(batch, fresh, at, success_ttl_seconds, failure_ttl_seconds)
                batch.clear()
        if batch:
            expired += self._load_fresh_batch(batch, fresh, at, success_ttl_seconds, failure_ttl_seconds)
        return fresh, expired

    def get_all(self) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        rows = self.conn.execute("SELECT fingerprint,result_json FROM results").fetchall()
        for fp, payload in rows:
            try:
                value = json.loads(payload)
                if isinstance(value, dict):
                    output[fp] = value
            except Exception:
                continue
        return output

    def _load_batch(self, batch: list[str], output: dict[str, dict[str, Any]]) -> None:
        placeholders = ",".join("?" for _ in batch)
        rows = self.conn.execute(
            f"SELECT fingerprint,result_json FROM results WHERE fingerprint IN ({placeholders})", batch
        ).fetchall()
        for fp, payload in rows:
            try:
                value = json.loads(payload)
                if isinstance(value, dict):
                    output[fp] = value
            except Exception:
                continue

    def _load_fresh_batch(
        self,
        batch: list[str],
        output: dict[str, dict[str, Any]],
        now: float,
        success_ttl_seconds: float,
        failure_ttl_seconds: float,
    ) -> int:
        placeholders = ",".join("?" for _ in batch)
        rows = self.conn.execute(
            f"SELECT fingerprint,result_json,updated FROM results WHERE fingerprint IN ({placeholders})", batch
        ).fetchall()
        expired = 0
        for fp, payload, updated in rows:
            try:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    continue
                ttl = success_ttl_seconds if bool(value.get("ok")) else failure_ttl_seconds
                if float(updated) + max(0.0, float(ttl)) < now:
                    expired += 1
                    continue
                output[fp] = value
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return expired

    def put_many(self, items: Iterable[tuple[str, dict[str, Any]]]) -> int:
        rows = []
        now = time.time()
        for fp, result in items:
            rows.append((fp, json.dumps(result, ensure_ascii=False, separators=(",", ":")), now))
        if not rows:
            return 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT INTO results(fingerprint,result_json,updated) VALUES(?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET result_json=excluded.result_json, updated=excluded.updated",
                rows,
            )
            self.set_meta("updated", now)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows)

    def mark_stage(self, stage: str, *, completed: bool | None = None) -> None:
        self.set_meta("stage", stage)
        if completed is not None:
            self.set_meta("completed", bool(completed))
        self.set_meta("updated", time.time())

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "job_id": self.get_meta("job_id", ""),
            "stage": self.get_meta("stage", "UNKNOWN"),
            "completed": bool(self.get_meta("completed", False)),
            "results_count": self.count(),
            "updated": self.get_meta("updated"),
        }

    def import_legacy_json(self, data: dict[str, Any], *, allowed_job_ids: Iterable[str] = ()) -> int:
        if not isinstance(data, dict) or data.get("job_id") not in {self.job_id, *allowed_job_ids}:
            raise CheckpointError("Identidad JSON incompatible, incluso si está completado.")
        if self.count():
            return 0  # Existing observations always take precedence.
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict) or not results:
            return 0
        timestamp = data.get("updated", 0)
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp) or
                timestamp < 0 or timestamp > time.time()):
            timestamp = 0  # Unknown/future dates must not rejuvenate observations.
        rows = [(str(fp), json.dumps(value, ensure_ascii=False, separators=(",", ":")), timestamp)
                for fp, value in results.items() if isinstance(value, dict)]
        owns_transaction = not self.conn.in_transaction
        if owns_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.count():  # Recheck after taking the write lock.
                if owns_transaction:
                    self.conn.rollback()
                return 0
            self.conn.executemany("INSERT INTO results(fingerprint,result_json,updated) VALUES(?,?,?)", rows)
            self.set_meta("stage", str(data.get("stage") or "VALIDATION_INCOMPLETE"))
            self.set_meta("completed", bool(data.get("completed")))
            self.set_meta("updated", timestamp)
            self.set_meta("legacy_imported", True)
            if owns_transaction:
                self.conn.commit()
        except Exception:
            if owns_transaction:
                self.conn.rollback()
            raise
        return len(rows)
