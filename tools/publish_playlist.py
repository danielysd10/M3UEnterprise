#!/usr/bin/env python3
"""Publish a finalized M3U atomically without exposing its source tree."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(source.read_bytes())
    os.replace(temporary, destination)


def _with_public_epg(raw: bytes, epg_url: str) -> bytes:
    text = raw.decode("utf-8-sig")
    header = next((line for line in text.splitlines() if line.upper().startswith("#EXTM3U")), "")
    if not header:
        raise RuntimeError("La lista no contiene cabecera #EXTM3U.")
    clean = re.sub(r'\s+url-tvg=("[^"]*"|\'[^\']*\'|\S+)', "", header, flags=re.I)
    replacement = f'{clean} url-tvg="{epg_url}"'
    text = text.replace(header, replacement, 1)
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def publish(source: Path, destination_root: Path, public_base_url: str | None = None, epg_source: Path | None = None) -> dict:
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.casefold() not in {".m3u", ".m3u8"}:
        raise RuntimeError("La entrada debe ser un archivo M3U/M3U8 existente.")
    raw = source.read_bytes()
    if not raw.lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"#EXTM3U"):
        raise RuntimeError("La entrada no empieza con la directiva #EXTM3U.")
    target = Path(destination_root).resolve() / "iptv" / "unificada.m3u"
    target.parent.mkdir(parents=True, exist_ok=True)
    guide = None
    if epg_source is not None:
        epg_source = Path(epg_source).resolve()
        if not epg_source.is_file() or epg_source.suffix.casefold() != ".xml":
            raise RuntimeError("La guía EPG debe ser un XML existente.")
        if not public_base_url or not public_base_url.casefold().startswith("https://"):
            raise RuntimeError("Publicar una guía EPG requiere --public-base-url HTTPS.")
        guide = target.parent / "guia.xml"
        _atomic_copy(epg_source, guide)
        raw = _with_public_epg(raw, urljoin(public_base_url.rstrip("/") + "/", "iptv/guia.xml"))
    temporary = target.with_suffix(".m3u.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, target)
    manifest = {
        "schema": 1,
        "published_utc": datetime.now(timezone.utc).isoformat(),
        "file": "iptv/unificada.m3u",
        "sha256": sha256(target),
        "bytes": target.stat().st_size,
    }
    if public_base_url:
        base = public_base_url.rstrip("/") + "/"
        manifest["playlist_url"] = urljoin(base, "iptv/unificada.m3u")
        manifest["manifest_url"] = urljoin(base, "iptv/unificada.manifest.json")
    if guide is not None:
        manifest["epg"] = {"file": "iptv/guia.xml", "sha256": sha256(guide), "bytes": guide.stat().st_size}
        manifest["epg_url"] = urljoin(public_base_url.rstrip("/") + "/", "iptv/guia.xml")
    manifest_path = target.parent / "unificada.manifest.json"
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(manifest_tmp, manifest_path)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Publica un M3U final de forma atómica.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--publish-root", type=Path, required=True)
    parser.add_argument("--public-base-url")
    parser.add_argument("--epg-input", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.input, args.publish_root, args.public_base_url, args.epg_input), ensure_ascii=False, indent=2))
