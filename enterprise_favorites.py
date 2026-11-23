# -*- coding: utf-8 -*-
"""Enterprise Favorites catalog/migration layer for UNIFICADOR M3U PRO."""
from __future__ import annotations
import os, re, shutil, time, unicodedata
from pathlib import Path
from typing import Iterable

FAVORITES_FILENAME = "favoritos.txt"
MASTER_FILENAME = "favoritos_maestro.txt"
LEGACY_BACKUP_FILENAME = "favoritos.txt.legacy.bak"
LEGACY_GROUP = "00 - FAVORITOS"
CATEGORIES = (
    "00 - Favoritos Infantil",
    "00 - Favoritos Colombia",
    "00 - Favoritos Entretenimiento",
    "00 - Favoritos Deportes",
    "00 - Favoritos Películas",
    "00 - Favoritos Internacional",
    "00 - Favoritos Noticias",
    "00 - Favoritos Win Sport",
)
ALIASES = {
    "favoritos infantil": CATEGORIES[0], "favoritos colombia": CATEGORIES[1],
    "favoritos entretenimiento": CATEGORIES[2], "favoritos deportes": CATEGORIES[3],
    "favoritos peliculas": CATEGORIES[4], "favoritos películas": CATEGORIES[4],
    "favoritos win sport": CATEGORIES[7], "favoritos win sports": CATEGORIES[7],
    "favoritos win": CATEGORIES[7],
    "favoritos internacional": CATEGORIES[5], "favoritos noticias": CATEGORIES[6],
}
STRICT_HEADER_RE = re.compile(r"^\[([^\]]+)\]$")
NON_CATEGORY_BRACKET_RE = re.compile(r"\[(?:HD|4K|USA|FHD|SD|UHD|Opc\.\d+|Not 24/7)\]", re.I)
LEGACY_HEADER_TEXTS = {"00 - favoritos", "favoritos", "00 favoritos", "00_favoritos"}

def is_legacy_header_text(line: str) -> bool:
    key = _key(line)
    return key in {_key(x) for x in LEGACY_HEADER_TEXTS}

def _key(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip().casefold())
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def canonical_category(raw: str) -> str | None:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text or text.casefold() == LEGACY_GROUP.casefold():
        return None
    # Accept numbered forms only when the semantic label is one of the seven.
    m = re.match(r"^\d+\s*[-_. ]\s*(.+)$", text)
    label = m.group(1).strip() if m else text
    return ALIASES.get(_key(label))

def is_strict_category_header(line: str) -> bool:
    if NON_CATEGORY_BRACKET_RE.search((line or "").strip()):
        return False
    m = STRICT_HEADER_RE.match((line or "").strip())
    return bool(m and canonical_category(m.group(1)))

def category_from_line(line: str) -> str | None:
    m = STRICT_HEADER_RE.match((line or "").strip())
    return canonical_category(m.group(1)) if m and is_strict_category_header(line) else None

def classify_legacy_item(line: str) -> str:
    """Deterministic 7-way classifier for flat legacy favorites."""
    k = _key(line)
    rules = [
        (CATEGORIES[3], ("espn", "fox sports", "dsports", "tnt sports", "win sports", "deportes", "sports", "futbol", "football", "nba", "nfl", "mlb", "nhl", "ufc", "motogp")),
        (CATEGORIES[0], ("kids", "kid", "cartoon", "disney junior", "disney jr", "nick", "nickelodeon", "baby tv", "baby first", "discovery kids", "toon", "clan infantil", "semillitas", "plim plim")),
        (CATEGORIES[4], ("hbo", "max", "cinema", "movie", "pelicula", "tnt", "universal", "paramount", "sony movies", "cinemax", "space")),
        (CATEGORIES[6], ("noticias", "news", "cnn", "bbc", "euronews", "ntn24", "cnbc", "fox news", "msnbc", "dw news", "france 24")),
        (CATEGORIES[1], ("colombia", "caracol", "rcn", "teleantioquia", "telemedellin", "televida", "canal uno", "city tv", "canal capital", "cristovision", "ewtn", "tv familia", "senal colombia")),
        (CATEGORIES[2], ("gourmet", "food network", "hgtv", "home & health", "discovery", "history", "animal planet", "tlc", "a&e", "lifetime", "entertainment", "entretenimiento")),
        (CATEGORIES[5], ("internacional", "international", "world", "latino", "telemundo")),
    ]
    for category, needles in rules:
        if any(n in k for n in needles):
            return category
    return CATEGORIES[5]

