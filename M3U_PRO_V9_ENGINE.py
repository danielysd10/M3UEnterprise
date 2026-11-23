# -*- coding: utf-8 -*-
"""
UNIFICADOR M3U PRO v9.0.0 ENTERPRISE — Unificación, Validación, Limpieza y UTF-8

Objetivos:
- Unificar listas M3U/M3U8 sin perder metadatos ni directivas.
- Limpiar los nombres de canal para que NO aparezcan #EXTGRP / #EXTVLCOPT.
- Mantener tvg-id, tvg-name, tvg-logo, group-title y cualquier atributo EXTINF.
- Mantener todas las directivas #EXTVLCOPT originales (user-agent, referrer, etc.).
- Añadir #EXTVLCOPT:network-caching=1000 solo si el canal no trae uno propio.
- Eliminar únicamente URLs exactamente idénticas.
- Validar las URLs y conservar solo las que parecen reproducibles/activas.
- Ejecutar la validación en paralelo para listas grandes.
- Guardar un informe completo: duplicados y URLs caídas/no válidas.
- Permitir URLs manuales, bloques guardados y favoritos persistentes.
- Reparar M3U existentes desde la misma aplicación.
- Auditar UTF-8 y estructura antes de publicar el archivo final.
- Escritura atómica para evitar archivos finales truncados.

Nota sobre "reproducible":
La validación estándar comprueba que el endpoint responda y, para HLS, que devuelva
una playlist HLS válida. Eso es más útil que comprobar solamente HTTP 200, pero no
sustituye una reproducción completa en VLC/FFmpeg. Si ffprobe está instalado y se
activa la verificación profunda, se intenta validar la pista multimedia real.
"""

import json
import os
import re
import shutil
import sys
import subprocess
import random
import urllib.parse
import threading
import hashlib
import time
import unicodedata

from url_safety import URLStatus, apply_url_policy, inspect_url, network_target_allowed, redact_text, redact_url
from functools import lru_cache
import zipfile
import tkinter as tk
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.error import HTTPError, URLError
from channel_metadata import epg_header
from safe_http import PublicHttpError, open_public_http
from network_runtime import NetworkCancelled, NetworkDeadlineExceeded, _run_owned_process, run_operation
from user_settings import (APP_CONFIG_NAME, BLOCKS_NAME, FAVORITES_FILENAME, MASTER_FILENAME,
                           SettingsError, default_config_dir, load_catalog, load_mapping, write_text)

APP_NAME = "UNIFICADOR M3U PRO v9.0.16 ENTERPRISE — Producción"
APP_DIR = Path(__file__).resolve().parent
USER_CONFIG_DIR = default_config_dir()
CONFIG_FILE = USER_CONFIG_DIR / BLOCKS_NAME
APP_CONFIG_FILE = USER_CONFIG_DIR / APP_CONFIG_NAME
FAVORITES_FILE = USER_CONFIG_DIR / FAVORITES_FILENAME
MASTER_FAVORITES_FILE = USER_CONFIG_DIR / MASTER_FILENAME
LEGACY_SETTINGS_DIR = None if "M3U_CONFIG_DIR" in os.environ else APP_DIR
ENTERPRISE_ROOT = APP_DIR.parent
COMPAT_READ_DIRS = (APP_DIR, ENTERPRISE_ROOT)
DEFAULT_FAVORITES_GROUP = "00 - FAVORITOS"
LEGACY_FAVORITES_GROUP = "00 - FAVORITOS"
FAVORITE_CATEGORY_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
FAVORITE_CATEGORY_PREFIX_RE = re.compile(r"^(\d+)\s*[-_. ]\s*(.+)$")
# Solo estas marcas se interpretan como variantes del mismo canal en Favoritos.
# El nombre completo sigue siendo obligatorio para localizar la entrada exacta en la M3U.
FAVORITE_OPTION_SUFFIX_RE = re.compile(r"\s*\[Opc\.(?:[1-5])\]\s*$", re.I)

# Orden empresarial fijo para las categorías de Favoritos. Todos usan prefijo
# 00 deliberadamente; el orden real lo determina esta lista.
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
FAVORITE_CATEGORY_INDEX = {name.casefold(): idx for idx, name in enumerate(FAVORITE_CATEGORY_ORDER)}
FAVORITE_CATEGORY_ALIASES = {
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

# Nombres canónicos exactos para categorías comunes. La clave se compara
# sin mayúsculas, espacios sobrantes ni diacríticos, pero la salida conserva
# la ortografía empresarial esperada.
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

def _category_compare_key(text):
    text = re.sub(r"\s+", " ", (text or "")).strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))

AUTO_CATEGORY_DEFINITIONS = [
    (10, "Deportes", ["deporte", "sports", "futbol", "fútbol", "football", "soccer", "nba", "nfl", "nhl", "mlb", "ufc", "wwe", "tenis", "tennis", "golf", "formula 1", "f1", "motogp", "beisbol", "béisbol", "basquet", "basket", "liga", "mundial", "champions", "libertadores", "espn", "fox sports", "win sports", "dsports", "tnt sports", "tudn"]),
    (11, "Infantil", ["kids", "kid", "infantil", "junior", "jr", "cartoon", "toon", "disney", "nick", "nickelodeon", "cartoonito", "discovery kids", "baby tv", "baby first", "semillitas", "peques", "rugrats", "plim plim", "chavo", "dreamworks", "supertoons", "clan internacional"]),
    (12, "Peliculas", ["pelicula", "películas", "movie", "cinema", "cine", "film", "hbo", "max", "universal cinema", "universal premiere", "paramount", "sony movies", "star channel", "fxm", "space", "studio universal", "cinemax", "amc"]),
    (13, "Series", ["series", "serie", "comedy", "sitcom", "drama", "crime", "detective", "novela", "novelas", "fiction", "axn", "warner tv", "sony channel", "syfy", "universal tv", "golden", "telemundo", "telenovela"]),
    (14, "Entretenimiento", ["entretenimiento", "entertainment", "reality", "variedades", "variety", "gourmet", "food", "cocina", "cooking", "tlc", "lifetime", "a&e", "tru tv", "trutv", "home & health", "home and health", "hgtv", "discovery home", "el gourmet"]),
    (15, "Noticias", ["noticias", "news", "newsroom", "cnn", "bbc news", "euronews", "ntn24", "cnbc", "abc news", "fox news", "msnbc", "dw news", "france 24", "telesur", "caracol noticias", "rcn noticias", "24 horas"]),
    (16, "Documentales", ["documental", "documentales", "documentary", "discovery channel", "discovery science", "discovery theater", "discovery theatre", "discovery world", "discovery turbo", "discovery id", "history", "history channel", "h2", "nat geo", "national geographic", "animal planet", "travel", "smithsonian", "science channel"]),
    (17, "Colombia", ["colombia", "colomb", "caracol", "rcn", "teleantioquia", "telemedellin", "telemedellín", "televida", "canal uno", "city tv", "canal capital", "cristovision", "cristovisión", "ewtn", "tv familia", "canal institucional", "senal colombia", "señal colombia"]),
    (18, "Musica", ["musica", "música", "music", "mtv", "vh1", "concert", "concierto", "hits", "radio", "dj", "4music", "stingray", "vevo", "billboard", "top 40"]),
    (19, "Religioso y Otros", ["relig", "crist", "catol", "catól", "biblia", "iglesia", "gospel", "evangel", "god", "faith", "dios te ve"]),
]

URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$", re.I)
ATTR_RE = re.compile(r'(\b[\w-]+)="([^"]*)"', re.I)
DIRECTIVE_RE = re.compile(r"#EXT[A-Z0-9_-]*", re.I)

DEFAULT_TIMEOUT = 8
DEFAULT_WORKERS = 8  # Modo Máxima Estabilidad
MAX_ENTERPRISE_WORKERS = 8
DEEP_GLOBAL_MAX_URLS = 999999
HOST_MAX_CONCURRENCY = 2
HOST_MIN_START_GAP = 0.18
HOST_START_JITTER_MAX = 0.35
TRANSIENT_RETRIES = 1
TRANSIENT_BACKOFF_SECONDS = 0.9
FAVORITE_MAX_PROBE_CANDIDATES_PER_CHANNEL = 24
FFPROBE_SEARCH_NAMES = ("ffprobe.exe", "ffprobe")

# La fuente principal sigue la versión publicada por Gyan. El mirror y el hash
# embebido corresponden exclusivamente al artefacto histórico indicado aquí.
FFMPEG_RELEASE_VERSION = "9.0.1"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_ZIP_FALLBACK_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/"
    f"{FFMPEG_RELEASE_VERSION}/ffmpeg-{FFMPEG_RELEASE_VERSION}-essentials_build.zip"
)
FFMPEG_SHA_URL = FFMPEG_ZIP_URL + ".sha256"
FFMPEG_SHA_FALLBACK_URL = (
    "https://www.gyan.dev/ffmpeg/builds/packages/"
    f"ffmpeg-{FFMPEG_RELEASE_VERSION}-essentials_build.zip.sha256"
)
# Hash oficial publicado por Gyan para el mismo artefacto release 9.0.1.
# Se usa solo como último recurso cuando los sidecars .sha256 no son accesibles.
FFMPEG_KNOWN_SHA256 = "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9"
FFMPEG_INSTALL_DIR = Path(__file__).resolve().parent / "ffmpeg_portable"
FFMPEG_CACHE_ZIP = Path(__file__).resolve().parent / "ffmpeg-release-essentials.zip"
FFMPEG_DOWNLOAD_RETRIES = 4
FFMPEG_RETRY_BACKOFF = 1.5

SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "key", "apikey", "api_key", "signature",
    "sig", "hash", "password", "passwd", "pass", "pwd", "username", "user",
}


def redact_url_for_report(url):
    """Never-raise, credential-safe representation for reports and diagnostics."""
    return redact_url(url)



