#!/usr/bin/env python3
"""Synthetic, network-free scalability harness for the M3U hardening layer.

It measures parser + URL gate + exact-URL deduplication on synthetic inputs.
It deliberately does not claim FFprobe/network performance.
"""
from __future__ import annotations

import argparse
import gc
try:
    import resource
except ImportError:  # Windows no incluye el módulo POSIX resource.
    resource = None
import tempfile
import time
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from M3U_PRO_V9_ENGINE import dedupe_entries, parse_m3u
from UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE import _quarantine_invalid_urls


def build_dataset(path: Path, n: int) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for i in range(n):
            url = f"http://synthetic.example/{i % max(1, n // 10)}.m3u8"
            if i % 9973 == 0:
                url = "http://synthetic.example:notaport/live"
            f.write(f'#EXTINF:0 group-title="General",Channel {i}\n{url}\n')


def _peak_rss():
    """Return peak RSS in platform-native units, or None where unavailable."""
    if resource is None:
        return None
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError, ValueError):
        return None


def run(n: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="m3u_stress_") as td:
        src = Path(td) / f"synthetic_{n}.m3u"
        build_dataset(src, n)
        gc.collect()
        rss_before = _peak_rss()
        t0 = time.perf_counter()
        parsed = parse_m3u(src)
        t_parse = time.perf_counter() - t0
        t1 = time.perf_counter()
        unique, removed = dedupe_entries(parsed)
        t_dedupe = time.perf_counter() - t1
        t2 = time.perf_counter()
        valid, quarantined = _quarantine_invalid_urls(unique)
        t_gate = time.perf_counter() - t2
        rss_after = _peak_rss()
        return {
            "records": n,
            "parsed": len(parsed),
            "unique": len(unique),
            "duplicates": len(removed),
            "valid_after_gate": len(valid),
            "quarantined": len(quarantined),
            "parse_s": round(t_parse, 3),
            "dedupe_s": round(t_dedupe, 3),
            "url_gate_s": round(t_gate, 3),
            "maxrss_delta": max(0, rss_after - rss_before) if rss_before is not None and rss_after is not None else None,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 50000])
    args = ap.parse_args()
    print("records,parsed,unique,duplicates,valid_after_gate,quarantined,parse_s,dedupe_s,url_gate_s,maxrss_delta")
    for n in args.sizes:
        r = run(n)
        print(",".join(str(r[k]) for k in ("records","parsed","unique","duplicates","valid_after_gate","quarantined","parse_s","dedupe_s","url_gate_s","maxrss_delta")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
