#!/usr/bin/env python3
"""Enrich a finalized M3U with curated official logos and XMLTV identifiers."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from channel_metadata import audit_xmltv_coverage, download_xmltv, enrich_entries, load_registry
from network_runtime import NetworkCancelled, cancel_on_sigint
from M3U_PRO_V9_ENGINE import atomic_write_utf8, build_m3u, parse_m3u


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--scope", choices=("favorites", "all"), default="favorites")
    parser.add_argument("--xmltv-output", type=Path)
    parser.add_argument("--refresh-hours", type=float, default=12.0)
    parser.add_argument("--profile", choices=("development", "campaign", "production"), help="Perfil obligatorio si se descarga XMLTV")
    args = parser.parse_args(argv)
    cancel = threading.Event()
    try:
        with cancel_on_sigint(cancel):
            registry = load_registry(args.registry, cancel_event=cancel)
            entries, summary = enrich_entries(parse_m3u(args.input), registry, scope=args.scope, cancel_event=cancel)
            if args.xmltv_output and registry.get("epg_url"):
                from runtime_profiles import apply_profile
                apply_profile(args.profile)
                summary_download = download_xmltv(
                    registry["epg_url"], args.xmltv_output,
                    max_age_seconds=max(0, int(args.refresh_hours * 60 * 60)), cancel_event=cancel,
                )
                summary["xmltv"] = summary_download
                summary["epg_coverage"] = audit_xmltv_coverage(args.xmltv_output, entries, scope=args.scope, cancel_event=cancel)
            text = build_m3u(entries, epg_url=summary.get("epg_url"))
    except NetworkCancelled:
        cancel.set()
    # Check all stop requests received before the SIGINT handler was restored.
    if cancel.is_set():
        print("Enriquecimiento cancelado.", file=sys.stderr)
        return 130
    atomic_write_utf8(args.output, text, bom=True)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