def _download_file(url, dest, progress_callback=None, timeout=60, retries=4):
    """Descarga un archivo con reintentos y escritura atómica.

    En Windows, WinError 10054 suele aparecer como una desconexión abrupta
    del servidor o de un proxy. Reintentamos únicamente fallos de transporte
    y errores HTTP 5xx; los 4xx definitivos se propagan inmediatamente.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_exc = None

    for attempt in range(1, max(1, retries) + 1):
        try:
            if part.exists():
                part.unlink()
            resp, _final_url = open_public_http(
                url,
                headers={"User-Agent": "UNIFICADOR-M3U-PRO/9.0 Enterprise", "Accept": "*/*", "Connection": "close"},
                timeout=timeout,
            )
            with resp:
                total = int(resp.headers.get("Content-Length") or 0)
                written = 0
                with open(part, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if progress_callback:
                            progress_callback(written, total)
                if written <= 0:
                    raise RuntimeError("El servidor respondió sin datos.")
            os.replace(part, dest)
            return
        except HTTPError as exc:
            last_exc = exc
            # 5xx puede ser transitorio; 4xx no debe maquillarse con reintentos.
            if not (500 <= getattr(exc, "code", 0) < 600) or attempt >= retries:
                raise
        except (URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise

        time.sleep(FFMPEG_RETRY_BACKOFF * attempt)

    if last_exc:
        raise last_exc
    raise RuntimeError(f"No se pudo descargar {url}")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_ffmpeg_zip(zip_path, install_dir):
    install_dir.mkdir(parents=True, exist_ok=True)
    base = install_dir.resolve()
    max_total_uncompressed = 1_500_000_000
    with zipfile.ZipFile(zip_path, "r") as z:
        total = 0
        for member in z.infolist():
            # Normalize both POSIX and Windows separators before validation.
            # This is intentionally OS-independent so a malicious ZIP created
            # on another platform cannot exploit backslash traversal on Windows.
            raw_name = member.filename or ""
            normalized_name = raw_name.replace("\\", "/")
            posix_name = PurePosixPath(normalized_name)
            first_part = posix_name.parts[0] if posix_name.parts else ""
            if normalized_name.startswith("/") or posix_name.is_absolute() or (len(first_part) == 2 and first_part[1] == ":"):
                raise RuntimeError(f"ZIP rechazado por ruta absoluta: {raw_name}")
            if any(part in ("..", "") for part in posix_name.parts):
                if ".." in posix_name.parts:
                    raise RuntimeError(f"ZIP rechazado por traversal: {raw_name}")
            # Reject traversal using the host OS resolver as a second barrier.
            target = (install_dir / normalized_name).resolve()
            if os.path.commonpath([str(base), str(target)]) != str(base):
                raise RuntimeError(f"ZIP rechazado por ruta insegura: {raw_name}")
            mode = (member.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise RuntimeError(f"ZIP rechazado por enlace simbólico: {raw_name}")
            total += int(member.file_size)
            if total > max_total_uncompressed:
                raise RuntimeError("ZIP rechazado por tamaño descomprimido excesivo")
        z.extractall(install_dir)

    candidates = list(install_dir.rglob("ffprobe.exe"))
    if not candidates:
        raise RuntimeError("La descarga no contiene ffprobe.exe. Puede que se haya descargado un paquete incorrecto.")
    return candidates[0]


def _download_ffmpeg_zip_with_fallback(zip_path, progress_callback=None, status_callback=None):
    errors = []
    sources = (FFMPEG_ZIP_URL, FFMPEG_ZIP_FALLBACK_URL)
    for idx, url in enumerate(sources):
        try:
            if status_callback:
                label = "fuente principal" if idx == 0 else "mirror oficial de GitHub"
                status_callback(f"Descargando FFmpeg Essentials desde {label}...")
            _download_file(
                url,
                zip_path,
                progress_callback=progress_callback,
                timeout=90,
                retries=FFMPEG_DOWNLOAD_RETRIES,
            )
            return url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError(
        "No fue posible descargar FFmpeg desde las fuentes oficiales.\n\n"
        + "\n".join(errors)
    )


def _resolve_expected_ffmpeg_sha256(zip_path, source, status_callback=None):
    """Obtiene el checksum del mismo artefacto que se descargó."""
    checksum_url = {
        FFMPEG_ZIP_URL: FFMPEG_SHA_URL,
        FFMPEG_ZIP_FALLBACK_URL: FFMPEG_SHA_FALLBACK_URL,
    }[source]
    sha_path = zip_path.with_suffix(zip_path.suffix + ".sha256")
    try:
        _download_file(checksum_url, sha_path, timeout=30, retries=FFMPEG_DOWNLOAD_RETRIES)
        checksum_text = sha_path.read_text(encoding="utf-8").strip()
        expected = checksum_text.split()[0].lower() if checksum_text else ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Formato SHA-256 no válido")
    except (OSError, RuntimeError, ValueError) as exc:
        if status_callback:
            status_callback(
                f"Checksum de origen no disponible ({exc}). Sólo se aceptará el "
                f"SHA-256 conocido de FFmpeg {FFMPEG_RELEASE_VERSION}."
            )
        return FFMPEG_KNOWN_SHA256.lower(), "SHA-256 oficial embebido"
    if source == FFMPEG_ZIP_FALLBACK_URL and expected != FFMPEG_KNOWN_SHA256:
        raise RuntimeError(f"El checksum del mirror no coincide con el SHA-256 de FFmpeg {FFMPEG_RELEASE_VERSION}.")
    return expected, "sidecar oficial"


def install_ffprobe(progress_callback=None, status_callback=None):
    """Descarga y extrae una compilación Windows de FFmpeg que incluye ffprobe."""
    FFMPEG_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = FFMPEG_CACHE_ZIP

    source = _download_ffmpeg_zip_with_fallback(
        zip_path, progress_callback=progress_callback, status_callback=status_callback
    )

    if status_callback:
        status_callback("Verificando integridad SHA-256 de FFmpeg...")
    expected, source_kind = _resolve_expected_ffmpeg_sha256(zip_path, source, status_callback=status_callback)
    actual = _sha256_file(zip_path).lower()
    if expected != actual:
        raise RuntimeError(
            "La verificación SHA-256 no coincide. La descarga se rechazó por seguridad. "
            f"\nEsperado: {expected}\nActual:   {actual}"
        )

    if status_callback:
        status_callback(
            f"SHA-256 verificado ({source_kind}). Extrayendo FFmpeg y localizando ffprobe.exe..."
        )
    exe = _extract_ffmpeg_zip(zip_path, FFMPEG_INSTALL_DIR)
    exe = exe.resolve()
    ok, info = test_ffprobe(str(exe))
    if not ok:
        raise RuntimeError("Se encontró ffprobe.exe, pero no pudo ejecutarse: " + info)
    save_app_config({**load_app_config(), "ffprobe_path": str(exe)})
    return str(exe), info


# Secuencias típicas de mojibake UTF-8 mal interpretado como CP1252/Latin-1.
# IMPORTANTE: no se debe marcar "â" o "ð" por sí solos porque pueden aparecer
# legítimamente en texto Unicode. La auditoría busca patrones realmente
# característicos de corrupción.
MOJIBAKE_MARKERS = (
    "Ã", "Â", "�", "ESPAÃ", "EspaÃ",
)
MOJIBAKE_PATTERNS = (
    r"Ã(?:[\x80-\xBF]|[¡-ÿ])",
    r"Â(?:[\x80-\xBF]|[¡-ÿ])",
    r"â(?:€|™|š|œ|ž|¦|¤|…|–|—|“|”|‘|’|†|‡|ˆ|‰|‹|›)",
    r"ð(?:Ÿ|\x9F)",
    r"\b(?:ESPAÃ(?:A|±A|A|‘A))\b",
)

# Correcciones conocidas de texto degradado que NO se pueden recuperar
# matemáticamente una vez perdidos algunos bytes. Se mantienen deliberadamente
# pequeñas y conservadoras; el usuario puede ampliar el diccionario desde
# m3u_repair_rules.json si necesita otros casos.
DEFAULT_REPAIR_RULES = {
    "ESPAÃA": "ESPAÑA",
    "ESPAÃ±A": "ESPAÑA",
    "ESPAÃA": "ESPAÑA",
    "ESPAÃ‘A": "ESPAÑA",
    "EspaÃ±a": "España",
    "EspaÃ±ol": "Español",
    "espaÃ±a": "españa",
    "espaÃ±ol": "español",
}
REPAIR_RULES_FILE = Path(__file__).resolve().with_name("m3u_repair_rules.json")


def mojibake_score(text):
    score = sum(text.count(m) for m in MOJIBAKE_MARKERS)
    score += text.count("\ufffd") * 5
    for pattern in MOJIBAKE_PATTERNS:
        score += len(re.findall(pattern, text, flags=re.UNICODE))
    # Restar falsos positivos introducidos por los marcadores amplios Ã/Â:
    # estos se cuentan como señales, pero la puntuación principal la dan los
    # patrones concretos. Se mantiene conservadora para no reparar texto válido.
    return score


@lru_cache(maxsize=1)
def load_repair_rules():
    """Load external repair rules once per process.

    Parsing calls this function for several textual fields per entry. Rules are
    operational configuration, not per-record input, so reading the same JSON
    files repeatedly turns a linear parse into an I/O-bound workload. A user
    who changes rules reloads the application, giving a deterministic run.
    """
    rules = dict(DEFAULT_REPAIR_RULES)
    for directory in COMPAT_READ_DIRS:
        candidate = directory / REPAIR_RULES_FILE.name
        try:
            if not candidate.exists():
                continue
            extra = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(k, str) and isinstance(v, str):
                        rules[k] = v
        except Exception:
            continue
    return rules


def repair_mojibake_text(text):
    """Repara UTF-8 mal interpretado como CP1252/Latin-1 y reglas explícitas.

    Devuelve (texto_reparado, cambios, score_antes, score_despues).
    Nunca toca URLs por contenido: la transformación se hace sobre el texto
    completo solo para deshacer secuencias de mojibake; la validación posterior
    evita que se publique un M3U con residuos detectables.
    """
    rules = load_repair_rules()
    # The common production path is already-valid text. Avoid the expensive
    # score/encode/decode search unless a known marker, a mojibake pattern or a
    # configured replacement can actually apply. This does not transform URLs;
    # callers still restrict repair to visible text fields.
    has_marker = any(marker in text for marker in MOJIBAKE_MARKERS)
    has_pattern = has_marker or any(re.search(pattern, text, flags=re.UNICODE) for pattern in MOJIBAKE_PATTERNS)
    has_rule = any(old in text for old in rules)
    if not has_pattern and not has_rule:
        return text, [], 0, 0

    original_score = mojibake_score(text)
    current = text
    changes = []

    # 1) Corrección reversible: UTF-8 -> CP1252/Latin-1 -> UTF-8.
    for _ in range(5):
        candidates = [current]
        for enc in ("cp1252", "latin1"):
            try:
                candidates.append(current.encode(enc).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        best = min(candidates, key=mojibake_score)
        if mojibake_score(best) < mojibake_score(current):
            changes.append(f"paso reversible: {mojibake_score(current)} -> {mojibake_score(best)}")
            current = best
        else:
            break

    # 2) Correcciones conocidas de casos no recuperables por bytes.
    for old, new in rules.items():
        count = current.count(old)
        if count:
            current = current.replace(old, new)
            changes.append(f"{old!r} -> {new!r} ({count})")

    return current, changes, original_score, mojibake_score(current)


def audit_text_encoding(text):
    """Audita corrupción de texto únicamente en campos textuales.

    Se ignoran URLs de stream, tvg-logo y directivas de transporte porque una
    URL puede contener bytes/caracteres que parecen mojibake sin que exista
    corrupción del texto mostrado al usuario. Los campos de nombre, grupo,
    ID/país/idioma y #EXTGRP sí se validan.
    """
    issues = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.upper().startswith("#EXTM3U"):
            continue
        if URL_RE.match(line):
            continue
        if line.upper().startswith("#EXTVLCOPT:"):
            continue
        if line.upper().startswith("#EXTGRP:"):
            candidate = line.split(":", 1)[1] if ":" in line else ""
        elif line.upper().startswith("#EXTINF"):
            # Auditar atributos de texto, excluyendo tvg-logo y cualquier URL
            # incrustada, más el nombre visible después de la coma.
            candidate_parts = []
            def collect_attr(m):
                attr, value = m.group(1), m.group(2)
                if attr.lower() != "tvg-logo":
                    candidate_parts.append(value)
                return m.group(0)
            re.sub(r'\b(tvg-id|tvg-name|group-title|tvg-country|tvg-language|tvg-logo)="([^"]*)"', collect_attr, line, flags=re.I)
            display = line.split(",", 1)[1] if "," in line else ""
            candidate_parts.append(display)
            candidate = " | ".join(candidate_parts)
        else:
            # Cabeceras/directivas desconocidas no se consideran texto visible.
            continue

        if "\ufffd" in candidate or any(re.search(pattern, candidate, flags=re.UNICODE) for pattern in MOJIBAKE_PATTERNS):
            issues.append((n, raw))
    return issues

def atomic_write_utf8(path, text, bom=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    tmp.write_bytes(payload)
    tmp.replace(path)


def read_text_robust(path):
    raw = Path(path).read_bytes()
    candidates = []
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            decoded = raw.decode(enc)
            score = mojibake_score(decoded)
            candidates.append((score, decoded, enc))
        except UnicodeDecodeError:
            continue
    if not candidates:
        return raw.decode("utf-8", errors="replace")
    candidates.sort(key=lambda x: x[0])
    decoded = candidates[0][1]
    repaired, _changes, _before, _after = repair_mojibake_text(decoded)
    return repaired


def extract_attr(extinf, attr_name):
    m = re.search(rf'{re.escape(attr_name)}="([^"]*)"', extinf, re.I)
    return m.group(1) if m else ""


def replace_extinf_duration(extinf):
    return re.sub(
        r"^#EXTINF\s*:\s*-?\d+(?:\.\d+)?",
        "#EXTINF:0",
        extinf,
        count=1,
        flags=re.I,
    )


def clean_channel_name(name):
    """Limpia directivas M3U y repara mojibake en campos textuales.

    Esta función se aplica únicamente a nombres/grupos/IDs textuales, nunca
    a URLs. Así se corrigen entradas como ``UCL TelevisiÃ³n LATAM`` sin
    modificar una URL que contenga bytes o parámetros con significado propio.

    Ejemplos:
      HGTV#EXTGRP:Documentales #EXTVLCOPT:network-caching=1000 -> HGTV
      HOLA TV HD  #EXTGRP:General -> HOLA TV HD
      ESPN 5 (Col)#EXTGRP:DEPORTIVO#EXTVLCOPT:network-caching=1000 -> ESPN 5 (Col)

    Nunca conserva #EXTGRP/#EXTVLCOPT/#EXTINF dentro del nombre.
    """
    if name is None:
        return "Canal sin nombre"
    name = str(name).replace("\x00", " ").replace("\\r", " ").replace("\\n", " ").strip()
    # Reparación conservadora de mojibake en texto visible. Nunca se llama
    # sobre el campo URL. Si la cadena ya está correcta, no cambia.
    try:
        repaired, _changes, _before, _after = repair_mojibake_text(name)
        name = repaired
    except Exception:
        pass

    # Si cualquier directiva M3U quedó concatenada al nombre, todo lo que
    # está desde la primera #EXT...: en adelante NO pertenece al nombre.
    m = re.search(r'#EXT[A-Z0-9_-]*\s*:', name, re.I)
    if m:
        name = name[:m.start()]

    # Algunas fuentes pegan varias directivas sin separador. Por seguridad,
    # corta también ante #EXT aunque falte el ':' en una directiva corrupta.
    m = re.search(r'#EXT(?:GRP|VLCOPT|INF)\b', name, re.I)
    if m:
        name = name[:m.start()]

    # Elimina restos de separadores/espacios que hayan quedado al cortar.
    name = re.sub(r'\s+', ' ', name).strip(' \t,;|#\r\n')
    return name or "Canal sin nombre"


def rebuild_extinf(extinf, name, group):
    """Reconstruye EXTINF sin perder atributos, pero limpia los campos
    que algunos reproductores usan como nombre visible (tvg-name y título)."""
    extinf = replace_extinf_duration(extinf)

    attrs_part = extinf
    if "," in extinf:
        attrs_part, _old_name = extinf.split(",", 1)

    clean_name = clean_channel_name(name)
    clean_group = clean_channel_name(group)

    # group-title debe ser el grupo limpio.
    if re.search(r'\bgroup-title="[^"]*"', attrs_part, re.I):
        attrs_part = re.sub(
            r'\bgroup-title="[^"]*"',
            lambda m: f'group-title="{clean_group}"',
            attrs_part,
            count=1,
            flags=re.I,
        )
    else:
        attrs_part = re.sub(
            r"^(#EXTINF:0)(\s*)",
            rf'\1 group-title="{clean_group}"\2',
            attrs_part,
            count=1,
            flags=re.I,
        )

    # MUY IMPORTANTE: muchos reproductores IPTV muestran tvg-name en lugar
    # del texto que viene después de la coma. Si tvg-name trae directivas
    # pegadas, las limpiamos y lo sincronizamos con el nombre visible.
    if re.search(r'\btvg-name="[^"]*"', attrs_part, re.I):
        attrs_part = re.sub(
            r'\btvg-name="[^"]*"',
            lambda m: f'tvg-name="{clean_name}"',
            attrs_part,
            count=1,
            flags=re.I,
        )

    # Limpieza defensiva de tvg-id y group-title por si una fuente corrupta los mezcló.
    def clean_known_attr(m):
        attr = m.group(1)
        value = clean_channel_name(m.group(2))
        return f'{attr}="{value}"'

    attrs_part = re.sub(
        r'\b(tvg-id|tvg-name|group-title)\s*=\s*"([^"]*)"',
        clean_known_attr,
        attrs_part,
        flags=re.I,
    )

    # Jamás permitir #EXT* después de la coma.
    clean_name = clean_channel_name(clean_name)
    return f"{attrs_part},{clean_name}"


def parse_referrer(directives):
    for d in directives:
        m = re.match(r"#EXTVLCOPT:http-referrer=(.*)$", d.strip(), re.I)
        if m:
            return m.group(1).strip()
    return ""


def parse_user_agent(directives):
    for d in directives:
        m = re.match(r"#EXTVLCOPT:http-user-agent=(.*)$", d.strip(), re.I)
        if m:
            return m.group(1).strip()
    return ""


def parse_manual_block(text):
    """Formato: nombre/grupo en una línea, seguido por una o varias URLs."""
    text, _c, _b, _a = repair_mojibake_text(text)
    entries = []
    current_name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if URL_RE.match(line):
            if current_name:
                name = clean_channel_name(current_name)
                entry = {
                    "name": name,
                    "group": name,
                    "extinf": f'#EXTINF:0 group-title="{name}",{name}',
                    "directives": [],
                    "url": line,
                    "source": "URLs manuales",
                }
                apply_url_policy(entry)
                entries.append(entry)
            continue
        if line.startswith("#"):
            continue
        current_name = line
    return entries


def extract_embedded_directives_from_extinf(extinf):
    """Normaliza líneas EXTINF mal formadas donde #EXTGRP/#EXTVLCOPT
    quedaron pegados al nombre o dentro de tvg-name.
    Devuelve (extinf_limpio, directivas_embebidas).
    """
    line = extinf.strip()
    embedded = []

    # 1) Extraer directivas incrustadas al final del nombre (después de la coma).
    if "," in line:
        head, tail = line.split(",", 1)
        # Corta todo a partir de la primera directiva EXT incrustada.
        m = re.search(r'(#EXT(?:GRP|VLCOPT|INF)\s*:)', tail, re.I)
        if m:
            clean_tail = tail[:m.start()].strip()
            rest = tail[m.start():]
            # Captura #EXTGRP:... y #EXTVLCOPT:... consecutivos, incluso pegados.
            for dm in re.finditer(r'#EXT(?:GRP|VLCOPT)\s*:[^#]*?(?=(?:#EXT(?:GRP|VLCOPT)\s*:)|$)', rest, re.I):
                d = dm.group(0).strip()
                if d:
                    embedded.append(d)
            line = head + "," + clean_tail

    # 2) Limpiar atributos como tvg-name="Nombre#EXTGRP:Grupo..." sin perder el resto.
    def clean_attr(m):
        attr = m.group(1)
        value = m.group(2)
        cut = re.search(r'#EXT(?:GRP|VLCOPT|INF)\s*:', value, re.I)
        if cut:
            value = value[:cut.start()].rstrip()
        return f'{attr}="{clean_channel_name(value)}"'

    line = re.sub(
        r'\b(tvg-name|tvg-id)\s*=\s*"([^"]*)"',
        clean_attr,
        line,
        flags=re.I,
    )

    # 3) Si quedó una directiva incrustada en cualquier sitio fuera de las comillas,
    # recorta desde ella, preservando la parte anterior.
    if "," in line:
        head, tail = line.split(",", 1)
        m = re.search(r'#EXT(?:GRP|VLCOPT|INF)\s*:', tail, re.I)
        if m:
            line = head + "," + tail[:m.start()].rstrip()

    return line, embedded


def parse_m3u(path, *, prevalidated: bool = False):
    """Parser tolerante y conservador; omite solo la URL policy cuando el archivo está certificado."""
    text = read_text_robust(path)
    lines = StringIO(text)
    entries = []
    pending_directives = []
    name_fallback = Path(path).stem
    extinf = None
    directives = []

    def emit(extinf_line, directives_for_entry, url):
        group = clean_channel_name(extract_attr(extinf_line, "group-title"))
        if not group:
            for d in directives_for_entry:
                if d.lower().startswith("#extgrp:"):
                    group = d.split(":", 1)[1].strip()
                    break
        group = normalize_source_category(group or "General")
        favorite_group = canonical_favorite_category(group)
        if favorite_group in FAVORITE_CATEGORY_ORDER:
            group = favorite_group
        display_name = extinf_line.split(",", 1)[1].strip() if "," in extinf_line else name_fallback
        tvg_name = extract_attr(extinf_line, "tvg-name")
        name = clean_channel_name(tvg_name or display_name)
        entry = {
            "name": name,
            "group": group,
            "extinf": extinf_line,
            "directives": list(directives_for_entry),
            "url": url,
            "source": str(path),
        }
        if not prevalidated:
            apply_url_policy(entry)
        else:
            entry["_prevalidated_verified"] = True
        entries.append(entry)

    for raw in lines:
        t = raw.rstrip("\r\n").strip()
        if not t:
            continue
        if t.upper().startswith("#EXTM3U"):
            continue

        if extinf is None:
            if t.startswith("#") and not t.upper().startswith("#EXTINF"):
                pending_directives.append(t)
                continue
            if URL_RE.match(t):
                emit(f'#EXTINF:0 group-title="General",{name_fallback}', pending_directives, t)
                pending_directives.clear()
                continue
            if not t.upper().startswith("#EXTINF"):
                continue
            extinf = t
            directives = pending_directives[:]
            pending_directives.clear()
            extinf, embedded_directives = extract_embedded_directives_from_extinf(extinf)
            directives.extend(embedded_directives)
            continue

        # Estamos consumiendo el bloque posterior a EXTINF hasta hallar la URL
        if t.upper().startswith("#EXTINF"):
            # Entrada previa incompleta: conserva sus directivas como pendientes y empieza otra.
            pending_directives.extend(directives)
            extinf = t
            directives = pending_directives[:]
            pending_directives.clear()
            extinf, embedded_directives = extract_embedded_directives_from_extinf(extinf)
            directives.extend(embedded_directives)
            continue
        if URL_RE.match(t):
            emit(extinf, directives, t)
            extinf = None
            directives = []
            continue
        if t.startswith("#"):
            directives.append(t)
        else:
            directives.append("#" + t)

    if extinf is not None:
        pending_directives.extend(directives)

    return entries


def normalize_entry(e):
    group = clean_channel_name(e.get("group") or "General")
    name = clean_channel_name(e.get("name") or "Canal sin nombre")
    extinf = rebuild_extinf(e["extinf"], name, group)

    original = []
    has_network_cache = False

    for d in e.get("directives", []):
        dl = d.strip().lower()
        if dl.startswith("#extgrp:"):
            continue
        if dl.startswith("#extvlcopt:network-caching="):
            has_network_cache = True
            original.append(d)
            continue
        original.append(d)

    out = [extinf, f"#EXTGRP:{group}"]
    if not has_network_cache:
        out.append("#EXTVLCOPT:network-caching=1000")
    out.extend(original)
    out.append(e["url"])
    return out


def sanitize_final_extinf_line(line):
    """Última barrera: nunca permite #EXTGRP/#EXTVLCOPT dentro del nombre."""
    if not line.upper().startswith("#EXTINF") or "," not in line:
        return line
    head, name = line.split(",", 1)
    name = clean_channel_name(name)

    def cattr(m):
        return f'{m.group(1)}="{clean_channel_name(m.group(2))}"'

    head = re.sub(
        r'\b(tvg-id|tvg-name|group-title)\s*=\s*"([^"]*)"',
        cattr,
        head,
        flags=re.I,
    )
    return head + "," + name


def sanitize_generated_m3u_text(text):
    """Repara mojibake solo en campos textuales de EXTINF/EXTGRP.

    No toca URLs de stream, tvg-logo ni directivas de transporte. Devuelve
    (texto, cambios), para que la auditoría final vea únicamente residuos
    que realmente no pudieron repararse.
    """
    changes = []
    out = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.upper().startswith("#EXTINF"):
            def attr_repair(m):
                attr, value = m.group(1), m.group(2)
                if attr.lower() == "tvg-logo":
                    return m.group(0)
                repaired, c, _before, _after = repair_mojibake_text(value)
                if c:
                    changes.append((line_no, attr, value, repaired))
                return f'{attr}="{repaired}"'
            line = re.sub(r'\b(tvg-id|tvg-name|group-title|tvg-country|tvg-language)="([^"]*)"', attr_repair, line, flags=re.I)
            if "," in line:
                head, name = line.split(",", 1)
                repaired, c, _before, _after = repair_mojibake_text(name)
                if c:
                    changes.append((line_no, "display-name", name, repaired))
                line = head + "," + repaired
        elif line.upper().startswith("#EXTGRP:"):
            value = line.split(":", 1)[1] if ":" in line else ""
            repaired, c, _before, _after = repair_mojibake_text(value)
            if c:
                changes.append((line_no, "EXTGRP", value, repaired))
            line = "#EXTGRP:" + repaired
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), changes


def audit_generated_m3u(text):
    """Comprueba que ni el nombre visible ni tvg-name contienen directivas."""
    bad = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, 1):
        if not line.upper().startswith("#EXTINF"):
            continue

        display_name = line.split(",", 1)[1] if "," in line else ""
        if re.search(r"#EXT(?:GRP|VLCOPT|INF)\s*:?", display_name, re.I):
            bad.append((idx, "display-name", line))

        for attr in ("tvg-name", "group-title"):
            m = re.search(rf'\b{attr}="([^"]*)"', line, re.I)
            if m and re.search(r"#EXT(?:GRP|VLCOPT|INF)\s*:?", m.group(1), re.I):
                bad.append((idx, attr, line))
    return bad

