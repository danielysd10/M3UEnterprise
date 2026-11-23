"""Privacy-preserving historical validation metrics stored in SQLite."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable
from urllib.parse import urlsplit


def provider_id(url: str) -> str:
    host = (urlsplit(url).hostname or "unknown").casefold()
    return "provider-" + hashlib.sha256(host.encode("utf-8", errors="replace")).hexdigest()[:16]


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ((len(ordered) * percentile + 99) // 100) - 1))
    return round(ordered[index], 3)


def record_campaign(path: Path, profile: str, rows: Iterable[dict]) -> dict:
    """Persist aggregate metrics only; URLs and paths never enter the database."""
    grouped: dict[str, list[dict]] = {}
    all_latencies: list[float] = []
    for row in rows:
        key = provider_id(str(row.get("url", "")))
        grouped.setdefault(key, []).append(row)
        latency = row.get("latency_ms")
        if isinstance(latency, (int, float)):
            all_latencies.append(float(latency))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, executed_utc REAL NOT NULL, profile TEXT NOT NULL,
          total INTEGER NOT NULL, succeeded INTEGER NOT NULL, p50_ms REAL, p95_ms REAL
        );
        CREATE TABLE IF NOT EXISTS provider_runs (
          run_id INTEGER NOT NULL, provider_id TEXT NOT NULL, total INTEGER NOT NULL,
          succeeded INTEGER NOT NULL, p50_ms REAL, p95_ms REAL, statuses_json TEXT NOT NULL,
          PRIMARY KEY (run_id, provider_id), FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        """)
        total = sum(len(items) for items in grouped.values())
        succeeded = sum(1 for items in grouped.values() for item in items if item.get("ok"))
        cursor = connection.execute("INSERT INTO runs(executed_utc,profile,total,succeeded,p50_ms,p95_ms) VALUES(?,?,?,?,?,?)", (time.time(), profile, total, succeeded, _percentile(all_latencies, 50), _percentile(all_latencies, 95)))
        run_id = int(cursor.lastrowid)
        for key, items in grouped.items():
            latencies = [float(item["latency_ms"]) for item in items if isinstance(item.get("latency_ms"), (int, float))]
            statuses: dict[str, int] = {}
            for item in items:
                status = str(item.get("status") or "UNKNOWN")
                statuses[status] = statuses.get(status, 0) + 1
            connection.execute("INSERT INTO provider_runs VALUES(?,?,?,?,?,?,?)", (run_id, key, len(items), sum(1 for item in items if item.get("ok")), _percentile(latencies, 50), _percentile(latencies, 95), json.dumps(statuses, sort_keys=True)))
        connection.commit()
        return {"run_id": run_id, "total": total, "succeeded": succeeded, "p50_ms": _percentile(all_latencies, 50), "p95_ms": _percentile(all_latencies, 95), "providers": len(grouped)}
    finally:
        connection.close()
