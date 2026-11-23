#!/usr/bin/env python3
"""Run a bounded, auditable FFprobe campaign against public playlist URLs.

This tool intentionally samples candidates.  It does not claim that its result
represents every stream in the source collection and it never accepts private or
DNS-resolved-private network targets.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DEFAULT = ROOT.parents[0] / "ListasUnificadas"
URL_LINE = re.compile(r"(?i)^(?:https?|rtsp|rtmp|rtmps)://\S+$")


def candidates(source: Path, limit: int, per_host: int):
    from url_safety import URLStatus, inspect_url
    selected, seen, host_counts = [], set(), {}
    stats = {"raw_url_lines": 0, "unique_url_lines": 0, "static_rejected": 0}
    for path in sorted(source.rglob("*"), key=lambda p: str(p).casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".m3u", ".m3u8"}:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            url = raw.strip()
            if not URL_LINE.match(url):
                continue
            stats["raw_url_lines"] += 1
            if url in seen:
                continue
            seen.add(url); stats["unique_url_lines"] += 1
            inspection = inspect_url(url)
            if inspection.status not in {URLStatus.VALID, URLStatus.REPAIRED}:
                stats["static_rejected"] += 1
                continue
            host = (urlsplit(inspection.normalized_url).hostname or "").casefold()
            if host_counts.get(host, 0) >= per_host:
                continue
            host_counts[host] = host_counts.get(host, 0) + 1
            selected.append({"name": f"Campaign candidate {len(selected)+1}", "url": inspection.normalized_url, "directives": [], "source": str(path)})
            if len(selected) >= limit:
                return selected, stats
    return selected, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE_DEFAULT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--metrics-store", type=Path)
    parser.add_argument("--profile", choices=("development", "campaign", "production"), default="campaign")
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--per-host", type=int, default=2)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args()
    if os.environ.get("M3U_ALLOW_PRIVATE_NETWORKS", "").strip() == "1":
        parser.error("La campaña pública requiere desactivar M3U_ALLOW_PRIVATE_NETWORKS.")
    import sys
    sys.path.insert(0, str(ROOT))
    from runtime_profiles import apply_profile
    profile = apply_profile(args.profile)
    if not args.source.is_dir():
        raise RuntimeError(f"Fuente no encontrada: {args.source}")
    if args.limit < 1 or args.workers < 1 or args.workers > profile.max_workers or args.per_host < 1 or args.per_host > profile.max_per_host:
        raise RuntimeError(f"Límites inválidos para {profile.name}: workers <= {profile.max_workers}, per-host <= {profile.max_per_host}.")
    if args.output is None:
        args.output = ROOT / "artifacts" / profile.name / f"campaign-{int(time.time())}"
    if args.metrics_store is None:
        args.metrics_store = ROOT / "artifacts" / profile.name / "metrics" / "history.sqlite3"
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        raise RuntimeError(f"Salida ya contiene evidencia; use otra carpeta: {args.output}")

    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    from metrics_store import record_campaign
    from network_runtime import cancel_on_sigint
    from url_safety import redact_url_for_export

    entries, sampling = candidates(args.source, args.limit, args.per_host)
    if not entries:
        raise RuntimeError("No se encontraron URLs públicas elegibles para la campaña.")
    enterprise.CHECKPOINT_BASE_DIR = args.output / "checkpoint"
    enterprise.CHECKPOINT_BASE_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"live-{int(time.time())}".encode().hex()[:64]
    updates = []
    started = time.perf_counter()
    def progress(done, total, stage):
        updates.append({"done": done, "total": total, "stage": stage, "seconds": round(time.perf_counter() - started, 3)})
        print(f"progress={done}/{total} stage={stage}", flush=True)

    cancel_event = threading.Event()
    try:
        with cancel_on_sigint(cancel_event):
            kept, dead, _ = enterprise.enterprise_validate_all(
                entries, True, args.timeout, args.workers, progress, cancel_event, job_id)
    except enterprise.UserCancelled:
        if not cancel_event.is_set():
            raise
    if cancel_event.is_set():
        print("Campaña interrumpida durante la validación.", file=sys.stderr)
        return 130
    elapsed = time.perf_counter() - started
    rows = []
    metric_rows = []
    for entry in kept + dead:
        result = entry.get("validation_enterprise", {})
        metric_rows.append({"url": entry.get("url", ""), "status": result.get("status", "UNKNOWN"), "ok": bool(result.get("ok")), "latency_ms": result.get("latency_ms")})
        rows.append({
            "stream_ref": redact_url_for_export(entry.get("url", "")), "status": result.get("status", "UNKNOWN"),
            "ok": bool(result.get("ok")), "latency_ms": result.get("latency_ms"), "reason": result.get("reason", ""),
        })
    with (args.output / "results.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["stream_ref", "status", "ok", "latency_ms", "reason"])
        writer.writeheader(); writer.writerows(rows)
    metrics = getattr(enterprise.enterprise_validate_all, "last_metrics", {})
    historical_metrics = record_campaign(args.metrics_store, profile.name, metric_rows)
    summary = {
        "executed_utc": datetime.now(timezone.utc).isoformat(),
        "source_id": hashlib.sha256(str(args.source.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16],
        "profile": profile.name, "sample_limit": args.limit, "per_host_limit": args.per_host, "workers": args.workers,
        "timeout_seconds": args.timeout, "sampled": len(entries), "kept": len(kept), "not_kept": len(dead),
        "elapsed_seconds": round(elapsed, 3), "sampling": sampling, "validation_metrics": metrics, "historical_metrics": historical_metrics,
        "limitation": "Muestra controlada de URLs públicas; no certifica toda la colección ni la disponibilidad futura.",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "progress.json").write_text(json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
