# -*- coding: utf-8 -*-
"""M3U PRO Enterprise Validator — producción, generación 10.0.0.

Fundación común de la suite:
- modelo de datos M3U;
- parser conservador y tolerante;
- normalización segura de metadatos;
- deduplicación determinista;
- validación estructural y opcional de red;
- escritura atómica y reportes;
- GUI responsiva para validación masiva.

Solo depende de la biblioteca estándar de Python.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence
from functools import lru_cache
from safe_http import PublicHttpError, open_public_http
from network_runtime import NetworkCancelled, NetworkDeadlineExceeded, NetworkWorkerError, run_operation

VERSION = "10.1.0"
RELEASE_DATE = "2026-08-29"
APP_NAME = "M3U PRO VALIDATOR — Enterprise Production Generation"

MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_REPORT_ISSUES = 50_000
MAX_NETWORK_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 8
DEFAULT_WORKERS = 4

SUPPORTED_EXTENSIONS = {".m3u", ".m3u8"}
RESOURCE_SCHEMES = {"http", "https", "rtmp", "rtmps", "rtsp", "udp", "p2p"}
URL_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):/{2,}\S+$")
EXTINF_RE = re.compile(r"^#EXTINF\s*:\s*(?P<duration>-?(?:\d+(?:\.\d*)?|\.\d+))(?P<attrs>.*)$", re.I)
LEGACY_EXTINF_RE = re.compile(r"^(?:#?EXTINF|#?XTINF)\b", re.I)
EXTGRP_RE = re.compile(r"^#EXTGRP\s*:\s*(.*)$", re.I)
ATTR_TOKEN_RE = re.compile(r"(?P<name>[A-Za-z_][\w:.-]*)\s*=\s*(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,]+)")
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "ðŸ", "�")

FAVORITE_CATEGORY_ORDER = (
    "00 - Favoritos Infantil",
    "00 - Favoritos Colombia",
    "00 - Favoritos Entretenimiento",
    "00 - Favoritos Deportes",
    "00 - Favoritos Películas",
    "00 - Favoritos Internacional",
    "00 - Favoritos Noticias",
    "00 - Favoritos Win Sport",
)
CATEGORY_CANONICAL_LABELS = {
    "colombia": "Colombia",
    "noticias": "Noticias",
    "musica": "Música",
    "peliculas": "Películas",
    "infantil": "Infantil",
    "entretenimiento": "Entretenimiento",
    "deportes": "Deportes",
    "documentales": "Documentales",
    "series": "Series",
    "novelas": "Novelas",
    "novelas y series": "Novelas y series",
    "religioso y otros": "Religioso y otros",
    "general": "General",
}
FAVORITE_ALIASES = {
    "favoritos infantil": FAVORITE_CATEGORY_ORDER[0],
    "favoritos colombia": FAVORITE_CATEGORY_ORDER[1],
    "favoritos entretenimiento": FAVORITE_CATEGORY_ORDER[2],
    "favoritos deportes": FAVORITE_CATEGORY_ORDER[3],
    "favoritos win sport": FAVORITE_CATEGORY_ORDER[7],
    "favoritos win sports": FAVORITE_CATEGORY_ORDER[7],
    "favoritos win": FAVORITE_CATEGORY_ORDER[7],
    "favoritos peliculas": FAVORITE_CATEGORY_ORDER[4],
    "favoritos películas": FAVORITE_CATEGORY_ORDER[4],
    "favoritos internacional": FAVORITE_CATEGORY_ORDER[5],
    "favoritos noticias": FAVORITE_CATEGORY_ORDER[6],
}
LEGACY_FAVORITE_GROUPS = {"favoritos", "00 favoritos", "00 - favoritos", "00_favoritos", "00 - favoritos legacy"}


class M3UError(Exception):
    """Base de errores del producto."""


class ParseError(M3UError):
    pass


class ValidationError(M3UError):
    pass


class RepairError(M3UError):
    pass


class FileOperationError(M3UError):
    pass


class ConfigurationError(M3UError):
    pass


@dataclass(frozen=True, slots=True)
class Issue:
    severity: str
    code: str
    line: int
    message: str
    path: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "line": self.line,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }


@dataclass(slots=True)
class M3UEntry:
    name: str
    url: str
    group: str
    attributes: list[tuple[str, str]] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)
    source: str = ""
    line: int = 0
    url_index: int = 1
    raw_extinf: str = ""
    raw_unparsed: list[str] = field(default_factory=list)

    @property
    def favorite(self) -> bool:
        return is_favorite_group(self.group)

    def clone(self) -> "M3UEntry":
        return M3UEntry(
            name=self.name,
            url=self.url,
            group=self.group,
            attributes=list(self.attributes),
            directives=list(self.directives),
            source=self.source,
            line=self.line,
            url_index=self.url_index,
            raw_extinf=self.raw_extinf,
            raw_unparsed=list(self.raw_unparsed),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["favorite"] = self.favorite
        return data


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("m3u_pro_enterprise")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    return logger


def _text_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]+", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_text_field(value: str) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return repair_mojibake_text(text)


def repair_mojibake_text(text: str) -> str:
    value = str(text or "")
    if not any(marker in value for marker in MOJIBAKE_MARKERS):
        return value
    candidates = [value]
    for encoding in ("latin1", "cp1252"):
        try:
            candidate = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidates.append(candidate)
    def score(candidate: str) -> tuple[int, int]:
        bad = sum(candidate.count(marker) for marker in MOJIBAKE_MARKERS)
        return (bad, len(candidate))
    return min(candidates, key=score)


def read_text_robust(path: Path) -> tuple[str, str, list[Issue]]:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FileOperationError(f"No se pudo leer {path}: {exc}") from exc
    if len(raw) == 0:
        return "", "utf-8", []
    if len(raw) > MAX_INPUT_BYTES:
        raise FileOperationError(f"Entrada demasiado grande: {len(raw)} bytes > {MAX_INPUT_BYTES}")
    encodings = []
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings = ["utf-32"]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    elif raw.startswith(b"\xef\xbb\xbf"):
        encodings = ["utf-8-sig"]
    else:
        encodings = ["utf-8", "cp1252", "latin-1"]
    decode_issues: list[Issue] = []
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, decode_issues
        except UnicodeDecodeError:
            continue
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    decode_issues.append(Issue("ERROR", "ENCODING_INVALID", 1, "No se pudo decodificar el archivo con un encoding fiable; se usó reemplazo Unicode.", str(path)))
    return text, "utf-8-replace", decode_issues


def looks_like_resource_line(value: str) -> bool:
    """Chequeo barato de parser; la validación forense completa usa assess_url()."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    match = URL_RE.match(text)
    if match and match.group("scheme").casefold() in RESOURCE_SCHEMES:
        return True
    # Preserve common deterministic-repair candidates for the forensic URL layer.
    return bool(re.match(r"(?i)^hhttps://\S+$", text) or re.match(r"(?i)^https?\s+:\s+//\S+$", text))


