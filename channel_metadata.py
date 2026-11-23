"""Conservative logo and XMLTV metadata enrichment for IPTV channels.

The module never guesses that a community image is official. A replacement logo
requires a curated registry record with an HTTPS source of provenance.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import gzip
import json
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit
import xml.etree.ElementTree as ElementTree

from safe_http import open_public_http
from network_runtime import NetworkCancelled, _check_boundary, run_operation


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise NetworkCancelled("Enriquecimiento de metadatos cancelado.")


def _key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip()


def _attr(extinf: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', extinf or "", flags=re.I)
    return match.group(1).strip() if match else ""


def _set_attr(extinf: str, name: str, value: str) -> str:
    if '"' in value or "\r" in value or "\n" in value:
        raise ValueError(f"Valor no seguro para {name}")
    pattern = rf'\b{re.escape(name)}="[^"]*"'
    rendered = f'{name}="{value}"'
    if re.search(pattern, extinf, flags=re.I):
        return re.sub(pattern, rendered, extinf, count=1, flags=re.I)
    comma = extinf.find(",")
    return (extinf[:comma] + " " + rendered + extinf[comma:]) if comma >= 0 else (extinf + " " + rendered)


def _https(value: str) -> bool:
    parsed = urlsplit(value or "")
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def load_registry(path: Path, *, cancel_event=None) -> dict:
    _check_cancel(cancel_event)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") not in {1, 2} or not isinstance(data.get("channels"), list):
        raise RuntimeError("Registro de metadatos inválido; se requiere schema=1/2 y channels[].")
    epg_url = str(data.get("epg_url") or "").strip()
    if epg_url and not _https(epg_url):
        raise RuntimeError("epg_url debe usar HTTPS.")
    index: dict[str, dict] = {}
    seen_tvg_ids: set[str] = set()
    seen_epg_ids: set[str] = set()
    for position, record in enumerate(data["channels"], 1):
        _check_cancel(cancel_event)
        if not isinstance(record, dict):
            raise RuntimeError(f"channels[{position}] debe ser un objeto.")
        aliases = record.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise RuntimeError(f"channels[{position}].aliases debe ser una lista de texto.")
        logo = str(record.get("official_logo_url") or "").strip()
        provenance = str(record.get("official_logo_source") or "").strip()
        if bool(logo) != bool(provenance) or (logo and (not _https(logo) or not _https(provenance))):
            raise RuntimeError(f"channels[{position}] debe declarar logo y procedencia HTTPS juntos.")
        keys = {_key(record.get("tvg_id", "")), _key(record.get("name", ""))}
        keys.update(_key(item) for item in aliases)
        keys.discard("")
        if not keys:
            raise RuntimeError(f"channels[{position}] requiere name, tvg_id o aliases.")
        tvg_id = str(record.get("tvg_id") or "").strip()
        epg_id = str(record.get("epg_id") or "").strip()
        if tvg_id and _key(tvg_id) in seen_tvg_ids:
            raise RuntimeError(f"tvg_id duplicado en channels[{position}].")
        if epg_id and _key(epg_id) in seen_epg_ids:
            raise RuntimeError(f"epg_id duplicado en channels[{position}].")
        if tvg_id:
            seen_tvg_ids.add(_key(tvg_id))
        if epg_id:
            seen_epg_ids.add(_key(epg_id))
        for key in keys:
            if key in index:
                raise RuntimeError(f"Identidad ambigua en registro: {key!r} aparece en más de un canal.")
            index[key] = record
    data["_index"] = index
    return data


def _record_for(entry: dict, registry: dict) -> dict | None:
    extinf = str(entry.get("extinf") or "")
    ids = {_key(_attr(extinf, "tvg-id")), _key(entry.get("name", "")), _key(_attr(extinf, "tvg-name"))}
    ids.discard("")
    index = registry.get("_index") or {}
    for identity in ids:
        record = index.get(identity)
        if record:
            return record
    # Compatibilidad para integradores que todavía llaman enrich_entries con un
    # diccionario no cargado por load_registry. El flujo de producción siempre
    # usa el índice validado construido en load_registry.
    if not index:
        for record in registry.get("channels", []):
            if not isinstance(record, dict):
                continue
            keys = {_key(record.get("tvg_id", "")), _key(record.get("name", ""))}
            keys.update(_key(item) for item in record.get("aliases", []) if isinstance(item, str))
            if ids & (keys - {""}):
                return record
    return None


def enrich_entries(entries: list[dict], registry: dict, *, scope: str = "favorites", cancel_event=None) -> tuple[list[dict], dict]:
    _check_cancel(cancel_event)
    if scope not in {"favorites", "all"}:
        raise ValueError("scope debe ser favorites o all")
    output = []
    summary = {
        "scope": scope, "considered": 0, "matched": 0,
        "logos_preserved": 0, "logos_invalid_syntax": 0, "logos_added": 0,
        "logos_still_missing": 0, "epg_ids_added": 0, "unmatched": 0,
        "epg_url": registry.get("epg_url", ""),
    }
    for entry in entries:
        _check_cancel(cancel_event)
        clone = deepcopy(entry)
        group = str(clone.get("group") or "")
        if scope == "favorites" and not group.casefold().startswith("00 - favoritos"):
            output.append(clone)
            continue
        summary["considered"] += 1
        record = _record_for(clone, registry)
        if not record:
            summary["unmatched"] += 1
            output.append(clone)
            continue
        summary["matched"] += 1
        extinf = str(clone.get("extinf") or "")
        existing_logo = _attr(extinf, "tvg-logo")
        if existing_logo:
            summary["logos_preserved"] += 1
            if not _https(existing_logo):
                # This is intentionally a local syntactic assessment only.  A
                # remote HEAD request for every logo would be slow and could
                # leak the user's whole channel catalogue to third parties.
                summary["logos_invalid_syntax"] += 1
        else:
            logo = str(record.get("official_logo_url") or "").strip()
            provenance = str(record.get("official_logo_source") or "").strip()
            if logo and _https(logo) and _https(provenance):
                extinf = _set_attr(extinf, "tvg-logo", logo)
                summary["logos_added"] += 1
            else:
                summary["logos_still_missing"] += 1
        epg_id = str(record.get("epg_id") or record.get("tvg_id") or "").strip()
        if epg_id and not _attr(extinf, "tvg-id"):
            extinf = _set_attr(extinf, "tvg-id", epg_id)
            summary["epg_ids_added"] += 1
        clone["extinf"] = extinf
        output.append(clone)
    return output, summary


def epg_header(url: str) -> str:
    if not _https(url):
        return "#EXTM3U"
    if '"' in url:
        return "#EXTM3U"
    return f'#EXTM3U url-tvg="{url}"'


def _read_limited(stream, maximum: int) -> bytes:
    payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise RuntimeError("La respuesta XMLTV excede el límite comprimido configurado.")
    return payload


def _write_xml_payload(payload: bytes, destination: Path, *, max_uncompressed_bytes: int) -> tuple[int, bool]:
    compressed = payload.startswith(b"\x1f\x8b")
    source = gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") if compressed else BytesIO(payload)
    temporary = destination.with_name(destination.name + ".tmp")
    written = 0
    try:
        with source, temporary.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > max_uncompressed_bytes:
                    raise RuntimeError("XMLTV descomprimido excede el límite configurado.")
                output.write(chunk)
        _reject_unsafe_xml(temporary)
        with temporary.open("rb") as stream:
            sample = stream.read(4096)
        if b"<tv" not in sample.lower():
            raise RuntimeError("La respuesta no parece un documento XMLTV.")
        temporary.replace(destination)
        return written, compressed
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _reject_unsafe_xml(path: Path, *, cancel_event=None) -> None:
    """Reject DTD/entity declarations before ElementTree sees remote XML."""
    tail = b""
    with Path(path).open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            _check_cancel(cancel_event)
            candidate = (tail + chunk).upper()
            if b"<!DOCTYPE" in candidate or b"<!ENTITY" in candidate:
                raise RuntimeError("XMLTV con DTD o entidades no está permitido.")
            tail = candidate[-16:]


def download_xmltv(url: str, destination: Path, *, max_age_seconds: int = 12 * 60 * 60,
                   max_bytes: int = 32 * 1024 * 1024, max_uncompressed_bytes: int = 128 * 1024 * 1024,
                   cancel_event=None, deadline=None) -> dict:
    """Fetch an approved XMLTV document with a bounded, reusable local cache.

    The URL is evaluated by the same SSRF policy used by the stream pipeline
    immediately before connecting.  A cache hit intentionally avoids network
    activity; XMLTV is usually shared by every favourite channel.
    """
    destination = Path(destination)
    if cancel_event is not None and deadline is None:
        # Keep the per-request timeout at 20s and give the complete interactive
        # operation a larger, finite budget for redirects and substantial guides.
        deadline = time.monotonic() + 120
    if deadline is not None:
        _check_boundary(deadline, cancel_event)
    if destination.exists() and max_age_seconds > 0:
        age = max(0.0, time.time() - destination.stat().st_mtime)
        if age < max_age_seconds:
            return {"cached": True, "bytes": destination.stat().st_size, "path": str(destination)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if deadline is None:
        return _fetch_xmltv(url, destination, max_bytes, max_uncompressed_bytes)
    # The parent alone publishes the guide. Killing a stalled worker can leave
    # either its output or its .tmp file here, both owned by this invocation.
    with tempfile.TemporaryDirectory(prefix=".xmltv-", dir=destination.parent) as directory:
        staged = Path(directory).resolve() / "guide.xml"
        result = run_operation("xmltv", {
            "url": url, "destination": str(staged), "max_bytes": max_bytes,
            "max_uncompressed_bytes": max_uncompressed_bytes,
        }, deadline=deadline, cancel_event=cancel_event)
        _check_boundary(deadline, cancel_event)
        staged.replace(destination)
    return {**result, "path": str(destination)}


def _fetch_xmltv(url: str, destination: Path, max_bytes: int, max_uncompressed_bytes: int) -> dict:
    response, _final_url = open_public_http(url, headers={"User-Agent": "M3U-Enterprise-Metadata/1.0", "Accept": "application/xml,text/xml,*/*"}, timeout=20)
    with response:
        payload = _read_limited(response, max_bytes)
    bytes_written, compressed = _write_xml_payload(payload, destination, max_uncompressed_bytes=max_uncompressed_bytes)
    return {"cached": False, "bytes": bytes_written, "compressed_source": compressed, "path": str(destination)}


def audit_xmltv_coverage(path: Path, entries: list[dict], *, scope: str = "favorites", max_elements: int = 1_000_000,
                         cancel_event=None) -> dict:
    """Return aggregate XMLTV coverage without retaining programme titles."""
    if scope not in {"favorites", "all"}:
        raise ValueError("scope debe ser favorites o all")
    requested: dict[str, set[str]] = {}
    _check_cancel(cancel_event)
    for entry in entries:
        _check_cancel(cancel_event)
        group = str(entry.get("group") or "General")
        if scope == "favorites" and not group.casefold().startswith("00 - favoritos"):
            continue
        identity = _attr(str(entry.get("extinf") or ""), "tvg-id")
        if identity:
            requested.setdefault(group, set()).add(identity)
    all_ids = set().union(*requested.values()) if requested else set()
    _reject_unsafe_xml(Path(path), cancel_event=cancel_event)
    found, programmed, seen_elements = set(), set(), 0
    for _event, element in ElementTree.iterparse(Path(path), events=("end",)):
        _check_cancel(cancel_event)
        seen_elements += 1
        if seen_elements > max_elements:
            raise RuntimeError("XMLTV excede el límite de elementos configurado.")
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "channel":
            identity = element.attrib.get("id", "")
            if identity in all_ids:
                found.add(identity)
        elif tag == "programme":
            identity = element.attrib.get("channel", "")
            if identity in all_ids:
                programmed.add(identity)
        element.clear()
    by_group = {}
    for group, identities in requested.items():
        by_group[group] = {
            "expected_ids": len(identities), "channel_ids_found": len(identities & found),
            "ids_with_programme": len(identities & programmed),
            "missing_channel_ids": len(identities - found), "without_programme": len((identities & found) - programmed),
        }
    return {"scope": scope, "expected_ids": len(all_ids), "channel_ids_found": len(all_ids & found), "ids_with_programme": len(all_ids & programmed), "parsed_elements": seen_elements, "by_group": by_group}