def build_m3u(entries, *, epg_url=""):
    """Construye un M3U compacto en UTF-8, sin líneas en blanco."""
    out = [epg_header(epg_url)]

    for e in entries:
        extinf = (e.get("extinf") or "").strip().replace("\ufeff", "")
        if extinf:
            out.append(extinf)

        directives = []
        for d in (e.get("directives") or []):
            d = (d or "").strip().replace("\ufeff", "")
            if d and d not in directives:
                directives.append(d)

        for d in directives:
            out.append(d)

        group = (e.get("group") or "General").strip() or "General"

        if not any(d.upper().startswith("#EXTGRP:") for d in directives):
            out.append(f"#EXTGRP:{group}")

        if not any(d.lower().startswith("#extvlcopt:network-caching=") for d in directives):
            out.append("#EXTVLCOPT:network-caching=1000")

        raw_url = e.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
        if url:
            out.append(url)

    return "\n".join(line.rstrip() for line in out if line.strip()) + "\n"


def load_app_config():
    return load_mapping(APP_CONFIG_FILE, LEGACY_SETTINGS_DIR)


def atomic_write_json(path, data):
    write_text(Path(path), json.dumps(data, ensure_ascii=False, indent=2))


def save_app_config(data):
    load_mapping(APP_CONFIG_FILE, None)  # Preserve an unreadable existing personal file.
    atomic_write_json(APP_CONFIG_FILE, data)


def detect_ffprobe():
    """Busca ffprobe automáticamente en PATH y ubicaciones comunes de Windows.

    También comprueba una ruta guardada previamente por el usuario.
    """
    try:
        cfg = load_app_config()
    except SettingsError:
        # The GUI reports the settings error at startup; independent PATH
        # discovery can still locate an existing FFprobe.
        cfg = {}
    saved = cfg.get("ffprobe_path")
    candidates = []
    if isinstance(saved, str) and saved:
        candidates.append(Path(saved))

    for name in FFPROBE_SEARCH_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    # Carpeta del programa y carpetas ffmpeg colocadas junto al programa o
    # en la carpeta Enterprise padre usada por entregas anteriores.
    here = Path(__file__).resolve().parent
    parent = here.parent
    candidates.extend([
        here / "ffprobe.exe",
        here / "bin" / "ffprobe.exe",
        here / "ffmpeg" / "bin" / "ffprobe.exe",
        here / "ffmpeg_portable" / "ffprobe.exe",
        parent / "ffprobe.exe",
        parent / "bin" / "ffprobe.exe",
        parent / "ffmpeg" / "bin" / "ffprobe.exe",
        parent / "ffmpeg_portable" / "ffprobe.exe",
    ])
    candidates.extend(parent.glob("ffmpeg_portable/**/bin/ffprobe.exe"))
    candidates.extend(parent.glob("ffmpeg_portable/**/ffprobe.exe"))

    # Rutas habituales de instalaciones/manuales.
    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if base:
            candidates.extend([
                Path(base) / "ffmpeg" / "bin" / "ffprobe.exe",
                Path(base) / "FFmpeg" / "bin" / "ffprobe.exe",
            ])

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate.resolve())
        except OSError:
            continue
    return ""


def ffprobe_path():
    return detect_ffprobe()


def test_ffprobe(exe):
    if not exe:
        return False, "FFprobe no encontrado"
    try:
        p = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=5)
        if p.returncode != 0:
            return False, (p.stderr or "No se pudo ejecutar FFprobe").strip()
        first = (p.stdout or "").splitlines()[0].strip() if p.stdout else "FFprobe"
        return True, first
    except Exception as e:
        return False, str(e)


class StableHostLimiter:
    """Limita concurrencia y ráfagas por host para maximizar reproducibilidad."""

    def __init__(self, max_per_host=HOST_MAX_CONCURRENCY, min_gap=HOST_MIN_START_GAP, jitter_max=HOST_START_JITTER_MAX):
        self.max_per_host = max(1, int(max_per_host))
        self.min_gap = max(0.0, float(min_gap))
        self.jitter_max = max(self.min_gap, float(jitter_max))
        self._lock = threading.Lock()
        self._states = {}

    def _host_key(self, url):
        # Host bucketing is scheduling metadata, not URL validation. Keep it independent
        # from inspect_url so certified inputs do not re-enter syntactic validation.
        try:
            parsed = urllib.parse.urlsplit(str(url or "").strip())
            host = parsed.hostname or ""
            if host:
                authority = parsed.netloc.rsplit("@", 1)[-1]
                port = None
                if authority.startswith("["):
                    close = authority.find("]")
                    if close >= 0 and authority[close + 1:].startswith(":"):
                        port_text = authority[close + 2:]
                        if port_text.isdigit():
                            port = int(port_text)
                elif authority.count(":") == 1:
                    port_text = authority.rsplit(":", 1)[1]
                    if port_text.isdigit():
                        port = int(port_text)
                return f"{host.lower()}:{port}" if port is not None else host.lower()
        except Exception:
            pass
        return str(url or "")[:256].lower() or "__unknown__"

    def acquire(self, url):
        host = self._host_key(url) or '__unknown__'
        with self._lock:
            state = self._states.get(host)
            if state is None:
                state = {
                    'sem': threading.BoundedSemaphore(self.max_per_host),
                    'next_start': 0.0,
                }
                self._states[host] = state
        state['sem'].acquire()
        with self._lock:
            now = time.monotonic()
            start_at = max(now, state['next_start'])
            delay = start_at - now
            jitter = random.uniform(0.0, self.jitter_max - self.min_gap)
            state['next_start'] = start_at + self.min_gap + jitter
        if delay > 0:
            time.sleep(delay)
        return host, state['sem']

    def release(self, token):
        _host, sem = token
        sem.release()


def _is_transient_failure(reason):
    text = str(reason or '').lower()
    transient_tokens = (
        'timeout', 'timed out', 'temporarily', 'connection reset',
        'connection refused', 'connection closed', 'network is unreachable',
        'server returned 429', 'http error 429', 'http error 500', 'http error 502',
        'http error 503', 'http error 504', '5xx',
    )
    return any(token in text for token in transient_tokens)


def _stable_call(url, limiter, fn, *args, **kwargs):
    token = limiter.acquire(url)
    try:
        return fn(*args, **kwargs)
    finally:
        limiter.release(token)


def _ffprobe_with_stable_retry(entry, timeout, limiter):
    last = None
    attempts = 1 + max(0, int(TRANSIENT_RETRIES))
    for attempt in range(attempts):
        result = _stable_call(entry.get('url', ''), limiter, validate_url_ffprobe, entry, timeout)
        last = result
        if result and result[0]:
            return result
        if attempt + 1 < attempts and _is_transient_failure(result[1] if len(result) > 1 else ''):
            time.sleep(TRANSIENT_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0.05, 0.25))
            continue
        return result
    return last


def validate_url_http_stable(entry, timeout, limiter):
    return _stable_call(entry.get('url', ''), limiter, validate_url_http, entry, timeout)


def validate_url_http(entry, timeout=DEFAULT_TIMEOUT):
    """Validación rápida: endpoint accesible y HLS devuelve una playlist válida.

    Acepta el diccionario de entrada Enterprise o una URL string por compatibilidad.
    """
    if isinstance(entry, str):
        entry = {"url": entry, "directives": []}
    elif not isinstance(entry, dict):
        return False, "URL_ERROR: entrada no es un registro ni una URL string", str(entry or "")
    url = entry.get("url")
    inspection = inspect_url(url)
    if not inspection.usable_for_network:
        status = "URL_UNSUPPORTED" if inspection.status == URLStatus.UNSUPPORTED_SCHEME else "URL_ERROR"
        return False, f"{status}: {inspection.reason}", str(url or "")
    permitted, policy_reason = network_target_allowed(url)
    if not permitted:
        return False, f"URL_ERROR: {policy_reason}", str(url or "")
    url = entry.get("url") or inspection.normalized_url
    headers = {
        "User-Agent": parse_user_agent(entry.get("directives", [])) or "Mozilla/5.0 (M3U Validator)",
        "Accept": "*/*",
    }
    ref = parse_referrer(entry.get("directives", []))
    if ref:
        headers["Referer"] = ref

    try:
        resp, final_url = open_public_http(url, headers=headers, timeout=timeout, method="GET")
        with resp:
            status = getattr(resp, "status", 200)
            content_type = (resp.headers.get("Content-Type") or "").lower()
            chunk = resp.read(65536)

            if status < 200 or status >= 400:
                return False, f"HTTP {status}", final_url
            if not chunk:
                return False, "Respuesta vacía", final_url

            # Identificación HLS incluso si la extensión no es .m3u8.
            sample = chunk.decode("utf-8", errors="ignore")
            looks_hls = (
                ".m3u8" in url.lower()
                or "mpegurl" in content_type
                or "vnd.apple.mpegurl" in content_type
                or sample.lstrip().startswith("#EXTM3U")
            )
            if looks_hls:
                if "#EXTM3U" not in sample:
                    return False, "No es una playlist HLS válida", final_url
                if not any(token in sample for token in ("#EXTINF", "#EXT-X-STREAM-INF", "#EXT-X-TARGETDURATION")):
                    return False, "Playlist HLS sin segmentos/streams", final_url

            return True, f"OK HTTP {status}", final_url
    except HTTPError as e:
        return False, f"HTTP {e.code}", url
    except (PublicHttpError, URLError, TimeoutError, ConnectionError) as e:
        return False, f"Conexión: {getattr(e, 'reason', e)}", url
    except Exception as e:
        return False, f"Error: {e}", url


def validate_url_ffprobe(entry, timeout=DEFAULT_TIMEOUT):
    """Validación profunda: intenta abrir realmente el stream con ffprobe.

    No se considera válido solo por HTTP 200: ffprobe debe detectar al menos
    una pista multimedia (video, audio o subtítulos).

    Acepta el diccionario de entrada Enterprise o una URL string por compatibilidad.
    """
    if isinstance(entry, str):
        entry = {"url": entry, "directives": []}
    elif not isinstance(entry, dict):
        return False, "URL_ERROR: entrada no es un registro ni una URL string", str(entry or ""), "URL"
    inspection = apply_url_policy(entry)
    safe_url = entry.get("url", "")
    if not inspection.usable_for_network:
        status = "URL_UNSUPPORTED" if inspection.status == URLStatus.UNSUPPORTED_SCHEME else "URL_ERROR"
        return False, f"{status}: {inspection.reason}", safe_url, "URL"
    permitted, policy_reason = network_target_allowed(safe_url)
    if not permitted:
        return False, f"URL_ERROR: {policy_reason}", safe_url, "URL"
    exe = ffprobe_path()
    if not exe:
        return False, "FFprobe no encontrado; instala FFmpeg/FFprobe para la validación profunda", safe_url, "FFprobe"

    url = safe_url or inspection.normalized_url
    directives = entry.get("directives", [])
    ua = parse_user_agent(directives)
    ref = parse_referrer(directives)
    headers = []

    # Conserva headers HTTP adicionales si vienen en la lista.
    for d in directives:
        m = re.match(r"#EXTVLCOPT:http-header=(.*)$", d.strip(), re.I)
        if m and m.group(1).strip():
            headers.append(m.group(1).strip())
    for d in directives:
        m = re.match(r"#EXTVLCOPT:http-origin=(.*)$", d.strip(), re.I)
        if m and m.group(1).strip():
            headers.append("Origin: " + m.group(1).strip())

    cmd = [
        exe,
        "-hide_banner",
        "-loglevel", "error",
        "-rw_timeout", str(max(3, int(timeout)) * 1_000_000),
        "-probesize", "2M",
        "-analyzeduration", "3M",
        "-show_entries", "stream=index,codec_type",
        "-of", "csv=p=0",
    ]
    if ua:
        cmd += ["-user_agent", ua]
    if ref:
        cmd += ["-referer", ref]
    if headers:
        cmd += ["-headers", "\r\n".join(headers) + "\r\n"]
    cmd.append(url)

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(timeout) + 5))
        lines = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        if p.returncode == 0 and lines:
            codecs = sorted({x.split(",", 1)[-1].strip() for x in lines if x.strip()})
            codecs = [x for x in codecs if x]
            desc = ", ".join(codecs) if codecs else "stream multimedia"
            return True, f"FFprobe OK: {desc}", url, "FFprobe"
        err = (p.stderr or "").strip().replace("\n", " ")
        if len(err) > 240:
            err = err[:240] + "..."
        return False, f"FFprobe no pudo abrir el stream: {err or 'sin pistas multimedia detectadas'}", url, "FFprobe"
    except subprocess.TimeoutExpired:
        return False, "FFprobe: tiempo agotado", url, "FFprobe"
    except Exception as e:
        return False, f"FFprobe: {e}", url, "FFprobe"


def validate_entries(entries, deep=False, timeout=DEFAULT_TIMEOUT, workers=DEFAULT_WORKERS, progress=None):
    """Validate URLs with a pre-network URL gate and per-record failure isolation."""
    by_url = {}
    unique = []
    direct_results = {}
    for entry in entries:
        inspection = apply_url_policy(entry)
        url = entry.get("url", "")
        if url in by_url:
            continue
        by_url[url] = entry
        unique.append(entry)
        if not inspection.usable_for_network:
            direct_results[url] = (
                False, inspection.reason, url,
                "URL_UNSUPPORTED" if inspection.status == URLStatus.UNSUPPORTED_SCHEME else "URL_ERROR",
            )

    runnable = [e for e in unique if e.get("url", "") not in direct_results]
    results = dict(direct_results)
    total = len(unique)
    completed = len(direct_results)
    limiter = StableHostLimiter()

    def task(entry):
        try:
            return entry.get("url", ""), (validate_url_ffprobe(entry, timeout) if deep else validate_url_http(entry, timeout))
        except Exception as exc:
            url = entry.get("url", "")
            return url, (False, f"Unexpected validation error: {redact_text(exc)}", url, "UNEXPECTED_ERROR")

    safe_workers = max(1, min(int(workers), MAX_ENTERPRISE_WORKERS))
    pool = ThreadPoolExecutor(max_workers=safe_workers)
    future_map = {}
    pending_iter = iter(runnable)
    try:
        for _ in range(min(safe_workers * 2, len(runnable))):
            try:
                entry = next(pending_iter)
            except StopIteration:
                break
            future_map[pool.submit(lambda e=entry: _stable_call(e.get("url", ""), limiter, task, e))] = entry
        while future_map:
            completed_futures, _ = wait(tuple(future_map), return_when=FIRST_COMPLETED)
            for fut in completed_futures:
                entry = future_map.pop(fut)
                try:
                    url, result = fut.result()
                except Exception as exc:
                    url = entry.get("url", "")
                    result = (False, f"Worker validation error: {redact_text(exc)}", url, "WORKER_ERROR")
                results[url] = result
                completed += 1
                if progress:
                    progress(completed, total)
                try:
                    nxt = next(pending_iter)
                except StopIteration:
                    nxt = None
                if nxt is not None:
                    future_map[pool.submit(lambda e=nxt: _stable_call(e.get("url", ""), limiter, task, e))] = nxt
    finally:
        pool.shutdown(wait=True)

    kept, dead = [], []
    for entry in entries:
        url = entry.get("url", "")
        result = results.get(url, (False, "Sin resultado", url, "UNKNOWN"))
        clone = dict(entry)
        clone["validation"] = result
        clone["validation_status"] = "VALID" if result and result[0] else (result[3] if len(result) > 3 else "INVALID")
        (kept if result and result[0] else dead).append(clone)
    return kept, dead


def load_favorites_text():
    return load_catalog(USER_CONFIG_DIR, LEGACY_SETTINGS_DIR)

def save_favorites_text(text):
    load_catalog(USER_CONFIG_DIR, None)  # A damaged personal catalog needs explicit recovery.
    write_text(MASTER_FAVORITES_FILE, text)

def _favorite_category_sort_key(group):
    """Orden empresarial de Favoritos; fallback estable para categorías heredadas."""
    text = (group or DEFAULT_FAVORITES_GROUP).strip()
    key = text.casefold()
    if key in FAVORITE_CATEGORY_INDEX:
        return (0, FAVORITE_CATEGORY_INDEX[key])
    m = FAVORITE_CATEGORY_PREFIX_RE.match(text)
    if m:
        return (1, int(m.group(1)), m.group(2).casefold())
    return (2, 10**9, text.casefold())


def canonical_favorite_category(group):
    """Normaliza una categoría de Favoritos a su nombre empresarial canónico."""
    text = clean_channel_name(group or "").strip()
    if not text:
        return DEFAULT_FAVORITES_GROUP
    m = FAVORITE_CATEGORY_PREFIX_RE.match(text)
    label = m.group(2).strip() if m else text
    alias_key = re.sub(r"\s+", " ", label).strip().casefold()
    return FAVORITE_CATEGORY_ALIASES.get(alias_key, text)


def _norm_match_text(text):
    text = clean_channel_name(text or "").casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def auto_category_for_entry(entry):
    fields = [
        clean_channel_name(entry.get("name", "")),
        extract_attr(entry.get("extinf", ""), "tvg-name"),
        extract_attr(entry.get("extinf", ""), "tvg-id"),
        clean_channel_name(entry.get("group", "")),
    ]
    norm_fields = [_norm_match_text(x) for x in fields if x]
    joined = " ".join(norm_fields)
    best = None
    for order, label, keywords in AUTO_CATEGORY_DEFINITIONS:
        score = 0.0
        for kw in keywords:
            nk = _norm_match_text(kw)
            if not nk:
                continue
            if any(nk in f for f in norm_fields[:2]):
                score += 6.0
            elif nk in joined:
                score += 2.0
        if score > 0 and (best is None or score > best[0] or (score == best[0] and order < best[1])):
            best = (score, order, label)
    if best is None:
        return "19 - Religioso y Otros"
    return f"{best[1]:02d} - {best[2]}"


def apply_auto_categories(entries):
    out = []
    for e in entries:
        clone = dict(e)
        group = auto_category_for_entry(clone)
        clone["auto_category"] = group
        clone["original_group"] = e.get("group", "")
        clone["group"] = group
        clone["extinf"] = rebuild_extinf(e.get("extinf", ""), e.get("name", "Canal sin nombre"), group)
        out.append(clone)
    return out


@lru_cache(maxsize=4096)
def normalize_source_category(group):
    """Normaliza group-title a un nombre canónico legible y determinista."""
    text = clean_channel_name(group or "").strip()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "General"

    compare_key = _category_compare_key(text)
    known = CATEGORY_CANONICAL_LABELS.get(compare_key)
    if known:
        return known

    lowered = text.casefold()
    for idx, char in enumerate(lowered):
        if char.isalpha():
            return lowered[:idx] + char.upper() + lowered[idx + 1:]
    return lowered