def repair_url_scheme(url: str) -> tuple[str, list[str]]:
    """Corrige solo errores de esquema inequívocos; nunca toca host/path/query/fragmento."""
    text = str(url or "").strip()
    changes: list[str] = []
    replacements = [
        (r"^hhttps://", "https://", "hhttps→https"),
        (r"^http\s*:\s*//", "http://", "http ://→http://"),
        (r"^https\s*:\s*//", "https://", "https ://→https://"),
    ]
    for pattern, repl, label in replacements:
        fixed, n = re.subn(pattern, repl, text, count=1, flags=re.I)
        if n and fixed != text:
            text = fixed
            changes.append(label)
            break
    return text, changes


def split_extinf_fields(line: str) -> tuple[str, str | None]:
    text = str(line or "")
    quote = None
    escaped = False
    for index, ch in enumerate(text):
        if ch in ('"', "'") and not escaped:
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
        elif ch == "," and quote is None:
            return text[:index], text[index + 1 :]
        escaped = (ch == "\\" and not escaped)
        if ch != "\\":
            escaped = False
    return text, None


def parse_attribute_tokens(head: str) -> tuple[list[tuple[str, str]], list[dict]]:
    attrs: list[tuple[str, str]] = []
    duplicates: list[dict] = []
    seen: dict[str, list[str]] = defaultdict(list)
    for match in ATTR_TOKEN_RE.finditer(head):
        name = match.group("name")
        raw = match.group("value")
        value = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'") else raw
        value = value.replace('\\"', '"').replace("\\'", "'")
        attrs.append((name, value))
        seen[name.casefold()].append(value)
    for key, values in seen.items():
        if len(values) > 1:
            duplicates.append({"attribute": key, "values": values, "conflict": len(set(values)) > 1})
    return attrs, duplicates


def get_attribute(entry_or_extinf: M3UEntry | str, name: str, default: str = "") -> str:
    if isinstance(entry_or_extinf, M3UEntry):
        for key, value in entry_or_extinf.attributes:
            if key.casefold() == name.casefold():
                return value
        return default
    head, _ = split_extinf_fields(str(entry_or_extinf or ""))
    attrs, _ = parse_attribute_tokens(head)
    for key, value in attrs:
        if key.casefold() == name.casefold():
            return value
    return default


def set_attribute(attributes: Sequence[tuple[str, str]], name: str, value: str, *, remove_duplicates: bool = True) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    replaced = False
    for key, old_value in attributes:
        if key.casefold() == name.casefold():
            if not replaced:
                result.append((key, value))
                replaced = True
            elif not remove_duplicates:
                result.append((key, old_value))
        else:
            result.append((key, old_value))
    if not replaced:
        result.append((name, value))
    return result


