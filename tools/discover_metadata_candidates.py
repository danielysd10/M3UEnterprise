#!/usr/bin/env python3
"""Create a review-only metadata candidate report from public IPTV-org data.

This tool never downloads streams and never declares a community logo official.
It produces evidence for human review before any record is promoted to the
curated `channel_metadata_registry.json` used by the unifier.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_profiles import apply_profile
from safe_http import open_public_http

API_BASE = "https://iptv-org.github.io/api"
# The guide index is materially larger than channels/logos. It is metadata,
# never stream media, but still receives a bounded per-document budget.
MAX_API_BYTES = {"channels": 16 * 1024 * 1024, "logos": 16 * 1024 * 1024, "guides": 64 * 1024 * 1024}
VARIANT = re.compile(r"\s*(?:\[[^\]]*\]|\([^)]*\))\s*$", re.I)
NOISE = re.compile(r"\b(?:fhd|uhd|hd|sd|opc(?:ion)?\.?\s*\d+|mpv|lat(?:in)?\s*america|center|south|north|panregional)\b", re.I)


def key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = VARIANT.sub("", text)
    text = NOISE.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def fetch_json(name: str) -> list[dict]:
    response, final_url = open_public_http(f"{API_BASE}/{name}.json", headers={"User-Agent": "M3U-Enterprise-Metadata-Discovery/1.0", "Accept": "application/json"}, timeout=20)
    with response:
        data = response.read(MAX_API_BYTES[name] + 1)
    if len(data) > MAX_API_BYTES[name]:
        raise RuntimeError(f"{name}.json supera el límite de {MAX_API_BYTES[name]} bytes.")
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, list):
        raise RuntimeError(f"{name}.json no contiene una lista.")
    return parsed


def favorites(path: Path) -> list[dict]:
    group = ""
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = raw.strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]"):
            group = text[1:-1].strip()
            continue
        rows.append({"group": group or "Sin categoría", "name": text, "key": key(text)})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Descubre candidatos de logos/EPG para revisión, sin descargar streams.")
    parser.add_argument("--favorites", type=Path, default=ROOT / "favoritos.txt")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "campaign" / "metadata_candidates.json")
    parser.add_argument("--profile", choices=("development", "campaign", "production"), default="campaign")
    args = parser.parse_args()
    apply_profile(args.profile)
    channels, logos, guides = (fetch_json(name) for name in ("channels", "logos", "guides"))
    by_key: dict[str, list[dict]] = {}
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        for label in [channel.get("name"), *(channel.get("alt_names") or [])]:
            normalized = key(label)
            if normalized:
                candidates = by_key.setdefault(normalized, [])
                # A channel may list its canonical name again as an alias.  It
                # is still one candidate, not an ambiguity.
                if not any(existing.get("id") == channel.get("id") for existing in candidates):
                    candidates.append(channel)
    logos_by_channel: dict[str, list[dict]] = {}
    for logo in logos:
        if isinstance(logo, dict) and logo.get("channel") and logo.get("url"):
            logos_by_channel.setdefault(str(logo["channel"]), []).append(logo)
    guides_by_channel: dict[str, list[dict]] = {}
    for guide in guides:
        if isinstance(guide, dict) and guide.get("channel"):
            guides_by_channel.setdefault(str(guide["channel"]), []).append(guide)

    reviewed, summary = [], {
        "favorites": 0, "unique_candidates": 0, "ambiguous": 0,
        "unmatched": 0, "with_official_website_candidate": 0,
        "with_community_logo_candidate": 0, "with_epg_source_url": 0,
        "with_epg_mapping_metadata": 0,
    }
    for favorite in favorites(args.favorites):
        summary["favorites"] += 1
        matches = by_key.get(favorite["key"], [])
        if len(matches) != 1:
            summary["unmatched" if not matches else "ambiguous"] += 1
            reviewed.append({**favorite, "status": "UNMATCHED" if not matches else "AMBIGUOUS", "candidate_channel_ids": [item.get("id") for item in matches[:10]]})
            continue
        channel = matches[0]
        channel_id = str(channel.get("id"))
        logo_candidates = [item.get("url") for item in logos_by_channel.get(channel_id, []) if item.get("in_use")][:3]
        guide_records = guides_by_channel.get(channel_id, [])
        guide_sources = [source.get("url") for item in guide_records for source in item.get("sources", []) if isinstance(source, dict) and source.get("url")][:3]
        guide_mappings = [{key: item.get(key) for key in ("site", "site_name", "site_id", "feed", "lang")} for item in guide_records[:3]]
        summary["unique_candidates"] += 1
        summary["with_official_website_candidate"] += int(bool(channel.get("website")))
        summary["with_community_logo_candidate"] += int(bool(logo_candidates))
        summary["with_epg_source_url"] += int(bool(guide_sources))
        summary["with_epg_mapping_metadata"] += int(bool(guide_mappings))
        reviewed.append({
            **favorite, "status": "REVIEW_REQUIRED", "channel_id": channel_id,
            "channel_name": channel.get("name"), "official_website_candidate": channel.get("website"),
            "community_logo_candidates": logo_candidates,
            "guide_source_candidates": guide_sources,
            "guide_mapping_metadata": guide_mappings,
            "provenance": "iptv-org community metadata; requires official/source and licensing review before promotion",
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "profile": args.profile, "sources": {"channels": f"{API_BASE}/channels.json", "logos": f"{API_BASE}/logos.json", "guides": f"{API_BASE}/guides.json"}, "summary": summary, "records": reviewed}
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