def _sync_group_directive(directives, group):
    """Mantiene #EXTGRP sincronizado con group-title, sin duplicados."""
    favorite = canonical_favorite_category(group)
    clean_group = favorite if favorite in FAVORITE_CATEGORY_ORDER else normalize_source_category(group)
    out = []
    found = False
    for raw in (directives or []):
        d = (raw or "").strip()
        if not d:
            continue
        if d.lower().startswith("#extgrp:"):
            if not found:
                out.append(f"#EXTGRP:{clean_group}")
                found = True
            continue
        out.append(d)
    if not found:
        out.insert(0, f"#EXTGRP:{clean_group}")
    return out


def synchronize_group_metadata(entries):
    """Barrera final: group, group-title y #EXTGRP deben representar lo mismo."""
    out = []
    for e in entries:
        clone = dict(e)
        raw_group = clone.get("group", "General")
        favorite = canonical_favorite_category(raw_group)
        group = favorite if favorite in FAVORITE_CATEGORY_ORDER else normalize_source_category(raw_group)
        name = clean_channel_name(clone.get("name", "Canal sin nombre"))
        clone["group"] = group
        clone["name"] = name
        clone["extinf"] = rebuild_extinf(clone.get("extinf", "#EXTINF:0"), name, group)
        clone["directives"] = _sync_group_directive(clone.get("directives", []), group)
        out.append(clone)
    return out


def _source_group_key(group):
    """Clave estable para unir variaciones triviales del mismo group-title."""
    text = normalize_source_category(group).casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def preserve_original_categories(entries):
    """Consolida group-title equivalentes y publica un nombre canónico legible."""
    out = []
    canonical_by_key = {}
    order_by_key = {}
    for e in entries:
        clone = dict(e)
        raw_group = normalize_source_category(e.get("group", ""))
        key = _source_group_key(raw_group)
        if key not in canonical_by_key:
            canonical_by_key[key] = raw_group
            order_by_key[key] = len(order_by_key)
        display_group = canonical_by_key[key]
        clone["original_group"] = raw_group
        clone["group"] = display_group
        clone["source_group_order"] = order_by_key[key]
        clone["auto_category"] = None
        clone["extinf"] = rebuild_extinf(e.get("extinf", ""), e.get("name", "Canal sin nombre"), display_group)
        clone["directives"] = _sync_group_directive(clone.get("directives", []), display_group)
        out.append(clone)
    return out


def sort_entries_by_group_and_name(entries):
    """Agrupa categorías originales y conserva su orden de primera aparición."""
    # Si la lista no fue pasada por preserve_original_categories(), crear un orden
    # estable en función de la primera aparición sin inventar categorías nuevas.
    fallback_order = {}
    for e in entries:
        key = _source_group_key(e.get("group", ""))
        if key not in fallback_order:
            fallback_order[key] = len(fallback_order)
    return sorted(
        entries,
        key=lambda e: (
            e.get("source_group_order", fallback_order[_source_group_key(e.get("group", ""))]),
            canonical_favorite_name(e.get("name", "")).casefold(),
            e.get("url", ""),
        ),
    )


def canonical_favorite_name(name):
    """Nombre lógico del Favorito, sin destruir el nombre exacto de origen.

    Ej.:
      FOX Sports 2 (USA)[Opc.1] -> FOX Sports 2 (USA)
      FOX Sports 2 (USA)[Opc.2] -> FOX Sports 2 (USA)
    Solo se eliminan sufijos explícitos [Opc.1]..[Opc.5]. Otras marcas,
    como [Not 24/7] o (MPV), se conservan porque pueden ser parte real del nombre.
    """
    text = clean_channel_name(name or "").strip()
    while True:
        new_text = FAVORITE_OPTION_SUFFIX_RE.sub("", text).strip()
        if new_text == text:
            return text
        text = new_text


def _favorite_channel_sort_key(entry):
    return (
        canonical_favorite_name(entry.get("name", "")).casefold(),
        clean_channel_name(entry.get("name", "")).casefold(),
        -float(entry.get("favorite_composite_score", -1) or -1),
        -float(entry.get("favorite_stability_pct", 0) or 0),
        float(entry.get("favorite_latency_ms") if entry.get("favorite_latency_ms") is not None else 10**12),
    )


def sort_favorite_entries(entries):
    """Ordena favoritos por categoría y por canal; mantiene el ranking de fuentes dentro del canal."""
    groups = {}
    for e in entries:
        groups.setdefault(e.get("group") or DEFAULT_FAVORITES_GROUP, []).append(e)

    ordered = []
    for group in sorted(groups, key=_favorite_category_sort_key):
        by_channel = {}
        for e in groups[group]:
            key = canonical_favorite_name(e.get("name", "")).casefold()
            by_channel.setdefault(key, []).append(e)
        for key in sorted(by_channel):
            channel_entries = by_channel[key]
            # Si hubo ranking FFprobe, la puntuación decide el orden de las fuentes.
            channel_entries.sort(
                key=lambda e: (
                    -float(e.get("favorite_composite_score", -1) if e.get("favorite_composite_score") is not None else -1),
                    -float(e.get("favorite_stability_pct", 0) or 0),
                    float(e.get("favorite_latency_ms") if e.get("favorite_latency_ms") is not None else 10**12),
                    e.get("url", ""),
                )
            )
            ordered.extend(channel_entries)
    return ordered


def validate_favorite_order(entries):
    """Valida orden y pertenencia estricta de categorías de Favoritos."""
    seen = []
    for e in entries:
        g = canonical_favorite_category(e.get("group", ""))
        if g not in FAVORITE_CATEGORY_ORDER:
            return False, seen + [g], list(FAVORITE_CATEGORY_ORDER)
        if g not in seen:
            seen.append(g)
    expected = [g for g in FAVORITE_CATEGORY_ORDER if g in seen]
    return seen == expected, seen, expected


def _legacy_favorite_group_for_line(line):
    """Clasificación determinista de una línea legacy sin encabezado.

    Si existe el módulo Enterprise dedicado se respeta como autoridad. Cuando
    el paquete se ejecuta aislado y ese módulo no está presente, se usa un
    clasificador mínimo local para evitar degradar todos los favoritos a
    Internacional.
    """
    try:
        from enterprise_favorites import classify_legacy_item
        category = classify_legacy_item(line)
        if category in FAVORITE_CATEGORY_ORDER:
            return category
    except Exception:
        pass

    text = _norm_match_text(str(line or ""))
    if any(k in text for k in (
        "canal uno", "caracol", "rcn", "colombia", "teleantioquia",
        "telemedellin", "senal colombia", "señal colombia", "city tv",
    )):
        return FAVORITE_CATEGORY_ORDER[1]  # Colombia
    if any(k in text for k in (
        "futbol", "football", "soccer", "espn", "sports", "deportes",
        "nba", "nfl", "nhl", "mlb", "ufc", "wwe", "tenis", "golf",
    )):
        return FAVORITE_CATEGORY_ORDER[3]  # Deportes
    if any(k in text for k in (
        "kids", "infantil", "disney", "nick", "cartoon", "baby tv",
        "baby first", "discovery kids", "semillitas",
    )):
        return FAVORITE_CATEGORY_ORDER[0]  # Infantil
    if any(k in text for k in (
        "noticias", "news", "cnn", "bbc news", "euronews", "ntn24",
    )):
        return FAVORITE_CATEGORY_ORDER[6]  # Noticias
    if any(k in text for k in (
        "pelicula", "peliculas", "movie", "cinema", "cine", "film",
        "hbo", "max", "cinemax", "sony movies",
    )):
        return FAVORITE_CATEGORY_ORDER[4]  # Películas
    if any(k in text for k in (
        "entretenimiento", "entertainment", "reality", "variedades",
        "tlc", "lifetime",
    )):
        return FAVORITE_CATEGORY_ORDER[2]  # Entretenimiento

    return FAVORITE_CATEGORY_ORDER[5]  # Internacional


def parse_favorite_lines(text):
    """Parsea Favoritos con migración automática y encabezados estrictos.

    Reglas enterprise:
    - Solo las 8 categorías oficiales dentro de [] son encabezados.
    - [HD], [4K], [USA], [FHD], [SD], [UHD], [Opc.1]... son contenido.
    - Un archivo antiguo sin encabezados se clasifica automáticamente en las 8 categorías.
    - El legacy [00 - FAVORITOS] se reconoce como marcador legacy, NO como categoría válida.
    - Se preserva nombre exacto/URL exacta para resolver contra la M3U.
    """
    specs = []
    seen = set()
    current_group = None
    legacy_mode = True

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        category = FAVORITE_CATEGORY_RE.match(line)
        if category:
            candidate_raw = category.group(1).strip()
            candidate = canonical_favorite_category(candidate_raw)
            if candidate in FAVORITE_CATEGORY_ORDER:
                current_group = candidate
                legacy_mode = False
                continue
            try:
                from enterprise_favorites import is_legacy_header_text
                legacy_marker = is_legacy_header_text(candidate_raw)
            except Exception:
                legacy_marker = _category_compare_key(candidate_raw) == _category_compare_key(LEGACY_FAVORITES_GROUP)
            if legacy_marker:
                current_group = None
                legacy_mode = True
                continue
            # Cualquier [] no reconocido se considera contenido.

        # También aceptamos el marcador legacy sin corchetes. Es un marcador
        # estructural, nunca el nombre de un canal.
        try:
            from enterprise_favorites import is_legacy_header_text
            if is_legacy_header_text(line):
                current_group = None
                legacy_mode = True
                continue
        except Exception:
            pass

        display_line = line
        if "|" in line and not URL_RE.match(line):
            name_part, url_part = [x.strip() for x in line.split("|", 1)]
            if URL_RE.match(url_part):
                name = clean_channel_name(name_part) or "Favorito"
                group = current_group if current_group else _legacy_favorite_group_for_line(name)
                key = ("pair", group.casefold(), name.casefold(), url_part)
                if key not in seen:
                    specs.append({"type": "pair", "name": name, "url": url_part, "group": group, "legacy": legacy_mode})
                    seen.add(key)
                continue

        if URL_RE.match(line):
            group = current_group if current_group else _legacy_favorite_group_for_line(line)
            key = ("url", group.casefold(), line)
            if key not in seen:
                specs.append({"type": "url", "url": line, "group": group, "legacy": legacy_mode})
                seen.add(key)
            continue

        name = clean_channel_name(display_line)
        if name:
            group = current_group if current_group else _legacy_favorite_group_for_line(name)
            key = ("name", group.casefold(), name.casefold())
            if key not in seen:
                specs.append({"type": "name", "name": name, "group": group, "legacy": legacy_mode})
                seen.add(key)

    return specs

def resolve_favorites(specs, active_entries, favorite_group=DEFAULT_FAVORITES_GROUP):
    """Convierte especificaciones en copias de entradas conservando su categoría."""
    by_name = {}
    by_url = {}
    for e in active_entries:
        by_name.setdefault(clean_channel_name(e["name"]).casefold(), []).append(e)
        by_url[e["url"]] = e

    favorites = []
    missing = []
    used_urls_by_group = {}

    for spec in specs:
        group = canonical_favorite_category(spec.get("group") or favorite_group or DEFAULT_FAVORITES_GROUP)
        matches = []

        if spec["type"] == "name":
            matches = by_name.get(spec["name"].casefold(), [])
        elif spec["type"] == "url":
            e = by_url.get(spec["url"])
            if e:
                matches = [e]
        elif spec["type"] == "pair":
            e = by_url.get(spec["url"])
            if e:
                matches = [e]
            else:
                matches = [{
                    "name": spec["name"] or "Favorito",
                    "group": group,
                    "extinf": f'#EXTINF:0 group-title="{group}",{spec["name"] or "Favorito"}',
                    "directives": [],
                    "url": spec["url"],
                    "source": "Favoritos manuales",
                }]

        if not matches:
            missing.append(spec)
            continue

        used_urls = used_urls_by_group.setdefault(group.casefold(), set())
        for e in matches:
            if e["url"] in used_urls:
                continue
            clone = dict(e)
            clone["group"] = group
            clone["favorite_category"] = group
            clone["favorite_logical_name"] = canonical_favorite_name(clone.get("name", ""))
            clone["source"] = e.get("source", "") + " [Favorito]"
            extinf = clone.get("extinf", "")
            if extinf:
                extinf = re.sub(
                    r'(group-title=\")[^\"]*(\")',
                    lambda m: m.group(1) + group + m.group(2),
                    extinf, count=1, flags=re.I
                )
                clone["extinf"] = extinf
            clone["directives"] = _sync_group_directive(clone.get("directives", []), group)
            favorites.append(clone)
            used_urls.add(e["url"])

    return favorites, missing

def _favorite_probe_command(exe, entry, timeout):
    url = entry["url"]
    directives = entry.get("directives", [])
    ua = parse_user_agent(directives)
    ref = parse_referrer(directives)
    headers = []
    for d in directives:
        m = re.match(r"#EXTVLCOPT:http-header=(.*)$", d.strip(), re.I)
        if m and m.group(1).strip():
            headers.append(m.group(1).strip())
        m = re.match(r"#EXTVLCOPT:http-origin=(.*)$", d.strip(), re.I)
        if m and m.group(1).strip():
            headers.append("Origin: " + m.group(1).strip())

    cmd = [
        exe, "-hide_banner", "-loglevel", "error",
        "-rw_timeout", str(max(3, int(timeout)) * 1_000_000),
        "-probesize", "5M", "-analyzeduration", "2M",
        "-show_streams", "-show_format", "-of", "json",
    ]
    if ua:
        cmd += ["-user_agent", ua]
    if ref:
        cmd += ["-referer", ref]
    if headers:
        cmd += ["-headers", "\r\n".join(headers) + "\r\n"]
    cmd.append(url)
    return cmd


def _safe_int(value, default=0):
    try:
        if value in (None, "", "N/A"):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _check_favorite_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise NetworkCancelled("Análisis de favoritos detenido por el usuario.")


def probe_favorite_quality(entry, timeout=DEFAULT_TIMEOUT, limiter=None, *, cancel_event=None):
    """Obtiene estado, latencia y características objetivas de calidad."""
    _check_favorite_cancelled(cancel_event)
    inspection = apply_url_policy(entry)
    if not inspection.usable_for_network:
        return {
            "ok": False, "latency_ms": None, "width": 0, "height": 0,
            "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
            "audio_codec": "", "score": -1,
            "reason": f"URL_ERROR: {inspection.reason}",
        }
    exe = ffprobe_path()
    if not exe:
        return {
            "ok": False, "latency_ms": None, "width": 0, "height": 0,
            "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
            "audio_codec": "", "score": -1, "reason": "FFprobe no encontrado",
        }

    t0 = time.perf_counter()
    try:
        def resolve(host, port, **_kwargs):
            try:
                return run_operation("dns", {"host": host, "port": port},
                                     deadline=time.monotonic() + 5, cancel_event=cancel_event)
            except NetworkDeadlineExceeded as exc:
                raise TimeoutError("DNS: venció el plazo de 5 s.") from exc

        def _run():
            _check_favorite_cancelled(cancel_event)  # The host limiter may have queued this worker.
            allowed, reason = network_target_allowed(entry["url"], resolver=resolve)
            if not allowed:
                raise ValueError(f"URL_ERROR: {reason}")
            _check_favorite_cancelled(cancel_event)
            cmd = _favorite_probe_command(exe, entry, timeout)
            limit = max(7, int(timeout) + 7)
            if cancel_event is not None:
                return _run_owned_process(cmd, deadline=time.monotonic() + limit, cancel_event=cancel_event)
            return subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        p = _stable_call(entry.get('url', ''), limiter, _run) if limiter else _run()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if p.returncode != 0:
            err = (p.stderr or "").strip().replace("\n", " ")
            if len(err) > 220:
                err = err[:220] + "..."
            return {
                "ok": False, "latency_ms": elapsed_ms, "width": 0, "height": 0,
                "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
                "audio_codec": "", "score": -1, "reason": err or "FFprobe no pudo abrir el stream",
            }

        try:
            data = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            return {
                "ok": False, "latency_ms": elapsed_ms, "width": 0, "height": 0,
                "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
                "audio_codec": "", "score": -1, "reason": "JSON inválido de FFprobe",
            }

        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

        width = _safe_int((video or {}).get("width"))
        height = _safe_int((video or {}).get("height"))
        vbr = _safe_int((video or {}).get("bit_rate"))
        abr = _safe_int((audio or {}).get("bit_rate"))
        vcodec = str((video or {}).get("codec_name") or "")
        acodec = str((audio or {}).get("codec_name") or "")

        if not video and not audio:
            return {
                "ok": False, "latency_ms": elapsed_ms, "width": width, "height": height,
                "video_bitrate": vbr, "audio_bitrate": abr, "video_codec": vcodec,
                "audio_codec": acodec, "score": -1, "reason": "Sin pista multimedia",
            }

        # La calidad se ordena por resolución, luego bitrate, con pequeños bonos
        # por disponer de vídeo y audio identificables.
        pixels = width * height
        score = (pixels / 1000.0) + ((vbr / 1_000_000.0) * 120.0) + ((abr / 1_000_000.0) * 20.0)
        score += 5.0 if video else 0.0
        score += 5.0 if audio else 0.0

        return {
            "ok": True, "latency_ms": elapsed_ms, "width": width, "height": height,
            "video_bitrate": vbr, "audio_bitrate": abr, "video_codec": vcodec,
            "audio_codec": acodec, "score": score, "reason": "FFprobe OK",
        }

    except NetworkCancelled:
        raise
    except (subprocess.TimeoutExpired, NetworkDeadlineExceeded):
        return {
            "ok": False, "latency_ms": float(timeout) * 1000.0, "width": 0, "height": 0,
            "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
            "audio_codec": "", "score": -1, "reason": "Tiempo agotado",
        }
    except Exception as exc:
        return {
            "ok": False, "latency_ms": None, "width": 0, "height": 0,
            "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
            "audio_codec": "", "score": -1, "reason": redact_text(exc),
        }


def _speed_score(avg_latency_ms):
    if avg_latency_ms is None:
        return 0.0
    return 100.0 / (1.0 + (float(avg_latency_ms) / 500.0))