def remove_attribute(attributes: Sequence[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    return [(key, value) for key, value in attributes if key.casefold() != name.casefold()]


def canonical_favorite_category(raw: str | None) -> str | None:
    value = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not value:
        return None
    m = re.match(r"^\d+\s*[-_. ]\s*(.+)$", value)
    label = m.group(1) if m else value
    return FAVORITE_ALIASES.get(_text_key(label))


def is_favorite_group(group: str | None) -> bool:
    return canonical_favorite_category(group) is not None


@lru_cache(maxsize=4096)
def normalize_source_category(group: str | None) -> str:
    value = re.sub(r"\s+", " ", str(group or "")).strip()
    if not value:
        return "General"
    favorite = canonical_favorite_category(value)
    if favorite:
        return favorite
    key = _text_key(repair_mojibake_text(value))
    if key in CATEGORY_CANONICAL_LABELS:
        return CATEGORY_CANONICAL_LABELS[key]
    # Conservador: corregir solo errores de una letra para nombres cortos conocidos.
    if len(key) <= 12 and key:
        for known, label in CATEGORY_CANONICAL_LABELS.items():
            if " " in known or abs(len(known) - len(key)) > 1:
                continue
            distance = _damerau_distance(key, known)
            if distance == 1:
                return label
    return repair_mojibake_text(value)


def _damerau_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def canonical_favorite_name(name: str) -> str:
    value = normalize_text_field(name)
    while True:
        new_value = re.sub(r"\s*\[Opc\.[1-5]\]\s*$", "", value, flags=re.I).strip()
        if new_value == value:
            return value
        value = new_value


def _clean_display_text(text: str, fallback: str = "Canal sin nombre") -> str:
    value = normalize_text_field(text)
    marker = re.search(r"#EXT(?:GRP|VLCOPT|INF)\s*:", value, re.I)
    if marker:
        value = value[: marker.start()]
    return value.strip(" \t,;|#") or fallback


def _sync_extgrp(directives: Sequence[str], group: str) -> list[str]:
    result = []
    seen = False
    for directive in directives:
        if EXTGRP_RE.match(directive):
            if not seen:
                result.append(f"#EXTGRP:{group}")
                seen = True
        else:
            result.append(directive)
    if group and not seen:
        result.append(f"#EXTGRP:{group}")
    return result


def build_extinf(entry: M3UEntry) -> str:
    duration_match = EXTINF_RE.match(entry.raw_extinf or "")
    duration = duration_match.group("duration") if duration_match else "0"
    attrs = []
    for key, value in entry.attributes:
        safe_key = str(key).strip()
        safe_value = value
        if safe_key.casefold() == "group-title":
            safe_value = entry.group
        elif safe_key.casefold() in {"tvg-name", "m3u-pro-original-group"}:
            safe_value = normalize_text_field(value)
        attrs.append(f'{safe_key}="{safe_value.replace(chr(34), chr(92)+chr(34))}"')
    has_group = any(key.casefold() == "group-title" for key, _ in entry.attributes)
    if not has_group:
        attrs.insert(0, f'group-title="{entry.group}"')
    attr_text = (" " + " ".join(attrs)) if attrs else ""
    return f"#EXTINF:{duration}{attr_text},{_clean_display_text(entry.name)}"


def parse_m3u(path: Path, *, strict_size: bool = True, repair_obvious_urls: bool = False) -> tuple[list[M3UEntry], list[Issue], dict]:
    path = Path(path)
    if strict_size and path.stat().st_size > MAX_INPUT_BYTES:
        raise FileOperationError(f"Entrada demasiado grande: {path.stat().st_size} bytes > {MAX_INPUT_BYTES}")
    text, encoding, decode_issues = read_text_robust(path)
    issues: list[Issue] = list(decode_issues)
    entries: list[M3UEntry] = []
    extinf_blocks = 0
    orphan_urls = 0
    missing_urls = 0
    noncanonical_extinf = 0
    duplicate_attributes = 0
    malformed_content = 0
    header_present = False
    pending_directives: list[str] = []
    current_extinf: dict | None = None

    def add_issue(severity: str, code: str, line: int, message: str, **details: object) -> None:
        if len(issues) < MAX_REPORT_ISSUES:
            issues.append(Issue(severity, code, line, message, str(path), dict(details)))

    def emit(block: dict, raw_url: str) -> None:
        nonlocal duplicate_attributes
        url = raw_url.strip()
        attrs, duplicates = parse_attribute_tokens(block["head"])
        for duplicate in duplicates:
            duplicate_attributes += 1
            add_issue("ERROR", "DUPLICATE_ATTRIBUTE", block["line"], f"Atributo EXTINF duplicado: {duplicate['attribute']}", **duplicate)
        group_title = get_attribute_from_pairs(attrs, "group-title") or ""
        extgrp_values = [match.group(1).strip() for directive in block["directives"] if (match := EXTGRP_RE.match(directive))]
        raw_extgrp = extgrp_values[0] if extgrp_values else ""
        if group_title and raw_extgrp and semantic_text(group_title) != semantic_text(raw_extgrp):
            add_issue("ERROR", "EXTGRP_MISMATCH", block["line"], "#EXTGRP y group-title están desincronizados.", group_title=group_title, extgrp=raw_extgrp)
        group = group_title or raw_extgrp
        group = normalize_source_category(group)
        head, tail = split_extinf_fields(block["extinf"])
        visible_name = tail or ""
        if looks_like_resource_line(visible_name.strip()):
            visible_name = ""
        tvg_name = get_attribute_from_pairs(attrs, "tvg-name")
        fallback = path.stem or "Canal sin nombre"
        visible_name = block.get("inline_name", visible_name)
        if tail is None or not visible_name.strip():
            if not tvg_name.strip():
                add_issue("WARNING", "MISSING_NAME", block["line"], "EXTINF no aporta nombre visible ni tvg-name; se aplicará un fallback determinista.")
        name = _clean_display_text(visible_name or tvg_name or fallback)
        entry = M3UEntry(
            name=name,
            url=url,
            group=group,
            attributes=attrs,
            directives=list(block["directives"]),
            source=str(path),
            line=block["line"],
            url_index=block["url_index"],
            raw_extinf=block["extinf"],
            raw_unparsed=list(block["raw_unparsed"]),
        )
        if entry.raw_unparsed:
            add_issue("ERROR", "UNEXPECTED_CONTENT", block["line"], "Contenido no URL dentro del bloque EXTINF; se aísla en reparación segura.", content=entry.raw_unparsed[:5])
        entries.append(entry)
        block["url_index"] += 1

    lines = text.splitlines()
    for line_no, raw in enumerate(lines, 1):
        if len(raw.encode("utf-8", "replace")) > MAX_LINE_BYTES:
            add_issue("ERROR", "LINE_TOO_LONG", line_no, f"Línea excede MAX_LINE_BYTES={MAX_LINE_BYTES}")
            continue
        t = raw.strip()
        if line_no == 1:
            t = t.lstrip("\ufeff")
        if not t:
            continue
        if t.upper().startswith("#EXTM3U"):
            header_present = True
            continue

        if LEGACY_EXTINF_RE.match(t):
            canonical = t
            if t.upper().startswith("XTINF"):
                canonical = "#" + t
            elif t.upper().startswith("#XTINF"):
                canonical = "#" + t[1:]
            match = EXTINF_RE.match(canonical)
            if not match:
                m = re.match(r"^#EXTINF\s*,(.*)$", canonical, re.I)
                if m:
                    canonical = "#EXTINF:0," + m.group(1)
                    noncanonical_extinf += 1
                    add_issue("ERROR", "NONCANONICAL_EXTINF", line_no, "EXTINF sin duración; reparación segura posible a duración 0.", original=t, canonical=canonical)
                else:
                    add_issue("ERROR", "INVALID_EXTINF", line_no, "Cabecera EXTINF malformada y no recuperable de forma segura.", content=t[:220])
                    current_extinf = None
                    continue
            elif canonical != t:
                noncanonical_extinf += 1
                add_issue("ERROR", "NONCANONICAL_EXTINF", line_no, "Cabecera EXTINF no canónica; puede repararse de forma determinista.", original=t, canonical=canonical)
            if current_extinf is not None and current_extinf["url_index"] == 1:
                missing_urls += 1
                add_issue("ERROR", "MISSING_URL", current_extinf["line"], "#EXTINF sin URL asociada antes del siguiente #EXTINF")
            head, tail = split_extinf_fields(canonical)
            current_extinf = {
                "extinf": canonical,
                "head": head,
                "line": line_no,
                "directives": list(pending_directives),
                "raw_unparsed": [],
                "url_index": 1,
            }
            pending_directives.clear()
            inline_tail = (tail or "").strip()
            inline_urls: list[str] = []
            visible_inline_name = inline_tail
            if inline_tail:
                # Legacy-safe form: `#EXTINF:-1,Nombre,URL` (and, where
                # unambiguous, `Nombre,URL1,URL2`). A comma is part of the
                # display name unless the suffix is unmistakably a resource URL.
                pieces = [part.strip() for part in inline_tail.split(",")]
                url_start = None
                for idx in range(1, len(pieces)):
                    if looks_like_resource_line(pieces[idx]):
                        url_start = idx
                        break
                if url_start is not None:
                    visible_inline_name = ",".join(pieces[:url_start]).strip()
                    inline_urls = [part for part in pieces[url_start:] if looks_like_resource_line(part)]
                    if len(inline_urls) == len(pieces[url_start:]):
                        for inline_url in inline_urls:
                            emit(current_extinf, inline_url)
                elif looks_like_resource_line(inline_tail):
                    visible_inline_name = ""
                    emit(current_extinf, inline_tail)
            if inline_urls:
                # The URL(s) have already been emitted; retain the display name
                # for consumers that need the parsed entry name.
                for emitted in entries[-len(inline_urls):]:
                    emitted.name = _clean_display_text(visible_inline_name or get_attribute_from_pairs(emitted.attributes, "tvg-name") or path.stem or "Canal sin nombre")
            elif not looks_like_resource_line(inline_tail) and inline_tail:
                # The tail is a normal visible name; URL may follow on a later line.
                current_extinf["inline_name"] = inline_tail
            continue

        candidate_url = t
        url_changes: list[str] = []
        if not looks_like_resource_line(candidate_url) and repair_obvious_urls:
            candidate_url, url_changes = repair_url_scheme(candidate_url)
        if looks_like_resource_line(candidate_url):
            if current_extinf is None:
                orphan_urls += 1
                add_issue("ERROR", "ORPHAN_URL", line_no, "URL sin #EXTINF asociado.", url=redact_url(candidate_url), url_sha256=sha256_text(candidate_url))
            else:
                if url_changes:
                    add_issue("WARNING", "REPAIRED_URL_SCHEME", line_no, "Esquema URL corregido de forma inequívoca.", changes=url_changes)
                current_extinf["url_index"] = 1 if current_extinf.get("url_index", 1) == 0 else current_extinf["url_index"]
                emit(current_extinf, candidate_url)
                current_extinf["raw_unparsed"] = []
                current_extinf["directives"] = []
            continue

        if t.startswith("#"):
            if current_extinf is None:
                pending_directives.append(t)
            else:
                current_extinf["directives"].append(t)
            continue

        if current_extinf is not None:
            current_extinf["raw_unparsed"].append(t)
            malformed_content += 1
        else:
            malformed_content += 1
            add_issue("ERROR", "UNEXPECTED_TOP_LEVEL_TEXT", line_no, "Texto no reconocido fuera de una entrada.", content=t[:220])

    if current_extinf is not None and current_extinf["url_index"] == 1:
        missing_urls += 1
        add_issue("ERROR", "MISSING_URL", current_extinf["line"], "#EXTINF sin URL asociada al final del archivo")
    if pending_directives:
        add_issue("WARNING", "ORPHAN_DIRECTIVE", 0, "Quedaron directivas sin una entrada posterior.", count=len(pending_directives))
    if not header_present and text.strip():
        add_issue("ERROR", "MISSING_HEADER", 1, "Falta #EXTM3U como cabecera canónica.")
    if not text.strip():
        add_issue("WARNING", "EMPTY_FILE", 0, "Archivo vacío.")
    mojibake_lines = []
    for idx, value in enumerate(lines, 1):
        if not any(marker in value for marker in MOJIBAKE_MARKERS):
            continue
        # Solo advertimos cuando la heurística tiene una corrección textual
        # efectiva o cuando queda el marcador de reemplazo Unicode, evitando
        # falsos positivos legítimos como `Â` en portugués (p. ej. CÂMARA).
        repaired = repair_mojibake_text(value)
        if repaired != value or "�" in value:
            mojibake_lines.append(idx)
    for idx in mojibake_lines[:1000]:
        add_issue("WARNING", "MOJIBAKE", idx, "Texto con patrones de mojibake; solo campos textuales son candidatos a reparación segura.")

    metadata = {
        "path": str(path),
        "encoding": encoding,
        "bytes": path.stat().st_size,
        "header_present": header_present,
        "entries": len(entries),
        "extinf_blocks": extinf_blocks,
        "orphan_urls": orphan_urls,
        "missing_urls": missing_urls,
        "noncanonical_extinf": noncanonical_extinf,
        "duplicate_attributes": duplicate_attributes,
        "malformed_content": malformed_content,
        "issue_count": len(issues),
    }
    # Recompute blocks from line-level signals: parsing loop increments via matches.
    metadata["extinf_blocks"] = sum(1 for raw in lines if LEGACY_EXTINF_RE.match(raw.strip()))
    return entries, issues, metadata


def get_attribute_from_pairs(attributes: Sequence[tuple[str, str]], name: str, default: str = "") -> str:
    for key, value in attributes:
        if key.casefold() == name.casefold():
            return value
    return default


@lru_cache(maxsize=8192)
def semantic_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def semantic_signature(entry: M3UEntry) -> tuple:
    attrs = tuple((key.casefold(), value) for key, value in entry.attributes)
    directives = tuple(entry.directives)
    return (
        entry.url,
        semantic_text(entry.name),
        semantic_text(normalize_source_category(entry.group)),
        attrs,
        directives,
    )


def deduplicate_entries(entries: Iterable[M3UEntry]) -> tuple[list[M3UEntry], list[M3UEntry], list[dict]]:
    kept: list[M3UEntry] = []
    removed: list[M3UEntry] = []
    collisions: list[dict] = []
    seen_exact: set[tuple] = set()
    first_by_url: dict[str, M3UEntry] = {}
    for entry in entries:
        sig = semantic_signature(entry)
        if sig in seen_exact:
            removed.append(entry)
            continue
        seen_exact.add(sig)
        prior = first_by_url.get(entry.url)
        if prior is not None and semantic_signature(prior) != sig:
            collisions.append({
                "url_sha256": sha256_text(entry.url),
                "first": {"name": prior.name, "group": prior.group, "source": prior.source},
                "conflict": {"name": entry.name, "group": entry.group, "source": entry.source},
            })
        first_by_url.setdefault(entry.url, entry)
        kept.append(entry)
    return kept, removed, collisions


def _sort_key(entry: M3UEntry, order: dict[str, int] | None = None) -> tuple:
    if order is None:
        order = {group.casefold(): idx for idx, group in enumerate(FAVORITE_CATEGORY_ORDER)}
    favorite_rank = order.get(entry.group.casefold(), len(order))
    return (favorite_rank, semantic_text(entry.group), semantic_text(entry.name), entry.url)


def normalize_entries(entries: Iterable[M3UEntry]) -> list[M3UEntry]:
    result: list[M3UEntry] = []
    group_winners: dict[str, str] = {}
    for entry in entries:
        clone = entry.clone()
        raw_group = clone.group
        group = normalize_source_category(raw_group)
        key = semantic_text(group)
        winner = group_winners.setdefault(key, group)
        clone.group = winner
        clone.name = _clean_display_text(clone.name)
        clone.attributes = set_attribute(clone.attributes, "group-title", clone.group)
        # tvg-name es metadata independiente: solo reparar mojibake, nunca sustituir por el título.
        if get_attribute_from_pairs(clone.attributes, "tvg-name"):
            clone.attributes = set_attribute(clone.attributes, "tvg-name", normalize_text_field(get_attribute_from_pairs(clone.attributes, "tvg-name")))
        clone.directives = _sync_extgrp(clone.directives, clone.group)
        result.append(clone)
    return result


def build_m3u(entries: Iterable[M3UEntry], *, bom: bool = True) -> str:
    output = ["#EXTM3U"]
    for entry in entries:
        if not entry.url or not looks_like_resource_line(entry.url):
            raise ValidationError(f"Entrada sin URL válida para serialización: {entry.name!r}")
        output.append(build_extinf(entry))
        output.extend(entry.directives)
        output.append(entry.url)
    text = "\n".join(output) + "\n"
    return "\ufeff" + text if bom else text


def atomic_write_text(path: Path, text: str, *, backup: bool = False) -> Path | None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Path | None = None
    if backup and path.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
        counter = 1
        while backup_path.exists():
            backup_path = path.with_name(f"{path.stem}.backup_{stamp}_{counter}{path.suffix}")
            counter += 1
        try:
            backup_path.write_bytes(path.read_bytes())
        except OSError as exc:
            raise FileOperationError(f"No se pudo crear backup de {path}: {exc}") from exc
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            payload = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return backup_path
    except Exception as exc:
        raise FileOperationError(f"No se pudo publicar atómicamente {path}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



@dataclass(frozen=True, slots=True)
class URLAssessment:
    """Compatibility adapter over the canonical url_safety authority."""
    raw: str
    normalized: str
    status: str
    code: str
    message: str
    scheme: str = ""
    hostname: str = ""
    port: int | None = None
    credentials: bool = False
    repaired_url: str = ""
    safe_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def assess_url(url: object, *, allow_repair: bool = True) -> URLAssessment:
    from url_safety import inspect_url
    inspection = inspect_url(url)
    status_map = {
        "VALID": "VALID",
        "REPAIRED": "REPAIRABLE" if allow_repair else "INVALID",
        "UNSUPPORTED_SCHEME": "UNSUPPORTED",
        "INVALID_PORT": "INVALID",
        "INVALID_HOST": "INVALID",
        "EMPTY": "INVALID",
        "NON_STRING": "INVALID",
        "INVALID_SYNTAX": "INVALID",
    }
    status = status_map.get(inspection.status.value, "INVALID")
    if inspection.status.value == "REPAIRED" and not allow_repair:
        code = "URL_REPAIR_DISABLED"
        repaired_url = ""
        message = "La URL requiere una reparación determinista, pero las reparaciones están deshabilitadas."
    else:
        code = inspection.code or ("URL_VALID" if status == "VALID" else inspection.status.value)
        repaired_url = inspection.normalized_url if status == "REPAIRABLE" else ""
        message = inspection.reason
    return URLAssessment(
        raw=inspection.raw,
        normalized=inspection.normalized_url,
        status=status,
        code=code,
        message=message,
        scheme=inspection.scheme,
        hostname=inspection.host,
        port=inspection.port,
        credentials=inspection.has_credentials,
        repaired_url=repaired_url,
        safe_url=redact_url(inspection.normalized_url or inspection.raw),
    )


def assess_entry_url(entry: M3UEntry, *, allow_repair: bool = True) -> URLAssessment:
    return assess_url(entry.url, allow_repair=allow_repair)


def redact_url(url: str) -> str:
    from url_safety import redact_url as _redact_url
    return _redact_url(url)


def validate_url_http(entry: M3UEntry, timeout: int = DEFAULT_TIMEOUT, *, allow_network: bool = False,
                      cancel_event: threading.Event | None = None) -> tuple[bool, str, float | None]:
    if not allow_network:
        return True, "Validación estructural; red no solicitada.", None
    started = time.monotonic()
    seconds = max(1, int(timeout))
    try:
        ok, reason = run_operation("validator_http", {
            "url": entry.url, "timeout": seconds,
            "user_agent": get_attribute(entry, "http-user-agent", "M3U-PRO-Validator/10.0"),
        }, deadline=started + seconds, cancel_event=cancel_event)
    except NetworkDeadlineExceeded:
        ok, reason = False, f"Timeout: venció el plazo total de {seconds} s."
    except NetworkWorkerError as exc:
        ok, reason = False, str(exc)
    return ok, reason, (time.monotonic() - started) * 1000


def _probe_validator_http(url: str, user_agent: str, timeout: int) -> tuple[bool, str]:
    try:
        response, _final_url = open_public_http(url, headers={"User-Agent": user_agent}, timeout=timeout)
        with response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_NETWORK_BODY_BYTES + 1)
            if len(body) > MAX_NETWORK_BODY_BYTES:
                return False, f"Respuesta excede {MAX_NETWORK_BODY_BYTES} bytes."
            if not body and response.status >= 400:
                return False, f"HTTP {response.status}"
            if "mpegurl" in content_type.lower() or url.lower().split("?", 1)[0].endswith((".m3u", ".m3u8")):
                text = body.decode("utf-8", errors="replace")
                if not text.lstrip("\ufeff\r\n ").startswith("#EXTM3U"):
                    return False, "Respuesta HTTP no parece una playlist M3U válida."
            return True, f"HTTP {response.status}"
    except (PublicHttpError, urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _category_findings(entries: Sequence[M3UEntry]) -> list[dict]:
    findings = []
    for entry in entries:
        raw = get_attribute(entry, "group-title", entry.group)
        canonical = normalize_source_category(raw)
        state = "CANONICAL_KNOWN" if canonical == raw else ("KNOWN_NONCANONICAL" if _text_key(raw) in CATEGORY_CANONICAL_LABELS else "UNKNOWN")
        findings.append({"line": entry.line, "raw": raw, "canonical": canonical, "state": state})
    return findings


def validate_file(path: Path, *, network: bool = False, workers: int = DEFAULT_WORKERS,
                 timeout: int = DEFAULT_TIMEOUT, allow_repairs: bool = True,
                 cancel_event: threading.Event | None = None) -> dict:
    path = Path(path).resolve()
    entries, parse_issues, metadata = parse_m3u(path)
    issue_details = [issue.to_dict() for issue in parse_issues]
    groups = Counter(entry.group for entry in entries)
    favorite_counts = Counter((entry.group, canonical_favorite_name(entry.name))
                              for entry in entries if entry.favorite)

    url_findings: list[dict] = []
    url_counts = Counter()

    for entry in entries:
        raw_group = get_attribute(entry, "group-title", "")
        if not raw_group.strip():
            issue_details.append(Issue("WARNING", "MISSING_GROUP", entry.line,
                                       "Entrada sin group-title; se normaliza de forma conservadora.",
                                       str(path), {"name": entry.name}).to_dict())
        else:
            canonical_group = normalize_source_category(raw_group)
            if raw_group.strip() != canonical_group:
                issue_details.append(Issue("WARNING", "NONCANONICAL_GROUP", entry.line,
                                           "group-title no está en forma canónica; puede normalizarse sin perder el registro.",
                                           str(path), {"original": raw_group, "canonical": canonical_group}).to_dict())
        if not entry.name.strip():
            issue_details.append(Issue("WARNING", "MISSING_NAME", entry.line,
                                       "Entrada sin nombre visible; se utilizará un fallback determinista al reparar.",
                                       str(path)).to_dict())

        assessment = assess_entry_url(entry, allow_repair=allow_repairs)
        url_counts[assessment.status] += 1
        finding = assessment.to_dict()
        finding["line"] = entry.line
        finding["name"] = entry.name
        finding["url_sha256"] = sha256_text(entry.url)
        url_findings.append(finding)

        if assessment.status == "INVALID":
            issue_details.append(Issue(
                "ERROR", assessment.code, entry.line,
                assessment.message, str(path),
                {"url_sha256": sha256_text(entry.url), "safe_url": assessment.safe_url,
                 "scheme": assessment.scheme, "host": assessment.hostname}
            ).to_dict())
        elif assessment.status == "UNSUPPORTED":
            issue_details.append(Issue(
                "WARNING", assessment.code, entry.line,
                assessment.message, str(path),
                {"url_sha256": sha256_text(entry.url), "safe_url": assessment.safe_url,
                 "scheme": assessment.scheme}
            ).to_dict())
        elif assessment.status == "REPAIRABLE":
            issue_details.append(Issue(
                "WARNING", assessment.code, entry.line,
                assessment.message, str(path),
                {"url_sha256": sha256_text(entry.url), "safe_url": assessment.safe_url,
                 "repaired_url": redact_url(assessment.repaired_url)}
            ).to_dict())

    seen_signatures: set[tuple] = set()
    duplicate_exact = 0
    url_first: dict[str, M3UEntry] = {}
    for entry in entries:
        sig = semantic_signature(entry)
        if sig in seen_signatures:
            duplicate_exact += 1
            issue_details.append(Issue("WARNING", "DUPLICATE_SEMANTIC", entry.line,
                                       "Entrada semánticamente duplicada.", str(path),
                                       {"url_sha256": sha256_text(entry.url)}).to_dict())
        seen_signatures.add(sig)
        url_first.setdefault(entry.url, entry)
        group_attr = get_attribute(entry, "group-title", entry.group)
        ext_group = next((m.group(1).strip() for m in map(EXTGRP_RE.match, entry.directives) if m), None)
        if ext_group and normalize_source_category(group_attr) != normalize_source_category(ext_group):
            issue_details.append(Issue("ERROR", "EXTGRP_MISMATCH", entry.line,
                                       "#EXTGRP y group-title están desincronizados.", str(path),
                                       {"group_title": group_attr, "extgrp": ext_group}).to_dict())

    for (group, logical), count in favorite_counts.items():
        if count > 4:
            issue_details.append(Issue("ERROR", "FAVORITE_SOURCE_LIMIT", 0,
                                       f"{group} / {logical}: {count} fuentes; máximo permitido 4.",
                                       str(path), {"count": count}).to_dict())

    if network and entries:
        todo = list(entries)
        worker_count = max(1, min(int(workers), 16))
        initial = todo[: max(1, worker_count * 2)]
        future_map = {}
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            for entry in initial:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkCancelled("Validación cancelada.")
                future_map[pool.submit(validate_url_http, entry, timeout, allow_network=True,
                                       cancel_event=cancel_event)] = entry
            iterator = iter(todo[len(initial):])
            while future_map:
                if cancel_event is not None and cancel_event.is_set():
                    for future in future_map:
                        future.cancel()
                    raise NetworkCancelled("Validación cancelada.")
                completed, _ = wait(tuple(future_map), timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    entry = future_map.pop(future)
                    try:
                        ok, reason, latency = future.result()
                    except NetworkCancelled:
                        raise
                    except Exception as exc:
                        ok, reason, latency = False, f"{type(exc).__name__}: {exc}", None
                    if not ok:
                        issue_details.append(Issue(
                            "ERROR", "URL_NETWORK", entry.line, reason, str(path),
                            {"url_sha256": sha256_text(entry.url), "latency_ms": latency,
                             "safe_url": redact_url(entry.url)}
                        ).to_dict())
                    try:
                        next_entry = next(iterator)
                    except StopIteration:
                        next_entry = None
                    if next_entry is not None and not (cancel_event is not None and cancel_event.is_set()):
                        future_map[pool.submit(validate_url_http, next_entry, timeout, allow_network=True,
                                               cancel_event=cancel_event)] = next_entry

    issue_details = issue_details[:MAX_REPORT_ISSUES]
    severities = Counter(row["severity"] for row in issue_details)
    ok = not any(level in severities for level in ("CRITICAL", "ERROR"))
    if metadata["bytes"] == 0:
        ok = False
    status = "FAIL" if not ok else ("PASS_WITH_WARNINGS" if any(level in severities for level in ("WARNING", "INFO")) else "PASS")
    invalid_or_repairable = url_counts["INVALID"] + url_counts["REPAIRABLE"]

    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "entries": len(entries),
        "extinf_blocks": metadata["extinf_blocks"],
        "orphan_urls": metadata["orphan_urls"],
        "missing_urls": metadata["missing_urls"],
        "duplicate_semantic": duplicate_exact,
        "duplicate_urls": len(entries) - len({entry.url for entry in entries}),
        "unique_groups": len(groups),
        "groups": dict(groups),
        "encoding": metadata["encoding"],
        "bytes": metadata["bytes"],
        "favorite_entries": sum(favorite_counts.values()),
        "issue_count": len(issue_details),
        "severity_counts": dict(severities),
        "url_counts": dict(url_counts),
        "url_invalid_or_repairable": invalid_or_repairable,
        "url_findings": url_findings,
        "issues": issue_details,
        "category_findings": _category_findings(entries),
        "ok": ok,
        "status": status,
    }




def validate(path: Path) -> dict:
    """Legacy compatibility API backed by the 10.1 validator."""
    result = validate_file(Path(path), network=False)
    legacy_messages = [item.get("message", "") for item in result.get("issues", [])]
    source_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    if re.search(r"(?im)(?:group-title=\"|#EXTGRP:)\s*00\s*-\s*FAVORITOS(?:\"|\s*$)", source_text):
        legacy_messages.append("Favoritos legacy sin migrar")
        result["ok"] = False
        result["status"] = "FAIL"
    result["url_issues"] = int(result.get("url_invalid_or_repairable", 0))
    raw_by_canonical = {}
    try:
        entries, _, _ = parse_m3u(Path(path))
        for entry in entries:
            raw = get_attribute(entry, "group-title", "")
            canonical = normalize_source_category(raw)
            if raw.strip() and canonical in raw_by_canonical and raw.strip().casefold() != raw_by_canonical[canonical].strip().casefold():
                legacy_messages.append(f"Categoría duplicada/no canónica: {raw.strip()} vs {raw_by_canonical[canonical].strip()}")
                result["ok"] = False
                result["status"] = "FAIL"
            elif raw.strip():
                raw_by_canonical.setdefault(canonical, raw)
    except Exception:
        pass
    legacy_messages.extend([item.get("code", "") for item in result.get("url_findings", []) if item.get("status") in {"INVALID", "UNSUPPORTED", "REPAIRABLE"}])
    result["issues_legacy"] = legacy_messages
    result["issues"] = legacy_messages
    return result

def discover_inputs(paths: Iterable[str | Path], *, recursive: bool = True) -> list[Path]:
    found: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            if path.is_symlink():
                continue
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                found.add(path.resolve())
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            for candidate in iterator:
                if candidate.is_file() and not candidate.is_symlink() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
                    found.add(candidate.resolve())
    return sorted(found, key=lambda value: str(value).casefold())




def assert_output_outside_inputs(inputs: Iterable[str | Path], output: Path) -> None:
    target = Path(output).expanduser().resolve()
    for raw in inputs:
        source = Path(raw).expanduser().resolve()
        if source == target:
            raise FileOperationError(f"La salida coincide con una entrada: {target}")
        if source.is_dir() and target == source:
            raise FileOperationError(f"La salida no puede ser la carpeta de entrada: {target}")
        if source.is_dir() and source in target.parents:
            raise FileOperationError(f"La salida no puede residir dentro de una carpeta fuente: {target}")

def validate_many(paths: Iterable[str | Path], *, network: bool = False, workers: int = DEFAULT_WORKERS, timeout: int = DEFAULT_TIMEOUT, allow_repairs: bool = True, progress: Callable[[int, int, str], None] | None = None, cancel_event: threading.Event | None = None) -> dict:
    logger = setup_logging()
    files = discover_inputs(paths)
    logger.info("stage=discovery operation=validate_many files=%d result=READY", len(files))
    results: list[dict] = []
    total = len(files)
    for index, path in enumerate(files, 1):
        if cancel_event and cancel_event.is_set():
            break
        try:
            result = validate_file(path, network=network, workers=workers, timeout=timeout,
                                   allow_repairs=allow_repairs, cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                break
        except NetworkCancelled:
            break
        except Exception as exc:
            result = {
                "path": str(path),
                "entries": 0,
                "status": "FAILED",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "issues": [{"severity": "CRITICAL", "code": "FILE_OPERATION", "line": 0, "message": f"{type(exc).__name__}: {exc}", "path": str(path), "details": {}}],
            }
        results.append(result)
        logger.info("stage=validation operation=validate_file file=%s result=%s entries=%s issues=%s", path, result.get("status"), result.get("entries", 0), len(result.get("issues", [])))
        if progress:
            progress(index, total, "Validación")
    aggregate = Counter()
    total_entries = 0
    for result in results:
        aggregate[result.get("status", "FAILED")] += 1
        total_entries += int(result.get("entries", 0))
    successful = sum(1 for result in results if result.get("ok") is True)
    logger.info("stage=reporting operation=validate_many files=%d ok=%d result=COMPLETE", len(results), successful)
    return {
        "version": VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "total_files": total,
        "processed_files": len(results),
        "successful_files": successful,
        "warning_or_failed_files": total - successful,
        "entries": total_entries,
        "results": results,
        "cancelled": bool(cancel_event and cancel_event.is_set()),
    }


def build_repair_manifest(path: Path, validation: dict, *, destination: Path | None = None) -> dict:
    """Construye un manifiesto pequeño para automatización sin duplicar URLs completas."""
    return {
        "version": VERSION,
        "source": str(Path(path).resolve()),
        "destination": str(destination.resolve()) if destination else "",
        "source_sha256": validation.get("sha256", ""),
        "status": validation.get("status", "FAIL"),
        "entries": validation.get("entries", 0),
        "url_counts": validation.get("url_counts", {}),
        "repairable": [
            {
                "line": row.get("line", 0),
                "name": row.get("name", ""),
                "code": row.get("code", ""),
                "url_sha256": row.get("url_sha256", ""),
            }
            for row in validation.get("url_findings", [])
            if row.get("status") == "REPAIRABLE"
        ],
        "invalid": [
            {
                "line": row.get("line", 0),
                "name": row.get("name", ""),
                "code": row.get("code", ""),
                "url_sha256": row.get("url_sha256", ""),
            }
            for row in validation.get("url_findings", [])
            if row.get("status") == "INVALID"
        ],
    }


def write_json_report(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text, backup=False)


def _choose_paths_gui() -> list[Path]:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    selection = filedialog.askopenfilenames(title="Selecciona listas M3U/M3U8", filetypes=[("M3U/M3U8", "*.m3u *.m3u8"), ("Todos", "*.*")])
    folder = filedialog.askdirectory(title="Opcional: agrega una carpeta completa (Cancelar = no agregar)")
    root.destroy()
    paths = [Path(item) for item in selection]
    if folder:
        paths.append(Path(folder))
    return paths


class ValidatorApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("940x680")
        self.root.minsize(820, 560)
        self.cancel_event = threading.Event()
        self.close_requested = False
        self.running = False
        self.paths: list[Path] = []
        self.status = tk.StringVar(value="Listo. Agrega archivos o carpetas para validar.")
        self.progress = tk.DoubleVar(value=0)
        self.worker: threading.Thread | None = None
        self._build()

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="M3U PRO VALIDATOR", font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Label(header, text=f"Enterprise Production v{VERSION}").pack(side="right")
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(12, 8))
        self.add_button = ttk.Button(toolbar, text="Agregar archivos", command=self.add_files)
        self.add_button.pack(side="left")
        self.add_folder_button = ttk.Button(toolbar, text="Agregar carpeta", command=self.add_folder)
        self.add_folder_button.pack(side="left", padx=6)
        self.clear_button = ttk.Button(toolbar, text="Limpiar", command=self.clear)
        self.clear_button.pack(side="left")
        ttk.Label(toolbar, text="  ").pack(side="left")
        self.network_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Validación de red HTTP/HTTPS", variable=self.network_var).pack(side="left")
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Label(toolbar, text=" Workers:").pack(side="left", padx=(12, 2))
        ttk.Spinbox(toolbar, from_=1, to=16, textvariable=self.workers_var, width=4).pack(side="left")
        self.validate_button = ttk.Button(toolbar, text="VALIDAR", command=self.start)
        self.validate_button.pack(side="right")
        self.cancel_button = ttk.Button(toolbar, text="Cancelar", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)
        columns = ("path", "size", "status")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("path", text="Archivo")
        self.tree.heading("size", text="Tamaño")
        self.tree.heading("status", text="Estado")
        self.tree.column("path", width=650)
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("status", width=140)
        self.tree.pack(fill="both", expand=True)
        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill="x", pady=(10, 6))
        ttk.Label(outer, textvariable=self.status).pack(fill="x")
        self.log = tk.Text(outer, height=8, wrap="word")
        self.log.pack(fill="x", pady=(8, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _refresh_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for path in self.paths:
            try:
                size = path.stat().st_size
                size_text = f"{size / 1024:.1f} KB"
            except OSError:
                size_text = "?"
            self.tree.insert("", "end", values=(str(path), size_text, "Pendiente"))

    def add_files(self) -> None:
        from tkinter import filedialog
        values = filedialog.askopenfilenames(title="Selecciona M3U/M3U8", filetypes=[("M3U/M3U8", "*.m3u *.m3u8"), ("Todos", "*.*")])
        for value in values:
            p = Path(value).resolve()
            if p.suffix.lower() in SUPPORTED_EXTENSIONS and p not in self.paths:
                self.paths.append(p)
        self.paths.sort(key=lambda p: str(p).casefold())
        self._refresh_tree()
        self.status.set(f"{len(self.paths)} archivo(s) en cola.")

    def add_folder(self) -> None:
        from tkinter import filedialog
        value = filedialog.askdirectory(title="Selecciona carpeta")
        if value:
            for path in discover_inputs([value]):
                if path not in self.paths:
                    self.paths.append(path)
            self.paths.sort(key=lambda p: str(p).casefold())
            self._refresh_tree()
            self.status.set(f"{len(self.paths)} archivo(s) en cola.")

    def clear(self) -> None:
        self.paths.clear()
        self._refresh_tree()
        self.status.set("Cola vacía.")

    def start(self) -> None:
        if self.running or not self.paths:
            return
        self.running = True
        self.cancel_event.clear()
        self.validate_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.set(0)
        selected_paths = list(self.paths)
        self.worker = threading.Thread(target=self._work, args=(selected_paths, bool(self.network_var.get()),
                                                               int(self.workers_var.get())), daemon=True)
        self.worker.start()

    def _work(self, paths: list[Path], network: bool, workers: int) -> None:
        try:
            report = validate_many(paths, network=network, workers=workers, progress=self._progress,
                                   cancel_event=self.cancel_event)
            output = paths[0].with_name("M3U_PRO_VALIDATOR_REPORT.json") if paths else Path.cwd() / "M3U_PRO_VALIDATOR_REPORT.json"
            self.root.after(0, lambda: self._done(report, output))
        except Exception as exc:
            self.root.after(0, lambda error=exc: self._error(error))

    def _progress(self, done: int, total: int, stage: str) -> None:
        value = 100 if not total else done * 100 / total
        self.root.after(0, lambda: (self.progress.set(value), self.status.set(f"{stage}: {done}/{total}")))

    def _done(self, report: dict, output: Path) -> None:
        self.running = False
        self.validate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        if self.close_requested:
            self.root.destroy()
            return
        if self.cancel_event.is_set() or report["cancelled"]:
            self.status.set("Validación cancelada. Se conservó el informe anterior.")
            return
        # The Tk thread serializes an accepted cancellation and final publication.
        try:
            write_json_report(output, report)
        except Exception as exc:
            self._error(exc)
            return
        self.progress.set(100)
        self.status.set(f"Finalizado: {report['successful_files']}/{report['processed_files']} archivos PASS.")
        self.log.insert("end", json.dumps(report, ensure_ascii=False, indent=2)[:12000] + "\n")
        self.log.see("end")

    def _error(self, exc: Exception) -> None:
        self.running = False
        if self.close_requested:
            self.root.destroy()
            return
        self.validate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status.set(f"ERROR: {type(exc).__name__}: {exc}")
        self.log.insert("end", f"ERROR: {type(exc).__name__}: {exc}\n")

    def cancel(self) -> None:
        if self.running:
            self.cancel_event.set()
            self.status.set("Cancelación solicitada; finalizando la operación actual de forma segura…")

    def close(self) -> None:
        if self.running:
            self.close_requested = True
            self.cancel_event.set()
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{VERSION}")
    parser.add_argument("paths", nargs="*", help="Archivos o carpetas M3U/M3U8")
    parser.add_argument("--report", help="Ruta del informe JSON")
    parser.add_argument("--network", action="store_true", help="Validar HTTP/HTTPS con protección de red privada")
    parser.add_argument("--profile", choices=("development", "campaign", "production"), help="Perfil obligatorio para cualquier validación de red")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--no-repair-suggestions", action="store_true", help="Solo clasificar; no proponer reparaciones deterministas.")
    parser.add_argument("--gui", action="store_true", help="Abrir GUI")
    args = parser.parse_args(argv)
    if args.gui or not args.paths:
        app = ValidatorApp()
        app.run()
        return 0
    if args.network:
        from runtime_profiles import apply_profile
        profile = apply_profile(args.profile)
        if args.workers < 1 or args.workers > profile.max_workers:
            raise RuntimeError(f"Workers inválidos para {profile.name}: 1..{profile.max_workers}.")
    files = discover_inputs(args.paths)
    if not files:
        print("ERROR: no se encontraron archivos M3U/M3U8.")
        return 2
    from network_runtime import cancel_on_sigint
    cancel = threading.Event()
    with (cancel_on_sigint(cancel) if args.network else nullcontext()):
        report = validate_many(files, network=args.network, workers=args.workers, timeout=args.timeout,
                               allow_repairs=not args.no_repair_suggestions, cancel_event=cancel)
    if cancel.is_set() or report["cancelled"]:
        print("Validación cancelada. Se conservó el informe anterior.")
        return 130
    report_path = Path(args.report) if args.report else Path.cwd() / "M3U_PRO_VALIDATOR_REPORT.json"
    try:
        write_json_report(report_path, report)
    except KeyboardInterrupt:
        print("Publicación interrumpida.")
        return 130
    print(f"Archivos: {report['processed_files']}/{report['total_files']}")
    print(f"Entradas: {report['entries']}")
    print(f"PASS: {report['successful_files']}")
    print(f"Reporte: {report_path.resolve()}")
    return 0 if all(item.get("ok", False) for item in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