def atomic_write(path: Path, text: str) -> None:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)

def parse_catalog(text: str) -> tuple[dict[str, list[str]], bool]:
    groups = {c: [] for c in CATEGORIES}
    current: str | None = None
    recognized_header = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cat = category_from_line(line)
        if cat:
            current = cat; recognized_header = True; continue
        # Legacy monolithic markers are headers for migration, never channels.
        if is_legacy_header_text(line):
            current = None
            continue
        # Bracketed source labels like [HD], [4K], [USA] are content, not headers.
        if current is None:
            groups[classify_legacy_item(line)].append(line)
        else:
            groups[current].append(line)
    return groups, recognized_header

def render_catalog(groups: dict[str, Iterable[str]]) -> str:
    out: list[str] = []
    seen = {c: set() for c in CATEGORIES}
    for c in CATEGORIES:
        out.append(f"[{c}]")
        for item in groups.get(c, ()):
            item = item.strip()
            if not item or item in seen[c]:
                continue
            seen[c].add(item); out.append(item)
        out.append("")
    return "\n".join(out).rstrip() + "\n"

def migrate_favorites_directory(base_dir: Path, force: bool = False) -> dict:
    """Migrate legacy favoritos.txt into the mandatory 7-category master catalog."""
    base_dir = Path(base_dir)
    src = base_dir / FAVORITES_FILENAME
    master = base_dir / MASTER_FILENAME
    backup = base_dir / LEGACY_BACKUP_FILENAME
    result = {"source": str(src), "master": str(master), "backup": str(backup), "migrated": False, "legacy": False, "recognized_headers": False, "entries": 0}

    if master.exists() and not force:
        master_text = master.read_text(encoding="utf-8", errors="replace")
        master_check = validate_catalog_file(master)
        if master_check.get("ok"):
            groups, recognized = parse_catalog(master_text)
            result.update({"recognized_headers": recognized, "entries": sum(map(len, groups.values())), "legacy": False})
            return result
        # A corrupt/incomplete master is not authoritative. Rebuild it from the
        # source favoritos.txt so production never silently preserves a bad catalog.
    if not src.exists():
        atomic_write(master, render_catalog({c: [] for c in CATEGORIES}))
        result.update({"migrated": True, "recognized_headers": True, "entries": 0})
        return result

    text = src.read_text(encoding="utf-8-sig", errors="replace")
    groups, recognized = parse_catalog(text)
    result["recognized_headers"] = recognized
    result["legacy"] = (LEGACY_GROUP.casefold() in text.casefold()) or not recognized
    if result["legacy"] and not backup.exists():
        shutil.copy2(src, backup)
    # If legacy group header is present, lines following it are classified; ordinary category headers remain strict.
    content = render_catalog(groups)
    atomic_write(master, content)
    atomic_write(src, content)
    result.update({"migrated": True, "entries": sum(map(len, groups.values()))})
    return result

def validate_catalog_file(path: Path) -> dict:
    path = Path(path)
    issues: list[str] = []
    if not path.exists():
        return {"ok": False, "issues": ["Catálogo maestro ausente"], "categories": []}
    groups, recognized = parse_catalog(path.read_text(encoding="utf-8-sig", errors="replace"))
    # Reparse strictly to ensure exactly the canonical section order.
    seen_headers = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        c = category_from_line(raw.strip())
        if c: seen_headers.append(c)
    if seen_headers != list(CATEGORIES):
        issues.append(f"Las 8 categorías obligatorias no están en el orden exacto: {seen_headers}")
    if not recognized:
        issues.append("Catálogo sin encabezados de Favoritos válidos")
    if not issues:
        legacy_markers = [x for x in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if _key(x) == _key(LEGACY_GROUP)]
        if legacy_markers:
            issues.append("Catálogo contiene grupo legacy 00 - FAVORITOS sin migrar")
    return {"ok": not issues, "issues": issues, "categories": list(seen_headers), "entries": sum(map(len, groups.values()))}