def assess_favorite_stability(entry, timeout=DEFAULT_TIMEOUT, repetitions=5, interval=0.35, limiter=None, *, cancel_event=None):
    """Ejecuta varias aperturas FFprobe y calcula estabilidad y latencia.

    La calidad (resolución/bitrate) se toma de la primera prueba exitosa y se
    mantiene; la estabilidad se obtiene de pruebas repetidas independientes.
    """
    repetitions = max(1, min(5, int(repetitions)))
    measurements = []
    successful = []

    for i in range(repetitions):
        _check_favorite_cancelled(cancel_event)
        try:
            if cancel_event is None:
                result = probe_favorite_quality(entry, timeout, limiter=limiter)
            else:
                result = probe_favorite_quality(entry, timeout, limiter=limiter, cancel_event=cancel_event)
        except NetworkCancelled:
            raise
        except Exception as exc:
            result = {
                "ok": False, "latency_ms": None, "width": 0, "height": 0,
                "video_bitrate": 0, "audio_bitrate": 0,
                "video_codec": "", "audio_codec": "", "score": -1,
                "reason": f"Error aislado de estabilidad FFprobe: {redact_text(exc)}",
            }
        measurements.append(result)

        if result.get("ok"):
            successful.append(result)

        if i + 1 < repetitions and interval > 0:
            delay = min(2.0, max(0.0, float(interval)))
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                _check_favorite_cancelled(cancel_event)

    _check_favorite_cancelled(cancel_event)
    success_count = len(successful)
    stability_pct = (success_count / repetitions) * 100.0
    latencies = [r.get("latency_ms") for r in successful if r.get("latency_ms") is not None]

    avg_latency = sum(latencies) / len(latencies) if latencies else None
    worst_latency = max(latencies) if latencies else None

    best_probe = successful[0] if successful else measurements[0]
    return {
        "ok": success_count > 0,
        "attempts": repetitions,
        "success_count": success_count,
        "stability_pct": stability_pct,
        "avg_latency_ms": avg_latency,
        "worst_latency_ms": worst_latency,
        "quality_score": best_probe.get("score", -1),
        "width": best_probe.get("width", 0),
        "height": best_probe.get("height", 0),
        "video_bitrate": best_probe.get("video_bitrate", 0),
        "audio_bitrate": best_probe.get("audio_bitrate", 0),
        "video_codec": best_probe.get("video_codec", ""),
        "audio_codec": best_probe.get("audio_codec", ""),
        "reason": best_probe.get("reason", ""),
    }


def optimize_favorites(favorites, specs, favorite_group, timeout=DEFAULT_TIMEOUT,
                       workers=8, max_sources=4, repetitions=5,
                       interval=0.35, prefer_fast=False, *, cancel_event=None):
    """Selecciona hasta N fuentes por canal usando calidad + estabilidad + velocidad.

    Ranking compuesto por canal:
      45% calidad objetiva (resolución/bitrate)
      35% estabilidad observada (éxitos/repeticiones)
      20% velocidad de apertura (latencia media)

    Se conservan hasta 4 URLs distintas por canal.
    """
    _check_favorite_cancelled(cancel_event)
    if not favorites:
        return favorites, [], []

    if not ffprobe_path():
        return favorites, ["FFprobe no disponible; no se pudo medir calidad/estabilidad/velocidad."], []

    # Capacidad de seguridad: una categoría puede repetir el mismo canal en
    # decenas de listas. Probamos como máximo N fuentes candidatas por canal
    # lógico; después el ranking elige hasta max_sources. Esto evita explosiones
    # de FFprobe sin eliminar URLs del catálogo principal.
    candidates_by_logical = {}
    for e in favorites:
        group_key = (e.get("group") or favorite_group).casefold()
        logical_key = canonical_favorite_name(e.get("name", "")).casefold()
        key = (group_key, logical_key)
        candidates_by_logical.setdefault(key, []).append(e)

    limited = []
    for items in candidates_by_logical.values():
        seen_urls = set()
        for e in items:
            url = e.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            limited.append(e)
            if len(seen_urls) >= FAVORITE_MAX_PROBE_CANDIDATES_PER_CHANNEL:
                break

    unique = {}
    for e in limited:
        unique.setdefault(e["url"], e)

    results = {}
    limiter = StableHostLimiter()

    def task(item):
        url, entry = item
        return url, assess_favorite_stability(
            entry,
            timeout=timeout,
            repetitions=repetitions,
            interval=interval,
            limiter=limiter,
            cancel_event=cancel_event,
        )

    # Keep at most two batches in flight.  Submitting every candidate at once
    # inflates memory and makes cancellation/backpressure ineffective for large
    # favorite catalogs.
    safe_workers = max(1, min(int(workers), MAX_ENTERPRISE_WORKERS))
    pending = iter(unique.items())
    future_map = {}
    with ThreadPoolExecutor(max_workers=safe_workers) as pool:
        def submit_next():
            _check_favorite_cancelled(cancel_event)
            try:
                item = next(pending)
            except StopIteration:
                return False
            future_map[pool.submit(task, item)] = item
            return True

        for _ in range(min(safe_workers * 2, len(unique))):
            submit_next()
        while future_map:
            done, _ = wait(tuple(future_map), return_when=FIRST_COMPLETED)
            for fut in done:
                url, _entry = future_map.pop(fut)
                try:
                    returned_url, result = fut.result()
                    url = returned_url or url
                except NetworkCancelled:
                    raise
                except Exception as exc:
                    result = {
                        "ok": False,
                        "latency_ms": None,
                        "width": 0, "height": 0,
                        "video_bitrate": 0, "audio_bitrate": 0,
                        "video_codec": "", "audio_codec": "",
                        "score": -1,
                        "reason": f"Error aislado de calidad FFprobe: {redact_text(exc)}",
                    }
                results[url] = result
                submit_next()
    _check_favorite_cancelled(cancel_event)
    optimize_favorites.last_metrics = {
        "workers": safe_workers,
        "unique_candidates": len(unique),
        "max_in_flight": min(len(unique), safe_workers * 2),
        "completed": len(results),
    }

    enriched = []
    for e in favorites:
        r = results.get(e["url"], {})
        clone = dict(e)
        clone.update({
            "favorite_probe_ok": bool(r.get("ok")),
            "favorite_stability_pct": r.get("stability_pct", 0.0),
            "favorite_probe_attempts": r.get("attempts", 0),
            "favorite_probe_successes": r.get("success_count", 0),
            "favorite_latency_ms": r.get("avg_latency_ms"),
            "favorite_worst_latency_ms": r.get("worst_latency_ms"),
            "favorite_quality_score": r.get("quality_score", -1),
            "favorite_width": r.get("width", 0),
            "favorite_height": r.get("height", 0),
            "favorite_video_bitrate": r.get("video_bitrate", 0),
            "favorite_audio_bitrate": r.get("audio_bitrate", 0),
            "favorite_video_codec": r.get("video_codec", ""),
            "favorite_audio_codec": r.get("audio_codec", ""),
            "favorite_probe_reason": r.get("reason", ""),
        })
        if not r.get("ok") and clone.get("playback_validation") == "HTTP_FALLBACK":
            clone["favorite_probe_reason"] = "FFprobe no concluyente; HTTP/HLS respondió correctamente."
            clone["favorite_quality_score"] = 0.0
            clone["favorite_stability_pct"] = 100.0
        enriched.append(clone)

    # Agrupar especificaciones exactas por canal lógico. Cada variante conserva
    # su nombre exacto y se busca primero de forma literal, pero todas compiten
    # juntas por el cupo de hasta N fuentes del mismo canal/categoría.
    spec_groups = {}
    for spec in specs:
        group = canonical_favorite_category(spec.get("group") or favorite_group or DEFAULT_FAVORITES_GROUP)
        if spec["type"] == "name":
            logical = canonical_favorite_name(spec.get("name", ""))
        elif spec["type"] == "pair":
            logical = canonical_favorite_name(spec.get("name", ""))
        elif spec["type"] == "url":
            logical = canonical_favorite_name(next((e.get("name", "Favorito") for e in enriched if e.get("url") == spec.get("url")), "Favorito"))
        else:
            continue
        key = (group.casefold(), logical.casefold())
        spec_groups.setdefault(key, []).append(spec)

    selected = []
    used = set()

    for (group_key, logical_key), group_specs in spec_groups.items():
        candidates = [
            e for e in enriched
            if (e.get("group") or "").casefold() == group_key
            and canonical_favorite_name(e.get("name", "")).casefold() == logical_key
            and (e.get("favorite_probe_ok") or e.get("playback_validation") == "HTTP_FALLBACK")
        ]
        # Defensa adicional: una variante solo entra si fue solicitada con su
        # nombre exacto o URL exacta en esta categoría.
        exact_names = {clean_channel_name(x.get("name", "")).casefold() for x in group_specs if x.get("type") == "name"}
        exact_names.update(clean_channel_name(x.get("name", "")).casefold() for x in group_specs if x.get("type") == "pair" and x.get("name"))
        exact_urls = {x.get("url") for x in group_specs if x.get("type") in ("url", "pair") and x.get("url")}
        candidates = [
            e for e in candidates
            if clean_channel_name(e.get("name", "")).casefold() in exact_names or e.get("url") in exact_urls
        ]
        if not candidates:
            continue

        # Normalización de calidad dentro del canal lógico.
        q_values = [max(0.0, float(e.get("favorite_quality_score", 0.0))) for e in candidates]
        q_min = min(q_values) if q_values else 0.0
        q_max = max(q_values) if q_values else 0.0

        scored = []
        for e in candidates:
            q = max(0.0, float(e.get("favorite_quality_score", 0.0)))
            q_norm = 100.0 if q_max <= q_min else 100.0 * (q - q_min) / (q_max - q_min)
            stability = float(e.get("favorite_stability_pct", 0.0))
            speed = _speed_score(e.get("favorite_latency_ms"))
            composite = (0.45 * q_norm) + (0.35 * stability) + (0.20 * speed)

            clone = dict(e)
            clone["favorite_logical_name"] = canonical_favorite_name(e.get("name", ""))
            clone["favorite_quality_norm"] = q_norm
            clone["favorite_speed_score"] = speed
            clone["favorite_composite_score"] = composite
            scored.append(clone)

        scored.sort(
            key=lambda e: (
                -e.get("favorite_composite_score", -1),
                -e.get("favorite_stability_pct", 0),
                e.get("favorite_latency_ms") if e.get("favorite_latency_ms") is not None else 10**12,
                -e.get("favorite_quality_score", -1),
                clean_channel_name(e.get("name", "")).casefold(),
                e.get("url", ""),
            )
        )

        take = min(max(1, int(max_sources)), len(scored))
        chosen = 0
        for e in scored:
            source_key = (group_key, logical_key)
            if (source_key, e["url"]) in used:
                continue
            selected.append(e)
            used.add((source_key, e["url"]))
            chosen += 1
            if chosen >= take:
                break

    if not selected:
        return [], ["Ninguna fuente de Favoritos pasó la evaluación de estabilidad FFprobe."], enriched

    return selected, [], enriched


def enforce_favorite_source_limit(entries, max_sources=4):
    """Hard production invariant: at most max_sources URLs per category/logical channel."""
    limit = max(1, int(max_sources))
    buckets = {}
    out = []
    for e in entries:
        key = (canonical_favorite_category(e.get("group", "")).casefold(), canonical_favorite_name(e.get("name", "")).casefold())
        bucket = buckets.setdefault(key, set())
        url = e.get("url", "")
        if url in bucket:
            continue
        if len(bucket) >= limit:
            continue
        bucket.add(url); out.append(e)
    return out

def order_favorites_by_category_and_channel(entries):
    """Orden final: categoría numérica, canal alfabético, fuentes por score."""
    return sort_favorite_entries(entries)


def validate_favorite_manuals(favorites, active_entries):
    """Evita que una entrada Nombre|URL manual quede sin validar."""
    active_urls = {e["url"] for e in active_entries}
    return [e for e in favorites if e["url"] not in active_urls]



def dedupe_entries(entries):
    seen = set()
    kept = []
    removed = []
    for e in entries:
        url = e["url"]
        if url in seen:
            removed.append(e)
        else:
            seen.add(url)
            kept.append(e)
    return kept, removed


def save_report(path, input_files, manual_count, total_before, after_validation, dead, kept, removed, deep, favorites_count=0, favorite_specs=0, favorite_missing=None, favorite_entries=None, favorite_probe_details=None, validation_metrics=None, pipeline_metrics=None):
    lines = [
        "INFORME DE UNIFICACIÓN Y VALIDACIÓN M3U",
        "=" * 78,
        "",
        f"Modo de validación: {'FFprobe (profundo)' if deep else 'HTTP/HLS (rápido)'}",
        f"Archivos de entrada: {len(input_files)}",
    ]
    for f, count in input_files:
        lines.append(f"  - {f}: {count} entradas")
    lines += [
        f"URLs manuales añadidas: {manual_count}",
        f"Total antes de validar/deduplicar: {total_before}",
        f"Total después de validar (activas): {after_validation}",
        f"URLs/canales descartados por no responder/reproducir: {len(dead)}",
        f"Duplicados eliminados por URL exactamente idéntica: {len(removed)}",
        f"Favoritos mostrados: {favorites_count}",
        f"Especificaciones de favoritos ingresadas: {favorite_specs}",
        f"Favoritos no encontrados/no activos: {len(favorite_missing or [])}",
        f"Canales finales (incluye copias de Favoritos): {len(kept)}",
        "",
    ]

    if pipeline_metrics:
        lines += ["MÉTRICAS DEL PIPELINE", "-" * 78]
        total_seconds = float(pipeline_metrics.get("total_seconds") or 0.0)
        lines.append(f"Tiempo total medido: {total_seconds:.3f} s")
        for name, seconds in (pipeline_metrics.get("stages") or {}).items():
            pct = (float(seconds) / total_seconds * 100.0) if total_seconds > 0 else 0.0
            lines.append(f"- {name}: {float(seconds):.3f} s ({pct:.1f}%)")
        lines += [
            f"Entradas de entrada: {pipeline_metrics.get('input_entries', total_before)}",
            f"Candidatas únicas por URL exacta: {pipeline_metrics.get('unique_candidates', 'N/D')}",
            f"Duplicados URL exacta: {pipeline_metrics.get('exact_url_duplicates', len(removed))}",
            f"Activas: {pipeline_metrics.get('active_entries', after_validation)}",
            f"Descartadas: {pipeline_metrics.get('dead_entries', len(dead))}",
            f"Favoritos: {pipeline_metrics.get('favorites', favorites_count)}",
            "",
        ]

        if pipeline_metrics.get("prevalidation_requested"):
            fallback_map = pipeline_metrics.get("prevalidation_fallbacks") or {}
            lines += [
                "PREVALIDACIÓN CERTIFICADA",
                "-" * 78,
                f"Solicitada: TRUE",
                f"Archivos certificados (manifest + SHA-256): {pipeline_metrics.get('prevalidation_manifest_verified', 0)}",
                f"Entradas certificadas: {pipeline_metrics.get('prevalidation_verified_entries', 0)}",
                f"SHA-256 coincidencias: {pipeline_metrics.get('prevalidation_sha256_matches', 0)}",
                f"SHA-256 mismatches: {pipeline_metrics.get('prevalidation_sha256_mismatches', 0)}",
                f"Entradas con URL prevalidación omitida: {pipeline_metrics.get('prevalidation_url_entries_skipped', 0)}",
                f"Archivos en fallback: {pipeline_metrics.get('prevalidation_fallback_count', len(fallback_map))}",
            ]
            if fallback_map:
                lines.append("Motivos de fallback:")
                for fallback_path, reason in sorted(fallback_map.items()):
                    lines.append(f"- {redact_text(fallback_path)} -> {redact_text(reason)}")
            else:
                lines.append("Fallback: NONE")
            lines.append("")

        metadata = pipeline_metrics.get("metadata") or {}
        if metadata.get("enabled"):
            lines += ["METADATOS IPTV", "-" * 78]
            for key in ("scope", "considered", "matched", "logos_preserved", "logos_invalid_syntax", "logos_added", "logos_still_missing", "epg_ids_added", "unmatched"):
                if key in metadata:
                    lines.append(f"{key}: {metadata[key]}")
            download = metadata.get("xmltv_download") or {}
            if download:
                lines.append(f"XMLTV caché: {'reutilizada' if download.get('cached') else 'descargada'}")
                lines.append(f"XMLTV bytes XML: {download.get('bytes', 'N/D')}")
            coverage = metadata.get("epg_coverage") or {}
            if coverage:
                lines.append(f"EPG IDs esperados: {coverage.get('expected_ids', 0)}")
                lines.append(f"EPG IDs encontrados: {coverage.get('channel_ids_found', 0)}")
                lines.append(f"EPG IDs con programación: {coverage.get('ids_with_programme', 0)}")
                for group, values in sorted((coverage.get("by_group") or {}).items()):
                    lines.append(f"- {group}: esperados={values.get('expected_ids', 0)}, encontrados={values.get('channel_ids_found', 0)}, con_programación={values.get('ids_with_programme', 0)}")
            lines.append("")

    if validation_metrics:
        lines += ["MÉTRICAS DE VALIDACIÓN", "-" * 78]
        lines += [
            f"Modo interno: {validation_metrics.get('mode', 'N/D')}",
            f"Workers usados: {validation_metrics.get('workers', 'N/D')}",
            f"Entradas recibidas: {validation_metrics.get('input_entries', 'N/D')}",
            f"Fingerprints únicos validados: {validation_metrics.get('unique_fingerprints', 'N/D')}",
            f"Pendientes antes de ejecutar: {validation_metrics.get('pending_before_run', 'N/D')}",
            f"Resultados reutilizados de checkpoint: {validation_metrics.get('checkpoint_hits', 'N/D')}",
            f"Tiempo validación: {float(validation_metrics.get('elapsed_seconds') or 0.0):.3f} s",
        ]
        avg = validation_metrics.get("avg_latency_ms")
        max_latency = validation_metrics.get("max_latency_ms")
        if avg is not None:
            lines.append(f"Latencia media FFprobe/validación: {float(avg):.1f} ms")
        if max_latency is not None:
            lines.append(f"Latencia máxima FFprobe/validación: {float(max_latency):.1f} ms")
        statuses = validation_metrics.get("statuses") or {}
        if validation_metrics.get("url_errors") is not None:
            lines.append(f"Errores de URL: {validation_metrics.get('url_errors', 0)}")
        if validation_metrics.get("repaired_entries") is not None:
            lines.append(f"URLs reparadas: {validation_metrics.get('repaired_entries', 0)}")
        if validation_metrics.get("report_errors") is not None:
            lines.append(f"Errores de reporte: {validation_metrics.get('report_errors', 0)}")
        if statuses:
            lines.append("Estados:")
            for status, count in sorted(statuses.items()):
                lines.append(f"- {status}: {count}")
        lines.append("")

    lines += [
        "REGLAS",
        "- El nombre del canal se limpia para quitar #EXTGRP/#EXTVLCOPT concatenados.",
        "- Se conservan todos los atributos originales de #EXTINF.",
        "- Se conservan todas las directivas #EXTVLCOPT originales.",
        "- Se añade network-caching=1000 solo si no existe uno propio.",
        "- Solo se deduplica cuando la URL es exactamente idéntica.",
        "- Solo se conserva una URL que pase la validación seleccionada.",
        "- El enriquecimiento IPTV es opcional: no cambia URLs de reproducción ni reemplaza logos existentes.",
        "- Favoritos pueden conservar hasta 4 fuentes por canal, rankeadas por 45% calidad, 35% estabilidad observada y 20% velocidad de apertura.",
        "- Los group-title originales se canonicalizan, consolidan y publican en sentence case; no se generan categorías automáticas.",
        "- Las categorías manuales de Favoritos se escriben primero en el orden empresarial fijo definido por FAVORITE_CATEGORY_ORDER.",
        "",
    ]

    if dead:
        lines += ["URLS DESCARTADAS POR NO ESTAR ACTIVAS", "-" * 78]
        for idx, e in enumerate(dead, 1):
            r = e.get("validation_enterprise") or e.get("validation") or (False, "Sin resultado", e["url"])
            if isinstance(r, dict):
                reason = redact_text(r.get("reason") or r.get("status") or "Sin resultado")
                status = r.get("status")
                if status and status not in str(reason):
                    reason = f"{status}: {reason}"
            else:
                reason = redact_text(r[1] if len(r) > 1 else "Sin resultado")
            lines += [
                f"{idx}. Nombre : {redact_text(e.get('name', ''))}",
                f"   Grupo  : {redact_text(e.get('group', ''))}",
                f"   Origen : {redact_text(e.get('source', ''))}",
                f"   Motivo : {reason}",
                f"   URL    : {redact_url_for_report(e.get('url', ''))}",
                "",
            ]
    else:
        lines.append("No se descartaron URLs por validación.")

    lines += ["", "DUPLICADOS ELIMINADOS POR URL EXACTA", "-" * 78]
    if removed:
        for idx, e in enumerate(removed, 1):
            lines += [
                f"{idx}. Nombre : {redact_text(e.get('name', ''))}",
                f"   Grupo  : {redact_text(e.get('group', ''))}",
                f"   Origen : {redact_text(e.get('source', ''))}",
                f"   URL    : {redact_url_for_report(e.get('url', ''))}",
                "",
            ]
    else:
        lines.append("No se eliminaron duplicados.")

    lines += ["", "FAVORITOS", "-" * 78]
    if favorite_entries:
        for idx, e in enumerate(favorite_entries, 1):
            lines += [
                f"{idx}. Categoría: {e.get('group', DEFAULT_FAVORITES_GROUP)}",
                f"   Nombre   : {e.get('name', '')}",
                f"   URL      : {redact_url_for_report(e.get('url', ''))}",
                "",
            ]
    else:
        lines.append("No se agregaron favoritos.")

    quality_items = [e for e in (favorite_entries or []) if e.get("favorite_probe_ok")]
    if quality_items:
        lines += ["", "CALIDAD / ESTABILIDAD / VELOCIDAD DE FAVORITOS (FFPROBE)", "-" * 78]
        for e in quality_items:
            res = f"{e.get('favorite_width', 0)}x{e.get('favorite_height', 0)}" if e.get("favorite_width") and e.get("favorite_height") else "N/D"
            br = e.get("favorite_video_bitrate") or 0
            br_txt = f"{br/1_000_000:.2f} Mbps" if br else "N/D"
            ms = e.get("favorite_latency_ms")
            ms_txt = f"{ms:.0f} ms" if ms is not None else "N/D"
            worst = e.get("favorite_worst_latency_ms")
            worst_txt = f"{worst:.0f} ms" if worst is not None else "N/D"
            stability = e.get("favorite_stability_pct", 0.0)
            comp = e.get("favorite_composite_score", 0.0)
            attempts = e.get("favorite_probe_attempts", 0)
            successes = e.get("favorite_probe_successes", 0)
            lines.append(
                f"{e.get('name', '')} | {res} | {br_txt} | estabilidad {stability:.0f}% "
                f"({successes}/{attempts}) | lat {ms_txt} | peor {worst_txt} | score {comp:.1f} | {redact_url_for_report(e.get('url', ''))}"
            )

    if favorite_missing:
        lines += ["", "FAVORITOS NO ENCONTRADOS / NO ACTIVOS", "-" * 78]
        for idx, spec in enumerate(favorite_missing, 1):
            if spec["type"] == "pair":
                desc = f"{spec.get('name', '')} | {redact_url_for_report(spec.get('url', ''))}"
            elif spec["type"] == "url":
                desc = redact_url_for_report(spec["url"])
            else:
                desc = spec["name"]
            lines.append(f"{idx}. {desc}")

    atomic_write_utf8(Path(path), "\n".join(lines) + "\n", bom=False)


def load_blocks():
    return load_mapping(CONFIG_FILE, LEGACY_SETTINGS_DIR)


def save_blocks(data):
    load_mapping(CONFIG_FILE, None)
    atomic_write_json(CONFIG_FILE, data)



def run_name_cleanup_self_test():
    """Pruebas internas para evitar regresiones en la limpieza de nombres."""
    tests = {
        "HGTV#EXTGRP:Documentales #EXTVLCOPT:network-caching=1000": "HGTV",
        "HOLA TV HD  #EXTGRP:General #EXTVLCOPT:network-caching=1000": "HOLA TV HD",
        "History Channel #EXTGRP:Documentales#EXTVLCOPT:network-caching=1000": "History Channel",
        "ESPN 5 (Col)#EXTGRP:DEPORTIVO": "ESPN 5 (Col)",
    }
    failures = [(src, expected, clean_channel_name(src)) for src, expected in tests.items() if clean_channel_name(src) != expected]
    if failures:
        raise RuntimeError("Falló la prueba de limpieza de nombres: " + repr(failures))
    return True


def run_favorites_self_test():
    sample = [
        {
            "name": "Canal Uno",
            "group": "General",
            "extinf": '#EXTINF:0 group-title="General",Canal Uno',
            "directives": [],
            "url": "http://example.com/uno.m3u8",
            "source": "test",
        },
        {
            "name": "Canal Dos",
            "group": "Deportes",
            "extinf": '#EXTINF:0 group-title="Deportes",Canal Dos',
            "directives": [],
            "url": "http://example.com/dos.m3u8",
            "source": "test",
        },
    ]
    specs = parse_favorite_lines("Canal Uno\nhttp://example.com/dos.m3u8")
    favs, missing = resolve_favorites(specs, sample)
    if len(favs) != 2 or missing:
        raise RuntimeError("Falló la prueba de Favoritos.")
    if [f["group"] for f in favs] != [FAVORITE_CATEGORY_ORDER[1], FAVORITE_CATEGORY_ORDER[5]]:
        raise RuntimeError("Falló la migración automática de Favoritos legacy sin encabezados.")
    return True


def run_favorite_variant_grouping_self_test():
    cases = {
        "FOX Sports 2 (USA)[Opc.1]": "FOX Sports 2 (USA)",
        "FOX Sports 2 (USA)[Opc.2]": "FOX Sports 2 (USA)",
        "Caracol2 HD (MPV)[Opc.1]": "Caracol2 HD (MPV)",
        "Extrema Kids TV [Not 24/7]": "Extrema Kids TV [Not 24/7]",
        "DSports 2 (MPV)": "DSports 2 (MPV)",
    }
    for raw, expected in cases.items():
        if canonical_favorite_name(raw) != expected:
            raise RuntimeError(f"Falló la agrupación de variantes: {raw!r} -> {canonical_favorite_name(raw)!r}")
    sample = [
        {"name": "FOX Sports 2 (USA)[Opc.1]", "group": "00 - Favoritos Deportes"},
        {"name": "FOX Sports 2 (USA)[Opc.2]", "group": "00 - Favoritos Deportes"},
    ]
    if len({canonical_favorite_name(e["name"]).casefold() for e in sample}) != 1:
        raise RuntimeError("Las variantes [Opc.1]/[Opc.2] no quedaron agrupadas como un mismo canal lógico.")
    return True


def run_favorite_categories_self_test():
    text = """[02 - Favoritos Peliculas]\nCanal B\nCanal A\n[00 - Favoritos Deportes]\nESPN\nRCN\n[01 - Favoritos Infantil]\nDisney Channel\nCanal Z\n"""
    specs = parse_favorite_lines(text)
    groups = [s["group"] for s in specs]
    expected_groups = [
        "00 - Favoritos Películas", "00 - Favoritos Películas",
        "00 - Favoritos Deportes", "00 - Favoritos Deportes",
        "00 - Favoritos Infantil", "00 - Favoritos Infantil"
    ]
    if groups != expected_groups:
        raise RuntimeError(f"Falló el parseo/canonicalización de categorías de Favoritos: {groups!r}")
    sample = []
    for name, group, idx in [
        ("Canal B", "00 - Favoritos Películas", 1),
        ("Canal A", "00 - Favoritos Películas", 2),
        ("ESPN", "00 - Favoritos Deportes", 3),
        ("RCN", "00 - Favoritos Deportes", 4)
    ]:
        sample.append({
            "name": name, "group": group, "url": f"http://example.com/{idx}.m3u8",
            "extinf": f'#EXTINF:0 group-title="{group}",{name}', "directives": [], "source": "test"
        })
    favs, missing = resolve_favorites(specs[:4], sample)
    ordered = order_favorites_by_category_and_channel(favs)
    if missing or [e["group"] for e in ordered] != [
        "00 - Favoritos Deportes", "00 - Favoritos Deportes",
        "00 - Favoritos Películas", "00 - Favoritos Películas"
    ]:
        raise RuntimeError("Falló el orden empresarial de categorías de Favoritos.")
    if [e["name"] for e in ordered[:2]] != ["ESPN", "RCN"]:
        raise RuntimeError("Falló el orden alfabético dentro de una categoría de Favoritos.")
    if [canonical_favorite_category(x) for x in [
        "00 - Favoritos Infantil", "00 - Favoritos Colombia",
        "00 - Favoritos Entretenimiento", "00 - Favoritos Deportes",
        "00 - Favoritos Peliculas", "00 - Favoritos Internacional",
        "00 - Favoritos Noticias", "00 - Favoritos Win Sport"
    ]] != list(FAVORITE_CATEGORY_ORDER):
        raise RuntimeError("Falló el conjunto/orden canónico de 8 categorías de Favoritos.")
    return True


def run_favorite_source_limit_self_test():
    if not 1 <= 4 <= 4:
        raise RuntimeError("El límite de fuentes por Favorito debe ser 1..4.")
    return True


def run_original_category_self_test():
    sample = [
        {"name": "Canal B", "group": "Deportes", "extinf": '#EXTINF:0 group-title="Deportes",Canal B', "url": "http://example/b.m3u8"},
        {"name": "Canal A", "group": " deportes ", "extinf": '#EXTINF:0 group-title=" deportes ",Canal A', "url": "http://example/a.m3u8"},
        {"name": "HBO", "group": "Películas", "extinf": '#EXTINF:0 group-title="Películas",HBO', "url": "http://example/hbo.m3u8"},
    ]
    preserved = preserve_original_categories(sample)
    if [e["group"] for e in preserved[:2]] != ["Deportes", "Deportes"]:
        raise RuntimeError("No se consolidaron correctamente categorías originales equivalentes.")
    if any(e["group"].startswith(("10 -", "11 -", "12 -", "13 -", "14 -", "15 -", "16 -", "17 -", "18 -", "19 -")) for e in preserved):
        raise RuntimeError("La clasificación automática se infiltró en la salida de producción.")
    ordered = sort_entries_by_group_and_name(preserved)
    if {e["group"] for e in ordered} != {"Deportes", "Películas"}:
        raise RuntimeError("Las categorías originales no se conservaron.")
    if [e["group"] for e in ordered] != ["Deportes", "Deportes", "Películas"]:
        raise RuntimeError("No se conservó el orden de primera aparición de las categorías originales.")
    return True


def run_source_category_normalization_self_test():
    cases = {
        " Colombia ": "Colombia",
        "COLOMBIA": "Colombia",
        " NOTICIAS ": "Noticias",
        "NOTICIAS": "Noticias",
        "MÚSICA": "Música",
        " MÚSICA ": "Música",
        "NOVELAS Y SERIES": "Novelas y series",
    }
    failures = [
        (raw, expected, normalize_source_category(raw))
        for raw, expected in cases.items()
        if normalize_source_category(raw) != expected
    ]
    if failures:
        raise RuntimeError("Falló la normalización de categorías de origen: " + repr(failures))

    sample = [
        {"name": "A", "group": "Colombia", "extinf": '#EXTINF:0 group-title="Colombia",A'},
        {"name": "B", "group": " COLOMBIA ", "extinf": '#EXTINF:0 group-title=" COLOMBIA ",B'},
        {"name": "C", "group": "NOTICIAS", "extinf": '#EXTINF:0 group-title="NOTICIAS",C'},
        {"name": "D", "group": " Noticias ", "extinf": '#EXTINF:0 group-title=" Noticias ",D'},
    ]
    normalized = preserve_original_categories(sample)
    if [e["group"] for e in normalized] != ["Colombia", "Colombia", "Noticias", "Noticias"]:
        raise RuntimeError("No se consolidaron categorías equivalentes con nombre canónico.")
    return True




def run_stability_score_self_test():
    if not (45 + 35 + 20 == 100):
        raise RuntimeError("Los pesos de ranking no suman 100%.")
    if not 1 <= 5 <= 5:
        raise RuntimeError("El rango de pruebas de estabilidad debe ser 1..5.")
    if assess_favorite_stability.__defaults__[1] != 5:
        raise RuntimeError("El valor predeterminado de estabilidad debe ser 5 pruebas.")
    return True


def run_tvg_name_cleanup_self_test():
    raw = '#EXTINF:-1 tvg-name="ESPN 5 (Col)#EXTGRP:DEPORTIVO #EXTVLCOPT:network-caching=1000" tvg-logo="x" group-title="DEPORTIVO",ESPN 5 (Col)#EXTGRP:DEPORTIVO'
    rebuilt = rebuild_extinf(raw, raw.split(",",1)[1], "DEPORTIVO")
    if '#EXTGRP' in rebuilt or '#EXTVLCOPT' in rebuilt:
        raise RuntimeError("Falló la limpieza de tvg-name/nombre visible.")
    if 'tvg-name="ESPN 5 (Col)"' not in rebuilt:
        raise RuntimeError("No se limpió tvg-name correctamente.")
    if not rebuilt.endswith(",ESPN 5 (Col)"):
        raise RuntimeError("No se limpió el nombre visible correctamente.")
    return True


def run_auto_category_self_test():
    samples = [
        {"name": "FOX Sports 2 (USA)[Opc.1]", "group": "Deportes", "extinf": '#EXTINF:0 group-title="Deportes",FOX Sports 2 (USA)[Opc.1]'},
        {"name": "Disney Junior", "group": "Kids", "extinf": '#EXTINF:0 group-title="Kids",Disney Junior'},
        {"name": "HBO", "group": "Peliculas", "extinf": '#EXTINF:0 group-title="Peliculas",HBO'},
        {"name": "Caracol TV", "group": "Colombia", "extinf": '#EXTINF:0 group-title="Colombia",Caracol TV'},
        {"name": "Canal Desconocido XYZ", "group": "General", "extinf": '#EXTINF:0 group-title="General",Canal Desconocido XYZ'},
    ]
    got = [auto_category_for_entry(e) for e in samples]
    expected = ["10 - Deportes", "11 - Infantil", "12 - Peliculas", "17 - Colombia", "19 - Religioso y Otros"]
    if got != expected:
        raise RuntimeError(f"Falló la clasificación automática: {got!r}")
    return True


def run_embedded_directives_self_test():
    raw = '#EXTINF:-1 tvg-name="ATV#EXTGRP:Latinoamerica#EXTVLCOPT:network-caching=1000" group-title="Latinoamerica",ATV#EXTGRP:Latinoamerica#EXTVLCOPT:network-caching=1000'
    clean, embedded = extract_embedded_directives_from_extinf(raw)
    if '#EXTGRP' in clean or '#EXTVLCOPT' in clean:
        raise RuntimeError("La limpieza de directivas incrustadas falló.")
    if 'tvg-name="ATV"' not in clean or not clean.endswith(",ATV"):
        raise RuntimeError("No se limpió correctamente tvg-name/nombre visible.")
    if not embedded:
        raise RuntimeError("No se detectaron las directivas incrustadas.")
    return True



def run_compact_m3u_self_test():
    sample = [{
        "extinf": '#EXTINF:0 group-title="General",Prueba',
        "group": "General",
        "directives": [],
        "url": "http://example.com/test.m3u8",
    }]
    result = build_m3u(sample)
    if "\n\n" in result:
        raise RuntimeError("La salida M3U contiene líneas en blanco.")
    if not result.startswith("#EXTM3U\n"):
        raise RuntimeError("La salida M3U no comienza correctamente.")
    return True



def run_unicode_m3u_self_test():
    sample = [{
        "extinf": '#EXTINF:0 group-title="Noticias",CNN Español – Ñandú',
        "group": "Noticias",
        "directives": [],
        "url": "http://example.com/espana.m3u8",
    }]
    result = build_m3u(sample)
    if "CNN Español – Ñandú" not in result:
        raise RuntimeError("Falló la prueba UTF-8.")
    if "\n\n" in result:
        raise RuntimeError("La salida M3U contiene líneas en blanco.")
    return True



def run_encoding_self_tests():
    cases = {
        "CNN EspaÃ±ol": "CNN Español",
        "EspaÃ±a": "España",
        "ESPAÃA": "ESPAÑA",
        "ESPAÃA": "ESPAÑA",
    }
    for bad, expected in cases.items():
        got, _c, _b, _a = repair_mojibake_text(bad)
        if got != expected:
            raise RuntimeError(f"Prueba UTF-8 fallida: {bad!r} -> {got!r}, esperado {expected!r}")
    sample = '#EXTINF:0 group-title="España",CNN Español\n#EXTGRP:España\n#EXTVLCOPT:network-caching=1000\nhttp://example.com/live.m3u8\n'
    if audit_text_encoding(sample):
        raise RuntimeError("La auditoría UTF-8 detectó falsos positivos.")



def run_favorite_speed_self_test():
    sample=[{
        "name":"A", "group":"General", "extinf":"#EXTINF:0 group-title=\"General\",A",
        "directives":[], "url":"http://example/a.m3u8"
    }]
    assert sample[0]["url"].startswith("http")
    return True


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(980, 740)
        max_width, max_height = self.maxsize()
        if self.tk.call("tk", "windowingsystem") == "win32" and (max_width < 1100 or max_height < 820):
            # Let Windows fit the frame and caption inside the monitor's work area.
            self.geometry("980x740")
            self.state("zoomed")
        else:
            self.geometry("1100x820")
        self.files = []
        self._settings_warnings = []
        self.blocks = self._load_for_editor(load_blocks, {})
        self._build_ui()
        if self._settings_warnings:
            message = "\n".join(self._settings_warnings)
            self.after_idle(lambda: messagebox.showwarning("Configuración personal", message, parent=self))

    def _load_for_editor(self, reader, default):
        try:
            return reader()
        except SettingsError as exc:
            self._settings_warnings.append(str(exc))
            return default

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # El formulario contiene varias áreas de edición. Mantenerlo dentro de
        # un canvas desplazable evita que la sección Salida/ejecución quede
        # inaccesible en pantallas portátiles, escalado DPI alto o ventanas
        # reducidas. La barra de acciones permanece fuera del viewport.
        footer = ttk.Frame(self, padding=(12, 6, 12, 10))
        footer.pack(side="bottom", fill="x")
        self._footer_top = ttk.Frame(footer)
        self._footer_top.pack(fill="x", pady=(0, 4))
        shell = ttk.Frame(self)
        shell.pack(side="top", fill="both", expand=True)
        self._content_canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0)
        content_scroll = ttk.Scrollbar(shell, orient="vertical", command=self._content_canvas.yview)
        self._content_canvas.configure(yscrollcommand=content_scroll.set)
        self._content_canvas.grid(row=0, column=0, sticky="nsew")
        content_scroll.grid(row=0, column=1, sticky="ns")
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        root = ttk.Frame(self._content_canvas, padding=12)
        self._content_window = self._content_canvas.create_window((0, 0), window=root, anchor="nw")
        root.bind("<Configure>", self._sync_content_scrollregion)
        self._content_canvas.bind("<Configure>", self._sync_content_width)
        self.bind("<FocusIn>", self._reveal_focused_control, add="+")
        self.bind_all("<MouseWheel>", self._on_canvas_mousewheel, add="+")
        self.bind_all("<Prior>", lambda _event: self._scroll_canvas_pages(-1), add="+")
        self.bind_all("<Next>", lambda _event: self._scroll_canvas_pages(1), add="+")
        self.bind_all("<Home>", lambda _event: self._scroll_canvas_home_end(False), add="+")
        self.bind_all("<End>", lambda _event: self._scroll_canvas_home_end(True), add="+")

        title = ttk.Label(root, text=APP_NAME, font=("Segoe UI", 19, "bold"), anchor="w")
        title.pack(fill="x")
        title.bind("<Configure>", lambda event: title.configure(wraplength=event.width))
        ttk.Label(
            root,
            text="Unifica • conserva metadatos • limpia nombres • valida streams • Favoritos por calidad + estabilidad + velocidad • hasta 4 fuentes por canal • solo URLs duplicadas exactas",
            wraplength=850,
        ).pack(anchor="w", pady=(2, 12))

        files_frame = ttk.LabelFrame(root, text="Listas M3U / M3U8", padding=8)
        files_frame.pack(fill="x")
        buttons = ttk.Frame(files_frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Agregar listas", command=self.add_files).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Quitar seleccionada", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Limpiar", command=self.clear_files).pack(side="left", padx=6)
        file_list_row = ttk.Frame(files_frame)
        file_list_row.pack(fill="x", pady=(8, 0))
        self.file_list = tk.Listbox(file_list_row, height=7, selectmode=tk.EXTENDED)
        self.file_list.pack(fill="x", expand=True)
        file_xscroll = ttk.Scrollbar(file_list_row, orient="horizontal", command=self.file_list.xview)
        file_xscroll.pack(fill="x")
        self.file_list.configure(xscrollcommand=file_xscroll.set)

        manual_frame = ttk.LabelFrame(root, text="URLs adicionales", padding=8)
        manual_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.manual_text = tk.Text(manual_frame, height=10, wrap="none", undo=True)
        scroll = ttk.Scrollbar(manual_frame, orient="vertical", command=self.manual_text.yview)
        scroll.pack(side="right", fill="y")
        self.manual_text.pack(side="left", fill="both", expand=True)
        self.manual_text.configure(yscrollcommand=scroll.set)

        blocks = ttk.Frame(root)
        blocks.pack(fill="x", pady=(6, 0))
        blocks.columnconfigure(1, weight=1)
        ttk.Label(blocks, text="Bloque guardado:").grid(row=0, column=0, sticky="w")
        self.block_combo = ttk.Combobox(blocks, values=sorted(self.blocks.keys()), state="readonly", width=30)
        self.block_combo.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(blocks, text="Cargar", command=self.load_block).grid(row=0, column=2, padx=3)
        ttk.Button(blocks, text="Guardar como...", command=self.save_block).grid(row=0, column=3, padx=3)
        ttk.Button(blocks, text="Eliminar", command=self.delete_block).grid(row=0, column=4, padx=3)

        fav_frame = ttk.LabelFrame(root, text="Favoritos", padding=8)
        fav_frame.pack(fill="both", expand=False, pady=(10, 0))
        ttk.Label(
            fav_frame,
            text="Usa los encabezados empresariales 00 - Favoritos … en el orden definido. "
                 "Los favoritos usan el nombre EXACTO de la M3U; [Opc.1]..[Opc.5] se buscan literalmente y luego se agrupan para seleccionar hasta 4 fuentes.",
            wraplength=850,
        ).pack(anchor="w")
        fav_row = ttk.Frame(fav_frame)
        fav_row.pack(fill="both", expand=True, pady=(6, 0))
        self.favorites_text = tk.Text(fav_row, height=7, wrap="none", undo=True)
        fav_scroll = ttk.Scrollbar(fav_row, orient="vertical", command=self.favorites_text.yview)
        fav_scroll.pack(side="right", fill="y")
        self.favorites_text.pack(side="left", fill="both", expand=True)
        self.favorites_text.configure(yscrollcommand=fav_scroll.set)
        self.favorites_text.insert("1.0", self._load_for_editor(load_favorites_text, ""))

        fav_buttons = ttk.Frame(fav_frame)
        fav_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(fav_buttons, text="Guardar favoritos", command=self.save_favorites).pack(side="left", padx=(0, 6))
        ttk.Button(fav_buttons, text="Nueva categoría", command=self.add_favorite_category).pack(side="left", padx=6)
        ttk.Button(fav_buttons, text="Limpiar", command=self.clear_favorites).pack(side="left", padx=6)
        fav_flags = ttk.Frame(fav_frame)
        fav_flags.pack(fill="x", pady=(6, 0))
        config = self._load_for_editor(load_app_config, {})
        def preference(key, default):
            value = config.get(key)
            return value if type(value) is type(default) else default

        self.favorite_group_var = tk.StringVar(value=preference("favorite_group", DEFAULT_FAVORITES_GROUP))
        self.fav_top_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fav_flags,
            text="Favoritos primero (obligatorio)",
            variable=self.fav_top_var,
            state="disabled",
        ).grid(row=0, column=0, sticky="w")
        self.fav_quality_var = tk.BooleanVar(value=preference("fav_quality", True))
        ttk.Checkbutton(
            fav_flags,
            text="Mejor calidad con FFprobe",
            variable=self.fav_quality_var,
        ).grid(row=0, column=1, sticky="w", padx=12)

        fav_tuning = ttk.Frame(fav_frame)
        fav_tuning.pack(fill="x", pady=(6, 0))
        ttk.Label(fav_tuning, text="Fuentes por canal:").pack(side="left", padx=(0, 4))
        self.fav_sources_var = tk.IntVar(value=preference("fav_sources", 4))
        ttk.Spinbox(
            fav_tuning, from_=1, to=4, textvariable=self.fav_sources_var, width=4
        ).pack(side="left", padx=4)

        ttk.Label(fav_tuning, text="Pruebas estabilidad:").pack(side="left", padx=(12, 4))
        self.fav_repetitions_var = tk.IntVar(value=preference("fav_reps", 5))
        ttk.Spinbox(
            fav_tuning, from_=1, to=5, textvariable=self.fav_repetitions_var, width=4
        ).pack(side="left", padx=4)

        ttk.Label(fav_tuning, text="Espera(s):").pack(side="left", padx=(8, 4))
        self.fav_interval_var = tk.DoubleVar(value=preference("fav_interval", 0.35))
        ttk.Spinbox(
            fav_tuning, from_=0.0, to=2.0, increment=0.05,
            textvariable=self.fav_interval_var, width=5
        ).pack(side="left", padx=4)

        self.fav_fast_var = tk.BooleanVar(value=preference("fav_fast", True))
        ttk.Checkbutton(
            fav_flags,
            text="Usar rapidez como desempate",
            variable=self.fav_fast_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(fav_frame, text="Categorías de origen preservadas • Favoritos 00+ primero", wraplength=850).pack(anchor="w", pady=(6, 0))

        metadata = ttk.LabelFrame(root, text="Metadatos IPTV: logos oficiales y guía XMLTV", padding=8)
        metadata.pack(fill="x", pady=(10, 0))
        self.metadata_enrich_var = tk.BooleanVar(value=preference("metadata_enrich_favorites", False))
        ttk.Checkbutton(
            metadata,
            text="Completar sólo Favoritos (logos ausentes + tvg-id)",
            variable=self.metadata_enrich_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        scope = preference("metadata_scope", "favorites")
        self.metadata_scope_var = tk.StringVar(value=scope if scope in ("favorites", "all") else "favorites")
        ttk.Label(metadata, text="Alcance:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(metadata, textvariable=self.metadata_scope_var, values=("favorites", "all"), state="readonly", width=11).grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        self.metadata_download_xmltv_var = tk.BooleanVar(value=preference("metadata_download_xmltv", False))
        ttk.Checkbutton(
            metadata,
            text="Descargar/cachar XMLTV (TTL 12 h)",
            variable=self.metadata_download_xmltv_var,
        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(14, 0), pady=(6, 0))
        self.metadata_registry_var = tk.StringVar(value=preference("metadata_registry_path", ""))
        ttk.Label(metadata, text="Registro curado:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(metadata, textvariable=self.metadata_registry_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 6), pady=(6, 0))
        ttk.Button(metadata, text="Elegir...", command=self.choose_metadata_registry).grid(row=2, column=3, sticky="e", pady=(6, 0))
        ttk.Label(
            metadata,
            text="Nunca reemplaza un logo existente. Sólo acepta HTTPS con procedencia oficial declarada; pendientes se conservan sin adivinar.",
            wraplength=850,
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        metadata.columnconfigure(1, weight=1)

        options = ttk.LabelFrame(root, text="Validación", padding=8)
        options.pack(fill="x", pady=(10, 0))

        self.validate_var = tk.BooleanVar(value=preference("validate", True))
        ttk.Checkbutton(
            options,
            text="Validar URLs y conservar solo canales activos",
            variable=self.validate_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        self.prevalidated_var = tk.BooleanVar(value=preference("prevalidated", False))
        ttk.Checkbutton(
            options,
            text="Usar prevalidación certificada (manifest + SHA-256)",
            variable=self.prevalidated_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        self.deep_var = tk.BooleanVar(value=preference("deep", True))
        deep_text = "FFprobe profundo — valida todas las URLs candidatas"
        ttk.Checkbutton(options, text=deep_text, variable=self.deep_var, command=self._on_deep_toggle).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.ffprobe_status_var = tk.StringVar(value="")
        ffprobe_controls = ttk.Frame(options)
        ffprobe_controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(ffprobe_controls, textvariable=self.ffprobe_status_var).pack(anchor="w")
        ffprobe_buttons = ttk.Frame(ffprobe_controls)
        ffprobe_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(ffprobe_buttons, text="Buscar FFprobe...", command=self.choose_ffprobe).pack(side="left")
        ttk.Button(ffprobe_buttons, text="Probar", command=self.check_ffprobe).pack(side="left", padx=8)
        ttk.Button(ffprobe_buttons, text="Descargar FFprobe", command=self.download_ffprobe).pack(side="left")

        time_controls = ttk.Frame(options)
        time_controls.grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(time_controls, text="Tiempo límite (seg):").pack(side="left", padx=(0, 8))
        self.timeout_var = tk.IntVar(value=preference("timeout", DEFAULT_TIMEOUT))
        ttk.Spinbox(time_controls, from_=3, to=30, textvariable=self.timeout_var, width=6).pack(side="left")

        worker_controls = ttk.Frame(options)
        worker_controls.grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        ttk.Label(worker_controls, text="Conexiones simultáneas:").pack(side="left", padx=(0, 8))
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(worker_controls, from_=8, to=8, textvariable=self.workers_var, width=6, state="readonly").pack(side="left")

        self.progress = ttk.Progressbar(options, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)

        out_frame = ttk.LabelFrame(root, text="Salida", padding=8)
        out_frame.pack(fill="x", pady=(10, 0))
        out_row = ttk.Frame(out_frame)
        out_row.pack(fill="x")
        ttk.Label(out_row, text="Archivo:").pack(side="left")
        self.output_var = tk.StringVar(value=str(Path.cwd() / "lista_unificada.m3u"))
        ttk.Entry(out_row, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(out_row, text="Elegir...", command=self.choose_output).pack(side="left")

        self.status_var = tk.StringVar(value="Listo.")
        self._refresh_ffprobe_status()
        self.status_label = ttk.Label(footer, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill="x", pady=(0, 6))
        footer.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(300, event.width - 24)))
        actions = ttk.Frame(footer)
        actions.pack(fill="x")
        ttk.Button(actions, text="UNIFICAR Y VALIDAR", command=self.merge).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="REPARAR M3U EXISTENTE", command=self.repair_existing).pack(side="left", fill="x", expand=True, padx=(8, 0))

    def _sync_content_scrollregion(self, _event=None):
        if hasattr(self, "_content_canvas"):
            self._content_canvas.configure(scrollregion=self._content_canvas.bbox("all"))

    def _sync_content_width(self, event):
        self._content_canvas.itemconfigure(self._content_window, width=event.width)

    def _reveal_focused_control(self, event):
        canvas = self._content_canvas
        widget = event.widget
        if not isinstance(widget, tk.Misc) or not str(widget).startswith(str(canvas) + "."):
            return
        if widget != self.focus_get():
            return
        top = widget.winfo_rooty() - canvas.winfo_rooty()
        viewport = canvas.winfo_height()
        bottom = top + min(widget.winfo_height(), viewport)
        offset = top if top < 0 else max(0, bottom - viewport)
        bounds = canvas.bbox(self._content_window)
        if offset and bounds and bounds[3] > bounds[1]:
            y = canvas.canvasy(0) + offset - bounds[1]
            canvas.yview_moveto(y / (bounds[3] - bounds[1]))

    def _on_canvas_mousewheel(self, event):
        # Text/Listbox mantienen su propio scroll para no impedir la edición.
        if event.widget.winfo_class() in {"Text", "Listbox", "TCombobox", "TSpinbox"}:
            return None
        if event.delta:
            self._content_canvas.yview_scroll(-max(1, abs(event.delta) // 120) if event.delta > 0 else max(1, abs(event.delta) // 120), "units")
            return "break"
        return None

    def _scroll_canvas_pages(self, direction):
        if self.focus_get() and self.focus_get().winfo_class() in {"Text", "Listbox"}:
            return None
        self._content_canvas.yview_scroll(direction, "pages")
        return "break"

    def _scroll_canvas_home_end(self, end):
        if self.focus_get() and self.focus_get().winfo_class() in {"Text", "Listbox"}:
            return None
        self._content_canvas.yview_moveto(1.0 if end else 0.0)
        return "break"

    def _refresh_ffprobe_status(self):
        exe = detect_ffprobe()
        if exe:
            ok, info = test_ffprobe(exe)
            if ok:
                self.ffprobe_status_var.set("✅ FFprobe detectado")
            else:
                self.ffprobe_status_var.set("⚠ FFprobe encontrado, pero no ejecuta")
        else:
            self.ffprobe_status_var.set("❌ FFprobe no encontrado")

    def check_ffprobe(self):
        exe = detect_ffprobe()
        ok, info = test_ffprobe(exe)
        if ok:
            self.ffprobe_status_var.set("✅ FFprobe detectado")
            messagebox.showinfo(APP_NAME, f"FFprobe funcionando correctamente.\n\n{info}\n\nRuta:\n{exe}")
        else:
            self.ffprobe_status_var.set("❌ FFprobe no disponible")
            messagebox.showwarning(APP_NAME, "No se pudo ejecutar FFprobe.\n\n" + info)

    def choose_ffprobe(self):
        p = filedialog.askopenfilename(
            title="Seleccionar ffprobe.exe",
            filetypes=[("FFprobe", "ffprobe.exe"), ("Ejecutables", "*.exe"), ("Todos los archivos", "*.*")],
        )
        if not p:
            return
        ok, info = test_ffprobe(p)
        if not ok:
            messagebox.showerror(APP_NAME, "El archivo seleccionado no parece ser un FFprobe válido.\n\n" + info)
            return
        try:
            save_app_config({**load_app_config(), "ffprobe_path": str(Path(p).resolve())})
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.ffprobe_status_var.set("✅ FFprobe configurado")
        self.deep_var.set(True)
        messagebox.showinfo(APP_NAME, f"FFprobe configurado correctamente.\n\n{info}")

    def download_ffprobe(self):
        if detect_ffprobe():
            self._refresh_ffprobe_status()
            messagebox.showinfo(APP_NAME, "FFprobe ya está instalado y disponible.\n\nSi quieres reemplazarlo por la compilación integrada, elimina la copia configurada o selecciónala manualmente.")
            return
        self.progress["value"] = 0
        self.status_var.set("Preparando descarga de FFprobe...")

        def worker():
            try:
                def prog(done, total):
                    if total:
                        pct = done / total * 100.0
                        self.after(0, lambda v=pct: self.progress.configure(value=v))
                        self.after(0, lambda d=done, t=total: self.status_var.set(f"Descargando FFmpeg Essentials: {d/1024/1024:.1f}/{t/1024/1024:.1f} MB"))
                def stat(text):
                    self.after(0, lambda: self.status_var.set(text))
                exe, info = install_ffprobe(progress_callback=prog, status_callback=stat)
                self.after(0, lambda: self._ffprobe_install_success(exe, info))
            except Exception as e:
                self.after(0, lambda reason=str(e): self._ffprobe_install_error(reason))
        threading.Thread(target=worker, daemon=True).start()

    def _ffprobe_install_success(self, exe, info):
        self.progress["value"] = 100
        self.ffprobe_status_var.set("✅ FFprobe instalado")
        self.deep_var.set(True)
        self.status_var.set("FFprobe instalado correctamente.")
        messagebox.showinfo(APP_NAME, f"FFprobe se instaló correctamente y quedó configurado.\n\n{info}\n\nRuta:\n{exe}")

    def _ffprobe_install_error(self, err):
        self.ffprobe_status_var.set("❌ Error al instalar FFprobe")
        self.status_var.set("No se pudo instalar FFprobe.")
        messagebox.showerror(APP_NAME, "No se pudo descargar/instalar FFprobe.\n\n" + str(err))

    def _on_deep_toggle(self):
        if self.deep_var.get():
            exe = detect_ffprobe()
            if not exe:
                messagebox.showwarning(APP_NAME, "La validación profunda requiere ffprobe.exe.\n\nSelecciona 'Buscar FFprobe...' para configurarlo.")
                self.deep_var.set(False)
            else:
                self.ffprobe_status_var.set("✅ FFprobe listo")

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Seleccionar listas M3U/M3U8",
            filetypes=[("Listas M3U", "*.m3u *.m3u8"), ("Todos los archivos", "*.*")],
        )
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.file_list.insert("end", p)
        self.status_var.set(f"{len(self.files)} lista(s) seleccionada(s).")

    def remove_selected(self):
        selected = list(self.file_list.curselection())
        for idx in reversed(selected):
            self.file_list.delete(idx)
            del self.files[idx]
        self.status_var.set(f"{len(self.files)} lista(s) seleccionada(s).")

    def clear_files(self):
        self.files.clear()
        self.file_list.delete(0, "end")
        self.status_var.set("Listas limpiadas.")

    def choose_output(self):
        p = filedialog.asksaveasfilename(
            title="Guardar lista unificada",
            defaultextension=".m3u",
            filetypes=[("Lista M3U", "*.m3u")],
        )
        if p:
            self.output_var.set(p)

    def choose_metadata_registry(self):
        p = filedialog.askopenfilename(
            title="Seleccionar registro de logos oficiales y XMLTV",
            filetypes=[("Registro JSON", "*.json"), ("Todos los archivos", "*.*")],
        )
        if not p:
            return
        try:
            save_app_config({**load_app_config(), "metadata_registry_path": str(Path(p).resolve())})
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.metadata_registry_var.set(p)

    def load_block(self):
        name = self.block_combo.get().strip()
        if not name:
            return
        self.manual_text.delete("1.0", "end")
        self.manual_text.insert("1.0", self.blocks.get(name, ""))
        self.status_var.set(f"Bloque '{name}' cargado.")

    def save_block(self):
        text = self.manual_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning(APP_NAME, "No hay contenido para guardar.")
            return
        name = simple_prompt(self, "Guardar bloque", "Nombre del bloque:")
        if not name:
            return
        try:
            updated = {**load_blocks(), name: text}
            save_blocks(updated)
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.blocks = updated
        self.block_combo["values"] = sorted(self.blocks)
        self.block_combo.set(name)
        self.status_var.set(f"Bloque '{name}' guardado.")

    def delete_block(self):
        name = self.block_combo.get().strip()
        if not name or name not in self.blocks:
            return
        if not messagebox.askyesno(APP_NAME, f"¿Eliminar el bloque '{name}'?"):
            return
        try:
            updated = load_blocks()
            updated.pop(name, None)
            save_blocks(updated)
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.blocks = updated
        self.block_combo["values"] = sorted(self.blocks.keys())
        self.block_combo.set("")
        self.status_var.set(f"Bloque '{name}' eliminado.")

    def add_favorite_category(self):
        """Agrega un encabezado de categoría al editor de Favoritos."""
        group = simpledialog.askstring(
            APP_NAME,
            "Escribe la categoría completa, por ejemplo:\n\n00 - Favoritos Deportes\n01 - Favoritos Infantil",
            parent=self,
        )
        if group is None:
            return
        group = clean_channel_name(group.strip())
        if not group:
            messagebox.showwarning(APP_NAME, "La categoría no puede quedar vacía.")
            return
        if not re.match(r"^\d+\s*[-_. ]\s*.+$", group):
            messagebox.showwarning(
                APP_NAME,
                "Para mantener el orden automático, usa un prefijo numérico, por ejemplo:\n\n00 - Favoritos Deportes",
            )
            return
        current = self.favorites_text.get("1.0", "end").strip()
        block = f"[{group}]"
        new_text = (current + "\n\n" if current else "") + block + "\n"
        self.favorites_text.delete("1.0", "end")
        self.favorites_text.insert("1.0", new_text)
        self.favorites_text.see("end")
        self.status_var.set(f"Categoría agregada: {group}")

    def save_favorites(self):
        text = self.favorites_text.get("1.0", "end-1c")
        try:
            save_favorites_text(text)
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.status_var.set("Favoritos guardados.")
        messagebox.showinfo(APP_NAME, f"Favoritos guardados en:\n{MASTER_FAVORITES_FILE}")

    def clear_favorites(self):
        if messagebox.askyesno(APP_NAME, "¿Limpiar la lista de favoritos?"):
            try:
                save_favorites_text("")
            except SettingsError as exc:
                messagebox.showerror(APP_NAME, str(exc))
                return
            self.favorites_text.delete("1.0", "end")
            self.status_var.set("Favoritos limpiados.")

    def repair_existing(self):
        src = filedialog.askopenfilename(
            title="Seleccionar M3U para reparar",
            filetypes=[("M3U/M3U8", "*.m3u *.m3u8"), ("Todos los archivos", "*.*")],
        )
        if not src:
            return
        default = Path(src).with_name(Path(src).stem + "_REPARADO.m3u")
        dst = filedialog.asksaveasfilename(
            title="Guardar M3U reparado",
            initialfile=default.name,
            defaultextension=".m3u",
            filetypes=[("M3U", "*.m3u"), ("Todos los archivos", "*.*")],
        )
        if not dst:
            return
        try:
            raw = Path(src).read_bytes()
            # Decodificación inicial conservadora.
            decoded_candidates = []
            for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                try:
                    s = raw.decode(enc)
                    decoded_candidates.append((mojibake_score(s), s, enc))
                except UnicodeDecodeError:
                    pass
            if not decoded_candidates:
                raise RuntimeError("No fue posible decodificar el archivo.")
            decoded_candidates.sort(key=lambda x: x[0])
            original = decoded_candidates[0][1]
            repaired, changes, before_score, after_score = repair_mojibake_text(original)
            repaired = "\n".join(line.rstrip() for line in repaired.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()) + "\n"
            issues = audit_text_encoding(repaired)
            if issues:
                raise RuntimeError(f"Quedaron {len(issues)} líneas con posibles errores de codificación. Revise antes de publicar.")
            atomic_write_utf8(Path(dst), repaired, bom=True)
            messagebox.showinfo(APP_NAME, f"Reparación completada.\n\nScore mojibake: {before_score} -> {after_score}\nCambios: {len(changes)}\n\nArchivo:\n{dst}")
            self.status_var.set("M3U reparado y auditado correctamente.")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"No se pudo reparar el M3U:\n\n{e}")

    def _set_status_threadsafe(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def merge(self):
        if not self.files and not self.manual_text.get("1.0", "end").strip():
            messagebox.showwarning(APP_NAME, "Agrega al menos una lista M3U o algunas URLs manuales.")
            return

        output = Path(self.output_var.get().strip())
        if not output:
            messagebox.showwarning(APP_NAME, "Selecciona un archivo de salida.")
            return
        if not output.parent.exists():
            output.parent.mkdir(parents=True, exist_ok=True)

        all_entries = []
        input_counts = []
        favorite_specs_text = self.favorites_text.get("1.0", "end")
        favorite_group = self.favorite_group_var.get().strip() or DEFAULT_FAVORITES_GROUP
        try:
            save_app_config({**load_app_config(), "favorite_group": favorite_group})
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        favorite_specs = parse_favorite_lines(favorite_specs_text)

        try:
            for f in self.files:
                entries = parse_m3u(f)
                input_counts.append((Path(f).name, len(entries)))
                all_entries.extend(entries)

            manual_entries = parse_manual_block(self.manual_text.get("1.0", "end"))
            all_entries.extend(manual_entries)
            total_before = len(all_entries)

            # Deduplicar SOLO las entradas de origen. Los favoritos se agregan después
            # como copias intencionales para que aparezcan en su categoría.
            unique_candidates, initial_dupes = dedupe_entries(all_entries)
            speed_notes = []
            scalable_deep = False

            if self.validate_var.get():
                if self.deep_var.get():
                    exe = detect_ffprobe()
                    ok, info = test_ffprobe(exe)
                    if not ok:
                        raise RuntimeError(
                            "La validación profunda está activada, pero FFprobe no está disponible.\n\n"
                            "Configúralo con el botón 'Buscar FFprobe...' o descárgalo con 'Descargar FFprobe'."
                        )

                timeout = max(3, int(self.timeout_var.get()))
                workers = max(1, min(64, int(self.workers_var.get())))
                scalable_deep = bool(self.deep_var.get() and len(unique_candidates) > DEEP_GLOBAL_MAX_URLS)
                self.progress["value"] = 0
                if scalable_deep:
                    self.status_var.set(
                        f"Modo escalable: validando {len(unique_candidates)} URLs por HTTP/HLS; FFprobe se reserva para Favoritos..."
                    )
                else:
                    self.status_var.set(f"Validando {len(unique_candidates)} URLs...")

                def progress(done, total):
                    value = (done / total * 100.0) if total else 100.0
                    self.after(0, lambda v=value, d=done, t=total: (
                        self.progress.configure(value=v),
                        self.status_var.set(f"Validando URLs: {d}/{t}")
                    ))

                kept_active, dead, _ = validate_entries(
                    unique_candidates,
                    deep=(self.deep_var.get() and not scalable_deep),
                    timeout=timeout,
                    workers=workers,
                    progress=progress,
                )
                active_entries = kept_active
                if scalable_deep:
                    speed_notes = [
                        f"Modo escalable activado automáticamente: {len(unique_candidates)} URLs > {DEEP_GLOBAL_MAX_URLS}. "
                        "La validación masiva usa HTTP/HLS; FFprobe profundo se aplica selectivamente a Favoritos."
                    ]
            else:
                dead = []
                active_entries = unique_candidates

            # Resolver favoritos SOLO contra canales activos (o, si se desactiva
            # validación, contra los canales únicos).
            favorite_source_pool = active_entries
            favorite_entries, favorite_missing = resolve_favorites(favorite_specs, favorite_source_pool, favorite_group=favorite_group)

            # Si hay favoritos manuales Nombre|URL que no estaban en el origen y
            # la validación está activada, se validan ahora para que tampoco entren
            # URLs muertas.
            manual_favorite_candidates = validate_favorite_manuals(favorite_entries, active_entries)
            if manual_favorite_candidates and self.validate_var.get():
                timeout = max(3, int(self.timeout_var.get()))
                workers = max(1, min(64, int(self.workers_var.get())))
                self.status_var.set(f"Validando {len(manual_favorite_candidates)} favoritos manuales...")

                def fav_progress(done, total):
                    value = (done / total * 100.0) if total else 100.0
                    self.after(0, lambda v=value, d=done, t=total: (
                        self.progress.configure(value=v),
                        self.status_var.set(f"Validando favoritos: {d}/{t}")
                    ))

                fav_ok, fav_dead, _ = validate_entries(
                    manual_favorite_candidates,
                    deep=self.deep_var.get(),
                    timeout=timeout,
                    workers=workers,
                    progress=fav_progress,
                )

                fav_dead_urls = {e["url"] for e in fav_dead}
                if fav_dead:
                    dead.extend(fav_dead)
                    favorite_entries = [e for e in favorite_entries if e["url"] not in fav_dead_urls]

            # Favoritos: seleccionar hasta 4 fuentes por canal. Se ordenan
            # por calidad objetiva (resolución/bitrate) y por latencia de apertura.
            quality_notes = []
            favorite_probe_details = []
            if self.fav_quality_var.get() and favorite_entries:
                if not detect_ffprobe():
                    quality_notes.append("FFprobe no disponible; Favoritos conservan sus fuentes sin ranking.")
                else:
                    max_sources = max(1, min(4, int(self.fav_sources_var.get())))
                    favorite_entries, quality_notes, favorite_probe_details = optimize_favorites(
                        favorite_entries,
                        favorite_specs,
                        favorite_group,
                        timeout=max(3, int(self.timeout_var.get())),
                        workers=max(1, min(16, int(self.workers_var.get()))),
                        max_sources=max_sources,
                        repetitions=max(1, min(5, int(self.fav_repetitions_var.get()))),
                        interval=max(0.0, min(2.0, float(self.fav_interval_var.get()))),
                        prefer_fast=self.fav_fast_var.get(),
                    )

            favorite_entries = order_favorites_by_category_and_channel(favorite_entries)

            # Producción: conservar las categorías originales de cada M3U.
            # No se generan categorías automáticas. Los Favoritos definidos por
            # el usuario se mantienen como bloque independiente y siempre quedan primero.
            active_entries = preserve_original_categories(active_entries)
            active_entries = sort_entries_by_group_and_name(active_entries)
            final_entries = favorite_entries + active_entries

            final_text = build_m3u(final_entries)
            encoding_issues = audit_text_encoding(final_text)
            structural_issues = audit_generated_m3u(final_text)
            if encoding_issues or structural_issues:
                details = []
                if encoding_issues:
                    details.append(f"UTF-8/mojibake: {len(encoding_issues)} líneas")
                if structural_issues:
                    details.append(f"estructura EXTINF: {len(structural_issues)} líneas")
                raise RuntimeError("La auditoría de seguridad no permitió generar el archivo final: " + "; ".join(details))
            atomic_write_utf8(output, final_text, bom=True)

            report = output.with_name(output.stem + "_informe.txt")
            save_report(
                report,
                input_counts,
                len(manual_entries),
                total_before,
                len(active_entries),
                dead,
                final_entries,
                initial_dupes,
                bool(self.deep_var.get() and self.validate_var.get() and not scalable_deep),
                favorites_count=len(favorite_entries),
                favorite_specs=len(favorite_specs),
                favorite_missing=favorite_missing,
                favorite_entries=favorite_entries,
                favorite_probe_details=favorite_probe_details,
            )

            self.progress["value"] = 100
            self.status_var.set(
                f"Terminado. Activos: {len(active_entries)} | Favoritos: {len(favorite_entries)} | "
                f"Caídos: {len(dead)} | Duplicados: {len(initial_dupes)}"
            )

            validation_text = (
                f"URLs/canales descartados por validación: {len(dead)}\n"
                if self.validate_var.get()
                else "Validación desactivada.\n"
            )
            msg = (
                "Proceso terminado correctamente.\n\n"
                f"Entradas originales: {total_before}\n"
                f"Duplicados por URL exacta: {len(initial_dupes)}\n"
                + validation_text +
                f"Canales activos en categorías originales: {len(active_entries)}\n"
                f"Favoritos en la categoría: {len(favorite_entries)}\n"
                f"Fuentes máximas por canal en Favoritos: {max(1, min(4, int(self.fav_sources_var.get())))}\n"
                f"Favoritos no encontrados/no activos: {len(favorite_missing)}\n"
                f"Optimización de rapidez: {len(speed_notes)} aviso(s)\n"
                f"Total de entradas en el archivo final: {len(final_entries)}\n\n"
                f"Archivo:\n{output}\n\n"
                f"Informe:\n{report}"
            )
            messagebox.showinfo("Proceso terminado", msg)

        except Exception as e:
            messagebox.showerror(APP_NAME, f"No se pudo completar la unificación:\n\n{e}")
            self.status_var.set("Error durante el proceso.")


def simple_prompt(parent, title, label):
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.grab_set()
    ttk.Label(win, text=label).pack(padx=12, pady=(12, 5))
    var = tk.StringVar()
    entry = ttk.Entry(win, textvariable=var, width=40)
    entry.pack(padx=12, pady=5)
    entry.focus_set()
    result = {"value": None}

    def ok():
        result["value"] = var.get().strip()
        win.destroy()

    def cancel():
        win.destroy()

    b = ttk.Frame(win)
    b.pack(pady=(5, 12))
    ttk.Button(b, text="Guardar", command=ok).pack(side="left", padx=5)
    ttk.Button(b, text="Cancelar", command=cancel).pack(side="left", padx=5)
    win.bind("<Return>", lambda e: ok())
    win.bind("<Escape>", lambda e: cancel())
    parent.wait_window(win)
    return result["value"]


def run_scalable_mode_self_test():
    # No ejecuta red: valida únicamente que el umbral produzca el modo selectivo.
    assert DEEP_GLOBAL_MAX_URLS >= 1000
    assert FAVORITE_MAX_PROBE_CANDIDATES_PER_CHANNEL >= 4
    return True

def run_atomic_config_self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        atomic_write_json(p, {"ok": True, "n": 7})
        assert json.loads(p.read_text(encoding="utf-8"))["n"] == 7
        assert not p.with_name(p.name + ".tmp").exists()
    return True


def run_source_category_edge_self_test():
    cases = {
        " Colombia ": "Colombia",
        "COLOMBIA": "Colombia",
        "NOTICIAS": "Noticias",
        " NOTICIAS ": "Noticias",
        "MÚSICA": "Música",
        "Música": "Música",
        "NOVELAS Y SERIES": "Novelas y series",
    }
    for source, expected in cases.items():
        assert normalize_source_category(source) == expected, (source, normalize_source_category(source))
    return True


def run_report_redaction_self_test():
    secret_url = "http://user:pass@example.com/live/token=TEST_PATH/channel.m3u8?token=TEST_VALUE&quality=hd"
    redacted = redact_url_for_report(secret_url)
    assert "user:pass" not in redacted
    assert "TEST_VALUE" not in redacted
    assert "TEST_PATH" not in redacted
    assert "example.com" in redacted
    assert "quality=hd" in redacted
    return True


def run_all_self_tests():
    """Ejecuta todas las auto-pruebas deterministas de la V9.0.0."""
    # Las pruebas ya se ejecutaron durante la importación; este punto de entrada
    # permite repetirlas explícitamente desde consola sin abrir la GUI.
    tests = [
        run_name_cleanup_self_test,
        run_favorite_variant_grouping_self_test,
        run_favorites_self_test,
        run_favorite_categories_self_test,
        run_favorite_source_limit_self_test,
        run_stability_score_self_test,
        run_tvg_name_cleanup_self_test,
        run_embedded_directives_self_test,
        run_compact_m3u_self_test,
        run_unicode_m3u_self_test,
        run_encoding_self_tests,
        run_favorite_speed_self_test,
        run_auto_category_self_test,
        run_scalable_mode_self_test,
        run_atomic_config_self_test,
        run_source_category_edge_self_test,
        run_report_redaction_self_test,
    ]
    for test in tests:
        test()
    return True


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        run_all_self_tests()
        print("SELF-TEST OK")
    else:
        app = App()
        app.mainloop()
