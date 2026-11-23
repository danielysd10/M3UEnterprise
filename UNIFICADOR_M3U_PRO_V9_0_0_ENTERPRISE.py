# -*- coding: utf-8 -*-
"""UNIFICADOR M3U PRO v9.0.0 ENTERPRISE

Capa de orquestación sobre el motor V6.x estabilizado.
- FFprobe profundo para todas las URLs candidatas cuando se activa validación profunda.
- Preflight HTTP opcional y solo como acelerador; nunca promociona una URL a activa.
- Checkpoint reanudable por huella de entrada/URL.
- Ejecución en segundo plano para que la GUI no se congele.
- Cancelación cooperativa.
- Favoritos inteligentes conservados; categorías de origen normalizadas; auto-categorías desactivadas en producción.
"""
import json, os, re, sys, time, hashlib, threading, traceback, math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import tkinter as tk
from tkinter import ttk, messagebox

# El motor contiene parser, reparación, M3U, favoritos, FFprobe y clasificación heredada de categorías originales.
from M3U_PRO_V9_ENGINE import *  # noqa: F401,F403
from M3U_PRO_V9_ENGINE import StableHostLimiter, _stable_call, _is_transient_failure
import random

from checkpoint_store import CheckpointStore, checkpoint_path, CheckpointError
from runtime_state import default_checkpoint_dir, recover_closed_checkpoint
from network_runtime import (run_operation, _run_owned_process, NetworkCancelled,
                             NetworkDeadlineExceeded, NetworkWorkerError)
from url_safety import URLStatus, apply_url_policy, inspect_url, network_target_allowed, redact_text, redact_url
from prevalidation_manifest import PrevalidationContext, verify_manifest_for_file
from channel_metadata import audit_xmltv_coverage, download_xmltv, enrich_entries, load_registry

APP_NAME = "UNIFICADOR M3U PRO v9.0.16 ENTERPRISE"
VALIDATOR_VERSION = "9.0.16-profiles-sbom-attestation-xmltv-publish-metadata-discovery"
APP_VERSION = "9.0.16-enterprise-production-2026.11"
LEGACY_VALIDATOR_VERSIONS = ("7.0-deep-protocol-v1", "7.0-enterprise-2026.08", "7.0.1-enterprise-2026.08", "7.0.2-enterprise-2026.08")
DEFAULT_CHECKPOINT_BASE_DIR = default_checkpoint_dir()
CHECKPOINT_BASE_DIR = DEFAULT_CHECKPOINT_BASE_DIR
LEGACY_CHECKPOINT_DIR = Path(__file__).resolve().parent
CHECKPOINT_FILE = CHECKPOINT_BASE_DIR / "validacion_checkpoint.json"  # legacy fallback only
CHECKPOINT_TMP = CHECKPOINT_FILE.with_suffix(".tmp")
FFPROBE_READ_SECONDS = 1
DEEP_HTTP_PREFLIGHT = False  # FFprobe es el juez final; evita duplicar conexiones en listas masivas.
CHECKPOINT_BATCH = 50
# A checkpoint is resumable evidence, not permanent proof of remote playback.
# Values are intentionally conservative and are part of the validation identity.
VALIDATION_SUCCESS_CACHE_TTL_SECONDS = 15 * 60
VALIDATION_FAILURE_CACHE_TTL_SECONDS = 60
INPUT_IS_PREVALIDATED = False
PREVALIDATION_MANIFEST_FILENAME = "PREVALIDATION_MANIFEST.json"


def preflight_favorites(text, candidate_entries):
    """Valida y normaliza Favoritos antes de cualquier operación costosa de red.

    No valida disponibilidad. Solo garantiza sintaxis, migración legacy, categorías
    estrictas, orden y que las referencias manuales puedan resolverse contra las
    entradas de entrada. Esto evita descubrir errores de Favoritos después de horas
    de FFprobe.
    """
    specs = parse_favorite_lines(text or "")
    if not specs:
        return specs, []

    # Legacy monolithic markers are never allowed to become a logical category.
    invalid_groups = sorted({spec.get("group", "") for spec in specs
                             if spec.get("group", "") not in FAVORITE_CATEGORY_ORDER})
    if invalid_groups:
        raise RuntimeError(
            "Preflight de Favoritos rechazado antes de la validación de URLs. "
            f"Categorías inválidas: {invalid_groups}. "
            "El legacy 00 - FAVORITOS se migra automáticamente y [HD]/[4K]/[USA] "
            "no se consideran categorías."
        )

    seen = []
    for spec in specs:
        group = spec.get("group")
        if group not in seen:
            seen.append(group)
    expected = [g for g in FAVORITE_CATEGORY_ORDER if g in seen]
    if seen != expected:
        raise RuntimeError(
            "Preflight de Favoritos rechazado: orden de categorías inválido. "
            f"Detectado={seen}; esperado={expected}"
        )

    # Verify references against the RAW input candidates before FFprobe. This is
    # intentionally independent of active/dead status because URLs have not been
    # validated yet. Missing names are reported immediately, avoiding a 4-hour run
    # that could never produce the requested Favorites block.
    by_name = {}
    by_url = set()
    for entry in candidate_entries:
        name = clean_channel_name(entry.get("name", "")).casefold()
        if name:
            by_name.setdefault(name, 0)
            by_name[name] += 1
        url = (entry.get("url") or "").strip()
        if url:
            by_url.add(url)

    missing = []
    for spec in specs:
        if spec.get("type") == "name":
            if spec.get("name", "").casefold() not in by_name:
                missing.append(spec)
        elif spec.get("type") == "url":
            if spec.get("url") not in by_url:
                missing.append(spec)
        elif spec.get("type") == "pair":
            # Explicit name|URL is always structurally valid even when the URL is
            # not present in a source list; it will be treated as a manual Favorite
            # and validated later before publication.
            if not spec.get("name") or not spec.get("url"):
                missing.append(spec)

    return specs, missing


def _fingerprint_entry(e):
    directives = "\n".join(sorted((d or "").strip() for d in e.get("directives", [])))
    payload = "\n".join([
        e.get("url", "").strip(),
        parse_user_agent(e.get("directives", [])),
        parse_referrer(e.get("directives", [])),
        directives,
        VALIDATOR_VERSION,
        "PREVALIDATED=1" if e.get("_prevalidated_verified") else "PREVALIDATED=0",
    ])
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def _job_id(files, manual_text, deep, timeout, prevalidation_signature=""):
    h = hashlib.sha256()
    h.update(VALIDATOR_VERSION.encode())
    h.update(str(bool(deep)).encode())
    h.update(str(int(timeout)).encode())
    h.update(manual_text.encode("utf-8", "ignore"))
    h.update(str(prevalidation_signature or "").encode("utf-8", "ignore"))
    for f in sorted(files):
        try:
            st = Path(f).stat()
            h.update(str(Path(f).resolve()).encode("utf-8", "ignore"))
            h.update(str(st.st_size).encode())
            h.update(str(st.st_mtime_ns).encode())
        except OSError:
            h.update(str(f).encode("utf-8", "ignore"))
    return h.hexdigest()


def _job_id_for_version(files, manual_text, deep, timeout, version):
    h = hashlib.sha256()
    h.update(version.encode())
    h.update(str(bool(deep)).encode())
    h.update(str(int(timeout)).encode())
    h.update(manual_text.encode("utf-8", "ignore"))
    for f in sorted(files):
        try:
            st = Path(f).stat()
            h.update(str(Path(f).resolve()).encode("utf-8", "ignore"))
            h.update(str(st.st_size).encode())
            h.update(str(st.st_mtime_ns).encode())
        except OSError:
            h.update(str(f).encode("utf-8", "ignore"))
    return h.hexdigest()

def _legacy_job_ids(files, manual_text, deep, timeout):
    return {_job_id_for_version(files, manual_text, deep, timeout, v) for v in LEGACY_VALIDATOR_VERSIONS}

def _legacy_fingerprint_entry(e, version):
    directives = "\n".join(sorted((d or "").strip() for d in e.get("directives", [])))
    payload = "\n".join([
        e.get("url", "").strip(),
        parse_user_agent(e.get("directives", [])),
        parse_referrer(e.get("directives", [])),
        directives,
        version,
    ])
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def _checkpoint_paths(job_id):
    path = checkpoint_path(CHECKPOINT_BASE_DIR, job_id).with_suffix(".json")
    return path, path.with_suffix(".tmp")


def _legacy_checkpoint_dir():
    # Explicit state configuration (including test overrides) opts out of
    # discovery beside the installed module. Never mix test and real state.
    if "M3U_STATE_DIR" in os.environ or Path(CHECKPOINT_BASE_DIR) != DEFAULT_CHECKPOINT_BASE_DIR:
        return None
    return LEGACY_CHECKPOINT_DIR


def _read_legacy_checkpoint_json(path, job_id, legacy_job_ids=()):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("job_id") not in {job_id, *legacy_job_ids}:
            raise CheckpointError("Identidad JSON incompatible; se conserva el original.")
        if not isinstance(data.get("results"), dict):
            raise CheckpointError("Resultados JSON incompatibles; se conserva el original.")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise CheckpointError("JSON de recuperación ilegible; se conserva el original.") from exc


def _open_checkpoint_store(job_id, legacy_job_ids=()):
    path = recover_closed_checkpoint(CHECKPOINT_BASE_DIR, job_id, _legacy_checkpoint_dir())
    data, source = None, None
    if not path.exists():
        # Validate JSON before creating SQLite. Schema + import are committed in
        # one transaction, so a parse failure cannot hide recovery on retry.
        for candidate in _legacy_json_candidates(job_id, legacy_job_ids):
            if candidate.exists():
                data = _read_legacy_checkpoint_json(candidate, job_id, legacy_job_ids)
                source = candidate
                break
    return CheckpointStore(CHECKPOINT_BASE_DIR, job_id, initial_legacy_data=data,
                           legacy_json_path=source, allowed_legacy_job_ids=legacy_job_ids)


def _legacy_json_candidates(job_id, legacy_job_ids=()):
    bases = [Path(CHECKPOINT_BASE_DIR)]
    legacy = _legacy_checkpoint_dir()
    if legacy is not None and legacy not in bases:
        bases.append(legacy)
    return [checkpoint_path(base, identity).with_suffix(".json")
            for base in bases for identity in [job_id, *sorted(legacy_job_ids)]]


def _load_checkpoint(job_id, legacy_job_ids=None):
    global CHECKPOINT_FILE, CHECKPOINT_TMP
    CHECKPOINT_FILE, CHECKPOINT_TMP = _checkpoint_paths(job_id)
    sqlite_path = recover_closed_checkpoint(CHECKPOINT_BASE_DIR, job_id, _legacy_checkpoint_dir())
    if sqlite_path.exists():
        with CheckpointStore(CHECKPOINT_BASE_DIR, job_id) as store:
            rows = store.get_all()
            return {
                "job_id": job_id,
                "results": rows,
                "updated": store.get_meta("updated", time.time()),
                "completed": bool(store.get_meta("completed", False)),
                "stage": store.get_meta("stage", "UNKNOWN"),
                "reused_completed_checkpoint": bool(store.get_meta("completed", False)),
            }
    for candidate in _legacy_json_candidates(job_id, legacy_job_ids or ()):
        if not candidate.exists():
            continue
        data = _read_legacy_checkpoint_json(candidate, job_id, legacy_job_ids or ())
        # Reading legacy JSON never rewrites the original or renews its timestamp.
        return {**data, "job_id": job_id, "reused_completed_checkpoint": bool(data.get("completed"))}
    return {"job_id": job_id, "results": {}, "updated": time.time(), "completed": False}


def _save_checkpoint(data):
    global CHECKPOINT_FILE, CHECKPOINT_TMP
    CHECKPOINT_FILE = Path(CHECKPOINT_FILE)
    CHECKPOINT_TMP = Path(CHECKPOINT_TMP)
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CHECKPOINT_TMP.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(CHECKPOINT_TMP, CHECKPOINT_FILE)


def _headers_for_entry(entry):
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
    return ua, ref, headers


def enterprise_ffprobe(entry, timeout=6, *, prevalidated: bool | None = None, cancel_event=None):
    """Prueba multimedia real. Una URL solo queda activa en deep mode si FFprobe detecta pista."""
    exe = ffprobe_path()
    is_prevalidated = bool(entry.get("_prevalidated_verified")) if prevalidated is None else bool(prevalidated)
    inspection = None if is_prevalidated else apply_url_policy(entry)
    url = entry.get("url", "")
    if not is_prevalidated and (inspection is None or not inspection.usable_for_network):
        status = "URL_UNSUPPORTED" if inspection.status == URLStatus.UNSUPPORTED_SCHEME else "URL_ERROR"
        return {"ok": False, "status": status, "reason": inspection.reason, "latency_ms": None, "streams": []}
    dns_started = time.perf_counter()
    def resolve(host, port, **_kwargs):
        return run_operation('dns', {'host': host, 'port': port},
                             deadline=time.monotonic() + 5, cancel_event=cancel_event)
    try:
        permitted, policy_reason = network_target_allowed(url, resolver=resolve)
    except NetworkDeadlineExceeded:
        return {"ok": False, "status": "TIMEOUT", "reason": "DNS timeout > 5s (límite total)",
                "latency_ms": (time.perf_counter() - dns_started) * 1000.0, "streams": []}
    except NetworkWorkerError as exc:
        return {"ok": False, "status": "NETWORK_ERROR", "reason": f"DNS: {redact_text(exc)}",
                "latency_ms": (time.perf_counter() - dns_started) * 1000.0, "streams": []}
    if not permitted:
        return {"ok": False, "status": "URL_ERROR", "reason": policy_reason, "latency_ms": None, "streams": []}
    if not exe:
        return {"ok": False, "status": "FFPROBE_ERROR", "reason": "FFprobe no encontrado", "latency_ms": None, "streams": []}
    ua, ref, headers = _headers_for_entry(entry)
    cmd = [
        exe, "-hide_banner", "-loglevel", "error",
        "-rw_timeout", str(max(3, int(timeout)) * 1_000_000),
        "-probesize", "4M", "-analyzeduration", "2M",
        "-show_entries", "stream=index,codec_type,codec_name,width,height,bit_rate:format=format_name,duration",
        "-of", "json",
    ]
    low = url.lower()
    if low.startswith(("http://", "https://")):
        cmd += ["-allowed_extensions", "ALL", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "2"]
    if ua:
        cmd += ["-user_agent", ua]
    if ref:
        cmd += ["-referer", ref]
    if headers:
        cmd += ["-headers", "\r\n".join(headers) + "\r\n"]
    # Obliga a FFprobe a avanzar algo en el stream, no solo leer metadata superficial.
    cmd += ["-read_intervals", f"%+{FFPROBE_READ_SECONDS}", url]
    process_timeout = max(8, int(timeout) + 4)
    t0 = time.perf_counter()
    try:
        p = _run_owned_process(cmd, input_text=None,
                               deadline=time.monotonic() + process_timeout, cancel_event=cancel_event)
        elapsed = (time.perf_counter() - t0) * 1000.0
        data = {}
        try:
            data = json.loads(p.stdout or "{}")
        except Exception:
            pass
        streams = data.get("streams") or []
        media = [s for s in streams if s.get("codec_type") in ("video", "audio", "subtitle")]
        if p.returncode == 0 and media:
            video = next((s for s in media if s.get("codec_type") == "video"), None) or {}
            audio = next((s for s in media if s.get("codec_type") == "audio"), None) or {}
            return {
                "ok": True, "reason": "FFprobe OK: stream multimedia detectado",
                "latency_ms": elapsed, "streams": media,
                "width": int(float(video.get("width") or 0)), "height": int(float(video.get("height") or 0)),
                "video_bitrate": int(float(video.get("bit_rate") or 0)) if str(video.get("bit_rate") or "").replace(".", "", 1).isdigit() else 0,
                "audio_bitrate": int(float(audio.get("bit_rate") or 0)) if str(audio.get("bit_rate") or "").replace(".", "", 1).isdigit() else 0,
                "video_codec": str(video.get("codec_name") or ""), "audio_codec": str(audio.get("codec_name") or ""),
            }
        err = (p.stderr or "").strip().replace("\n", " ")
        if len(err) > 260: err = err[:260] + "..."
        return {"ok": False, "reason": err or "FFprobe no detectó pistas multimedia", "latency_ms": elapsed, "streams": []}
    except NetworkCancelled:
        raise
    except NetworkDeadlineExceeded:
        elapsed = (time.perf_counter() - t0) * 1000.0
        return {"ok": False, "reason": f"FFprobe timeout > {process_timeout}s (límite del proceso)", "latency_ms": elapsed, "streams": []}
    except Exception as exc:
        return {"ok": False, "reason": f"FFprobe error: {exc}", "latency_ms": None, "streams": []}


def enterprise_http_validate(entry, timeout=4, *, cancel_event=None):
    """Bound the entire HTTP validation, including DNS, redirects and body reads."""
    total_timeout = max(8, int(timeout) + 4)
    try:
        return run_operation('http', {'entry': entry, 'timeout': timeout},
                             deadline=time.monotonic() + total_timeout, cancel_event=cancel_event)
    except NetworkDeadlineExceeded:
        return False, f"HTTP timeout > {total_timeout}s (límite total)", entry.get('url', '')
    except NetworkWorkerError as exc:
        return False, f"Conexión HTTP: {redact_text(exc)}", entry.get('url', '')


def _http_preflight(entry, timeout=4):
    """Acelerador. Solo clasifica un fallo HTTP evidente; nunca promociona a activa."""
    try:
        result = validate_url_http(entry, timeout=timeout)
        return {"ok": bool(result[0]), "status": result[1], "url": result[2]}
    except Exception as exc:
        return {"ok": False, "status": str(exc), "url": entry.get("url", "")}


class UserCancelled(RuntimeError):
    """The user stopped the current merge at a cancellation checkpoint."""


class MergeCancellation(threading.Event):
    """Serialize a user stop with the start of publication for a single merge."""

    def __init__(self):
        super().__init__()
        self._transition_lock = threading.Lock()
        self._publishing = False

    def request_stop(self):
        with self._transition_lock:
            if self._publishing:
                return False
            self.set()
            return True

    def begin_publication(self):
        with self._transition_lock:
            if self.is_set():
                raise UserCancelled("Proceso detenido por el usuario.")
            self._publishing = True


def _validation_status(result):
    if result.get("ok"):
        return "VALID"
    explicit = str(result.get("status") or "").upper().strip()
    if explicit in {"URL_ERROR", "URL_UNSUPPORTED", "URL_INVALID", "URL_REPAIRED"}:
        return explicit
    reason = str(result.get("reason") or result.get("status") or "").lower()
    if "timeout" in reason or "tiempo agotado" in reason or "timed out" in reason:
        return "TIMEOUT"
    if "ffprobe" in reason:
        return "FFPROBE_ERROR"
    if any(token in reason for token in ("conexión", "connection", "network", "dns", "http error", "server returned")):
        return "NETWORK_ERROR"
    if "unexpected" in reason or "worker" in reason:
        return "UNEXPECTED_ERROR"
    if reason and reason != "sin resultado":
        return "INVALID"
    return "UNKNOWN"


def enterprise_validate_all(entries, deep, timeout, workers, progress, cancel_event, job_id):
    """Validate entries with durable SQLite checkpoints and record isolation."""
    for entry in entries:
        if not entry.get("_prevalidated_verified"):
            apply_url_policy(entry)

    by_fp = {}
    unique = []
    for entry in entries:
        fp = _fingerprint_entry(entry)
        if fp not in by_fp:
            by_fp[fp] = entry
            unique.append((fp, entry))

    safe_workers = max(1, min(int(workers), MAX_ENTERPRISE_WORKERS))
    legacy_ids = getattr(enterprise_validate_all, "_legacy_job_ids", set())
    with _open_checkpoint_store(job_id, legacy_ids) as store:
        cache, expired_cache_entries = store.get_many_fresh(
            (fp for fp, _ in unique),
            success_ttl_seconds=VALIDATION_SUCCESS_CACHE_TTL_SECONDS,
            failure_ttl_seconds=VALIDATION_FAILURE_CACHE_TTL_SECONDS,
        )
        checkpoint_hits = len(cache)
        pending = [(fp, e) for fp, e in unique if fp not in cache]
        total = len(unique)
        completed = checkpoint_hits
        if progress:
            progress(completed, total, "Reanudando validación desde checkpoint")

        started = time.perf_counter()
        limiter = StableHostLimiter()
        pending_queue = iter(pending)
        future_map = {}
        buffered = []
        unexpected_errors = 0
        url_errors = 0
        repaired_entries = 0
        for _fp, entry in unique:
            if entry.get("_prevalidated_verified"):
                continue
            inspection = inspect_url(entry.get("url"))
            if not inspection.usable_for_network:
                url_errors += 1
            if inspection.repaired:
                repaired_entries += 1

        def safe_task(item):
            fp, entry = item
            if cancel_event.is_set():
                return fp, None
            try:
                inspection = None if entry.get("_prevalidated_verified") else inspect_url(entry.get("url"))
                if inspection is not None and not inspection.usable_for_network:
                    status = "URL_UNSUPPORTED" if inspection.status == URLStatus.UNSUPPORTED_SCHEME else "URL_ERROR"
                    return fp, {
                        "ok": False, "status": status, "reason": redact_text(inspection.reason),
                        "url": redact_url(entry.get("url", "")), "latency_ms": None,
                    }
                if deep:
                    if entry.get("_prevalidated_verified"):
                        result = enterprise_ffprobe(entry, timeout, prevalidated=True, cancel_event=cancel_event)
                    else:
                        result = enterprise_ffprobe(entry, timeout, cancel_event=cancel_event)
                else:
                    raw = enterprise_http_validate(entry, timeout, cancel_event=cancel_event)
                    result = {"ok": bool(raw[0]), "reason": raw[1], "url": raw[2], "latency_ms": None}
                if not isinstance(result, dict):
                    raise TypeError(f"resultado de validación inesperado: {type(result).__name__}")
                result.setdefault("status", _validation_status(result))
                result["reason"] = redact_text(result.get("reason", ""))
                result["url"] = redact_url(entry.get("url", ""))
                if inspection is not None and inspection.repaired:
                    result["url_repaired"] = True
                    result["url_original_redacted"] = redact_url(inspection.raw)
                return fp, result
            except NetworkCancelled:
                return fp, None
            except Exception as exc:
                return fp, {
                    "ok": False, "status": "UNEXPECTED_ERROR",
                    "reason": f"Error aislado por registro: {redact_text(exc)}",
                    "url": redact_url(entry.get("url", "")), "latency_ms": None,
                }

        pool = ThreadPoolExecutor(max_workers=safe_workers)

        def submit_one(item):
            return pool.submit(lambda: _stable_call(item[1].get("url", ""), limiter, safe_task, item))

        try:
            for _ in range(min(safe_workers * 2, len(pending))):
                try:
                    item = next(pending_queue)
                    future_map[submit_one(item)] = item
                except StopIteration:
                    break
            while future_map:
                if cancel_event.is_set():
                    break
                done_futures, _ = wait(tuple(future_map), timeout=0.1, return_when=FIRST_COMPLETED)
                for fut in done_futures:
                    item = future_map.pop(fut, None)
                    fp = item[0] if item else None
                    result = None
                    try:
                        returned_fp, result = fut.result()
                        fp = returned_fp or fp
                    except Exception as exc:
                        unexpected_errors += 1
                        result = {
                            "ok": False,
                            "status": "UNEXPECTED_ERROR",
                            "reason": f"Excepción aislada del worker: {redact_text(exc)}",
                            "url": (redact_url(item[1].get("url", "")) if item else ""),
                            "latency_ms": None,
                        }
                    if fp is not None and result is not None:
                        result.setdefault("status", _validation_status(result))
                        cache[fp] = result
                        buffered.append((fp, result))
                        completed += 1
                        if len(buffered) >= CHECKPOINT_BATCH:
                            store.put_many(buffered)
                            buffered.clear()
                    if progress:
                        progress(completed, total, "FFprobe profundo" if deep else "Validación HTTP/HLS")
                    try:
                        nxt = next(pending_queue)
                    except StopIteration:
                        nxt = None
                    if nxt is not None and not cancel_event.is_set():
                        future_map[submit_one(nxt)] = nxt
        finally:
            # Do not release the GUI for another run while old workers can still
            # observe its cancellation event. Owned DNS/HTTP/FFprobe children are
            # reaped before their tasks return; queued host waiters recheck cancel.
            pool.shutdown(wait=True, cancel_futures=cancel_event.is_set())

        if buffered:
            store.put_many(buffered)
            buffered.clear()
        complete = not cancel_event.is_set() and all(fp in cache for fp, _ in unique)
        store.mark_stage("VALIDATION_COMPLETE" if complete else "VALIDATION_INCOMPLETE", completed=complete)
        store.set_meta("validated_entries", len(unique))
        store.set_meta("repaired_entries", repaired_entries)
        store.set_meta("url_errors", url_errors)
        store.set_meta("unexpected_errors", unexpected_errors)
        store.set_meta("resume_count", int(store.get_meta("resume_count", 0)) + (1 if checkpoint_hits else 0))

        if cancel_event.is_set():
            raise UserCancelled("Proceso detenido por el usuario. El checkpoint SQLite quedó guardado para reanudar.")

        kept, dead = [], []
        statuses = {}
        latencies = []
        result_map = {}
        for fp, entry in unique:
            result = cache.get(fp) or {"ok": False, "status": "UNEXPECTED_ERROR", "reason": "Sin resultado persistido"}
            result.setdefault("status", _validation_status(result))
            statuses[result["status"]] = statuses.get(result["status"], 0) + 1
            if result.get("latency_ms") is not None:
                try:
                    latencies.append(float(result["latency_ms"]))
                except (TypeError, ValueError):
                    pass
            result_map[entry.get("url", "")] = result
        for entry in entries:
            result = cache.get(_fingerprint_entry(entry)) or {"ok": False, "status": "UNEXPECTED_ERROR", "reason": "Sin resultado"}
            result.setdefault("status", _validation_status(result))
            clone = dict(entry)
            clone["validation_enterprise"] = result
            if result.get("ok"):
                clone["playback_validation"] = "FFPROBE_DEEP" if deep else "HTTP"
                kept.append(clone)
            else:
                clone["playback_validation"] = "FAILED_DEEP" if deep else "FAILED_HTTP"
                dead.append(clone)

        elapsed = time.perf_counter() - started
        enterprise_validate_all.last_metrics = {
            "mode": "FFprobe profundo" if deep else "HTTP/HLS",
            "workers": safe_workers,
            "input_entries": len(entries),
            "unique_fingerprints": len(unique),
            "pending_before_run": len(pending),
            "checkpoint_hits": checkpoint_hits,
            "checkpoint_expired": expired_cache_entries,
            "checkpoint_success_ttl_seconds": VALIDATION_SUCCESS_CACHE_TTL_SECONDS,
            "checkpoint_failure_ttl_seconds": VALIDATION_FAILURE_CACHE_TTL_SECONDS,
            "elapsed_seconds": elapsed,
            "statuses": statuses,
            "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "url_errors": url_errors,
            "repaired_entries": repaired_entries,
            "unexpected_errors": unexpected_errors,
            "checkpoint_path": str(store.path),
            "checkpoint_count": store.count(),
            "resume_count": int(store.get_meta("resume_count", 0)),
        }
        return kept, dead, result_map


def _quarantine_invalid_urls(entries):
    valid = []
    quarantined = []
    for entry in entries:
        if entry.get("_prevalidated_verified"):
            valid.append(entry)
            continue
        try:
            inspection = apply_url_policy(entry)
        except Exception as exc:
            inspection = None
            entry["url_validation"] = {
                "status": "INVALID_SYNTAX",
                "reason": f"Error de inspección de URL: {redact_text(exc)}",
                "raw": redact_text(entry.get("url", "")),
            }
        if inspection is not None and inspection.usable_for_network:
            valid.append(entry)
        else:
            result = {
                "ok": False,
                "status": (
                    "URL_UNSUPPORTED" if inspection is not None and inspection.status == URLStatus.UNSUPPORTED_SCHEME
                    else "URL_ERROR"
                ),
                "reason": (inspection.reason if inspection is not None else entry["url_validation"].get("reason", "URL inválida")),
                "url": entry.get("url", ""),
                "latency_ms": None,
            }
            clone = dict(entry)
            clone["validation_enterprise"] = result
            clone["playback_validation"] = "FAILED_URL"
            quarantined.append(clone)
    return valid, quarantined


def _prevalidation_signature(contexts: dict[str, PrevalidationContext]) -> str:
    rows = []
    for path, ctx in sorted(contexts.items()):
        rows.append("|".join([path, str(ctx.verified), ctx.reason, ctx.clean_sha256, ctx.manifest_version, ctx.validator_version, ctx.repairer_version]))
    return hashlib.sha256("\n".join(rows).encode("utf-8", "ignore")).hexdigest()


def resolve_prevalidation_contexts(files, requested=False):
    contexts = {}
    for raw in files:
        path = Path(raw).expanduser().resolve()
        if requested:
            contexts[str(path)] = verify_manifest_for_file(path, path.parent / PREVALIDATION_MANIFEST_FILENAME, requested=True)
        else:
            contexts[str(path)] = PrevalidationContext(False, False, "NOT_REQUESTED")
    return contexts

class EnterpriseApp(App):
    def __init__(self):
        super().__init__()
        self.cancel_event = MergeCancellation()
        self.running = False
        self.progress_started = None
        self.cancel_btn = ttk.Button(self._footer_top, text="DETENER / REANUDAR DESPUÉS", command=self.cancel_run)
        self.cancel_btn.pack(side="right")
        self.title(APP_NAME)
        self.status_var.set("Listo. V9.0.16 Enterprise: preflight temprano + checkpoint reanudable + publicación atómica.")

    def cancel_run(self):
        if self.running:
            if self.cancel_event.request_stop():
                self.status_var.set("Deteniendo el proceso. Espera a que termine la operación en curso.")
            else:
                self.status_var.set("Publicando la lista y el informe. Espera a que termine.")

    def _set_running(self, running):
        self.running = running
        state = "disabled" if running else "normal"
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def merge(self):
        if self.running:
            return
        if not self.files and not self.manual_text.get("1.0", "end").strip():
            messagebox.showwarning(APP_NAME, "Agrega al menos una lista M3U o algunas URLs manuales.")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showwarning(APP_NAME, "Selecciona un archivo de salida.")
            return
        output = Path(output_text)
        try:
            output_resolved = output.expanduser().resolve()
            if output_resolved.is_dir():
                messagebox.showwarning(APP_NAME, "Selecciona un archivo de salida, no una carpeta.")
                return
            source_resolved = {Path(f).expanduser().resolve() for f in self.files if Path(f).exists()}
            if output_resolved in source_resolved:
                messagebox.showerror(APP_NAME, "El archivo de salida no puede sobrescribir una lista de entrada.")
                return
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"No se pudo validar la ruta de salida:\n\n{exc}")
            return
        try:
            timeout = max(3, int(self.timeout_var.get()))
            fav_sources = max(1, min(4, int(self.fav_sources_var.get())))
            fav_reps = max(1, min(5, int(self.fav_repetitions_var.get() or 5)))
            interval = float(self.fav_interval_var.get())
            if not math.isfinite(interval):
                raise ValueError("La espera debe ser un número finito.")
            fav_interval = max(0.0, min(2.0, interval))
        except (ValueError, TypeError, OverflowError, tk.TclError):
            messagebox.showwarning(APP_NAME, "Revisa los campos numéricos: tiempo límite, fuentes por canal, pruebas de estabilidad y espera.")
            return
        snapshot = {
            "files": list(self.files),
            "manual": self.manual_text.get("1.0", "end"),
            "favorites": self.favorites_text.get("1.0", "end"),
            "favorite_group": self.favorite_group_var.get().strip() or DEFAULT_FAVORITES_GROUP,
            "validate": bool(self.validate_var.get()),
            "deep": bool(self.deep_var.get()),
            "timeout": timeout,
            "workers": DEFAULT_WORKERS,
            "fav_quality": bool(self.fav_quality_var.get()),
            "fav_sources": fav_sources,
            "fav_reps": fav_reps,
            "fav_interval": fav_interval,
            "fav_fast": bool(self.fav_fast_var.get()),
            "fav_top": bool(self.fav_top_var.get()),
            "output": str(output),
            "prevalidated": bool(getattr(self, "prevalidated_var", None) and self.prevalidated_var.get()) if hasattr(self, "prevalidated_var") else False,
            "metadata_enrich": bool(getattr(self, "metadata_enrich_var", None) and self.metadata_enrich_var.get()),
            "metadata_scope": self.metadata_scope_var.get() if hasattr(self, "metadata_scope_var") else "favorites",
            "metadata_registry": self.metadata_registry_var.get().strip() if hasattr(self, "metadata_registry_var") else "",
            "metadata_download_xmltv": bool(getattr(self, "metadata_download_xmltv_var", None) and self.metadata_download_xmltv_var.get()),
        }
        try:
            preferences = load_app_config()
            for key in ("favorite_group", "validate", "deep", "timeout", "prevalidated",
                        "fav_quality", "fav_sources", "fav_reps", "fav_interval", "fav_fast",
                        "metadata_scope", "metadata_download_xmltv"):
                preferences[key] = snapshot[key]
            preferences["metadata_registry_path"] = snapshot["metadata_registry"]
            preferences["metadata_enrich_favorites"] = snapshot["metadata_enrich"]
            save_app_config(preferences)
        except SettingsError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.cancel_event = MergeCancellation()
        self.progress_started = time.perf_counter()
        self._set_running(True)
        threading.Thread(target=self._merge_worker, args=(snapshot,), daemon=True).start()

    def _progress(self, done, total, stage):
        value = 100.0 if not total else min(100.0, done * 100.0 / total)
        elapsed = (time.perf_counter() - self.progress_started) if self.progress_started else 0.0
        eta = ""
        if done > 0 and total > done and elapsed > 0:
            remaining = (total - done) * (elapsed / done)
            eta = f" | ETA aprox.: {int(remaining//60)}m {int(remaining%60)}s"
        self.after(0, lambda: (self.progress.configure(value=value), self.status_var.set(f"{stage}: {done}/{total}{eta}")))

    def _merge_worker(self, c):
        try:
            pipeline_started = time.perf_counter()
            stage_times = {}
            stage_started = time.perf_counter()
            all_entries, input_counts = [], []
            prevalidation_contexts = resolve_prevalidation_contexts(c["files"], requested=bool(c.get("prevalidated", INPUT_IS_PREVALIDATED)))
            effective_prevalidated = {}
            for f in c["files"]:
                ctx = prevalidation_contexts[str(Path(f).expanduser().resolve())]
                effective_prevalidated[str(Path(f).expanduser().resolve())] = ctx.verified
                entries = parse_m3u(f, prevalidated=ctx.verified)
                input_counts.append((Path(f).name, len(entries)))
                all_entries.extend(entries)
            all_entries.extend(parse_manual_block(c["manual"]))
            total_before = len(all_entries)
            prevalidated_files = sum(1 for ctx in prevalidation_contexts.values() if ctx.verified)
            prevalidation_fallbacks = {path: ctx.reason for path, ctx in prevalidation_contexts.items() if not ctx.verified and ctx.reason != "NOT_REQUESTED"}
            stage_times["lectura_parsing"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            unique_candidates, initial_dupes = dedupe_entries(all_entries)
            stage_times["deduplicacion"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            network_candidates, quarantined_urls = _quarantine_invalid_urls(unique_candidates)
            stage_times["url_gate"] = time.perf_counter() - stage_started

            # CRITICAL PRODUCTION GATE: Favoritos are parsed/migrated/validated
            # before any potentially multi-hour network validation.
            favorite_specs, favorite_preflight_missing = preflight_favorites(c["favorites"], network_candidates)
            stage_times["preflight_favoritos"] = time.perf_counter() - stage_started
            self._progress(0, len(network_candidates), "Preflight de Favoritos completado")

            if c["validate"]:
                if c["deep"]:
                    ok, info = test_ffprobe(ffprobe_path())
                    if not ok:
                        raise RuntimeError("FFprobe profundo está activado pero no está disponible. Configúralo o descárgalo.")
                job = _job_id(c["files"], c["manual"], c["deep"], c["timeout"], _prevalidation_signature(prevalidation_contexts))
                enterprise_validate_all._legacy_job_ids = _legacy_job_ids(c["files"], c["manual"], c["deep"], c["timeout"])
                stage_started = time.perf_counter()
                active_entries, dead, _ = enterprise_validate_all(
                    network_candidates, c["deep"], c["timeout"], c["workers"], self._progress, self.cancel_event, job
                )
                dead = list(quarantined_urls) + list(dead)
                stage_times["validacion"] = time.perf_counter() - stage_started
            else:
                dead=list(quarantined_urls); active_entries=network_candidates
                stage_times["validacion"] = 0.0

            if self.cancel_event.is_set():
                raise UserCancelled("Proceso detenido por el usuario.")

            stage_started = time.perf_counter()
            favorites, favorite_missing = resolve_favorites(favorite_specs, active_entries, favorite_group=c["favorite_group"])
            # Carry raw preflight misses into the final report. Any misses caused
            # only by validation failures are kept separately below.
            if favorite_preflight_missing:
                favorite_missing = list(favorite_preflight_missing) + list(favorite_missing)

            # Favoritos siempre pasan por ranking profundo si FFprobe está disponible.
            quality_notes=[]; favorite_details=[]
            if c["fav_quality"] and favorites and ffprobe_path():
                favorites, quality_notes, favorite_details = optimize_favorites(
                    favorites, favorite_specs, c["favorite_group"], timeout=c["timeout"], workers=min(16,c["workers"]),
                    max_sources=c["fav_sources"], repetitions=c["fav_reps"], interval=c["fav_interval"], prefer_fast=c["fav_fast"],
                    cancel_event=self.cancel_event,
                )
            favorites = enforce_favorite_source_limit(favorites, max_sources=4)
            favorites = order_favorites_by_category_and_channel(favorites)
            favorites = synchronize_group_metadata(favorites)
            fav_ok, fav_seen, fav_expected = validate_favorite_order(favorites)
            if not fav_ok:
                raise RuntimeError(f"Orden de Favoritos inválido. Detectado={fav_seen}; esperado={fav_expected}")
            stage_times["favoritos"] = time.perf_counter() - stage_started

            stage_started = time.perf_counter()
            active_entries = preserve_original_categories(active_entries)
            active_entries = sort_entries_by_group_and_name(active_entries)
            active_entries = synchronize_group_metadata(active_entries)
            # En producción los Favoritos definidos por el usuario siempre van primero.
            final_entries = favorites + active_entries
            final_entries = synchronize_group_metadata(final_entries)
            metadata_summary = {"enabled": False}
            metadata_epg_url = ""
            if c.get("metadata_enrich"):
                registry_path = Path(c.get("metadata_registry") or "").expanduser()
                if not registry_path.is_file():
                    raise RuntimeError("El enriquecimiento IPTV requiere seleccionar un registro JSON curado existente.")
                registry = load_registry(registry_path, cancel_event=self.cancel_event)
                scope = c.get("metadata_scope", "favorites")
                final_entries, metadata_summary = enrich_entries(final_entries, registry, scope=scope,
                                                                 cancel_event=self.cancel_event)
                metadata_summary["enabled"] = True
                metadata_epg_url = str(metadata_summary.get("epg_url") or "")
                if c.get("metadata_download_xmltv") and metadata_epg_url:
                    xmltv_path = Path(c["output"]).with_name(Path(c["output"]).stem + "_guia.xml")
                    metadata_summary["xmltv_download"] = download_xmltv(metadata_epg_url, xmltv_path,
                                                                        cancel_event=self.cancel_event)
                    metadata_summary["epg_coverage"] = audit_xmltv_coverage(xmltv_path, final_entries, scope=scope,
                                                                            cancel_event=self.cancel_event)
            final_text = build_m3u(final_entries, epg_url=metadata_epg_url)
            # Última barrera de reparación: solo campos textuales de EXTINF/EXTGRP.
            # No modifica URLs ni tvg-logo.
            final_text, final_repairs = sanitize_generated_m3u_text(final_text)
            encoding_issues = audit_text_encoding(final_text)
            structural_issues = audit_generated_m3u(final_text)
            if encoding_issues or structural_issues:
                details = []
                if encoding_issues:
                    sample = "\n".join(f"  línea {n}: {line[:180]}" for n, line in encoding_issues[:5])
                    details.append(f"Mojibake/UTF-8: {len(encoding_issues)} línea(s)\n{sample}")
                if structural_issues:
                    sample = "\n".join(f"  línea {n}: {kind}" for n, kind, _line in structural_issues[:5])
                    details.append(f"Estructura M3U: {len(structural_issues)} problema(s)\n{sample}")
                if final_repairs:
                    details.append(f"Reparaciones automáticas de texto: {len(final_repairs)}")
                raise RuntimeError("Auditoría final rechazada:\n\n" + "\n\n".join(details))
            stage_times["ensamblado_auditoria"] = time.perf_counter() - stage_started
            stage_started = time.perf_counter()
            self.cancel_event.begin_publication()
            output = Path(c["output"])
            output.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_utf8(output, final_text, bom=True)
            stage_times["escritura_salida"] = time.perf_counter() - stage_started
            prevalidation_requested = bool(c.get("prevalidated", INPUT_IS_PREVALIDATED))
            prevalidation_verified_entries = sum(1 for e in unique_candidates if e.get("_prevalidated_verified"))
            prevalidation_sha256_mismatches = sum(1 for ctx in prevalidation_contexts.values() if ctx.reason == "SHA256_MISMATCH")
            prevalidation_manifest_verified = prevalidated_files
            pipeline_metrics = {
                "prevalidation_requested": prevalidation_requested,
                "prevalidation_verified_files": prevalidated_files,
                "prevalidation_verified_entries": prevalidation_verified_entries,
                "prevalidation_manifest_verified": prevalidation_manifest_verified,
                "prevalidation_sha256_matches": prevalidated_files,
                "prevalidation_sha256_mismatches": prevalidation_sha256_mismatches,
                "prevalidation_url_entries_skipped": prevalidation_verified_entries,
                "prevalidation_fallbacks": prevalidation_fallbacks,
                "prevalidation_fallback_count": len(prevalidation_fallbacks),
                "total_seconds": time.perf_counter() - pipeline_started,
                "stages": stage_times,
                "total_inputs": len(c["files"]),
                "total_entries": total_before,
                "parsed_entries": total_before,
                "normalized_entries": len(unique_candidates),
                "repaired_entries": sum(1 for e in unique_candidates if (e.get("url_validation") or {}).get("status") == "REPAIRED"),
                "input_entries": total_before,
                "unique_candidates": len(unique_candidates),
                "quarantined_entries": len(quarantined_urls),
                "url_errors": len(quarantined_urls),
                "exact_url_duplicates": len(initial_dupes),
                "active_entries": len(active_entries),
                "dead_entries": len(dead),
                "favorites": len(favorites),
                "metadata": metadata_summary,
            }
            report = output.with_name(output.stem + "_informe.txt")
            report_error = None
            try:
                save_report(report, input_counts, len(parse_manual_block(c["manual"])), total_before, len(active_entries), dead, final_entries, initial_dupes,
                            c["deep"], favorites_count=len(favorites), favorite_specs=len(favorite_specs), favorite_missing=favorite_missing,
                            favorite_entries=favorites, favorite_probe_details=favorite_details,
                            validation_metrics=getattr(enterprise_validate_all, "last_metrics", None),
                            pipeline_metrics=pipeline_metrics)
            except Exception as exc:
                report_error = redact_text(exc)
                pipeline_metrics["report_errors"] = 1
                pipeline_metrics["report_error"] = report_error
                fallback = output.with_name(output.stem + "_informe_fallback.txt")
                try:
                    atomic_write_utf8(fallback, "REPORTE FALLBACK\n" + "=" * 78 + "\n" +
                                      f"Reporte completo no disponible: {report_error}\n" +
                                      f"Salida M3U publicada: {output.resolve()}\n" +
                                      f"Entradas: {total_before}\nActivas: {len(active_entries)}\nCaídas: {len(dead)}\n" +
                                      f"Duplicados: {len(initial_dupes)}\n", bom=False)
                    report = fallback
                except Exception:
                    pass
            try:
                with _open_checkpoint_store(_job_id(c["files"], c["manual"], c["deep"], c["timeout"], _prevalidation_signature(prevalidation_contexts))) as store:
                    store.set_meta("published_output", str(output.resolve()))
                    store.set_meta("published_at", time.time())
                    store.mark_stage("PUBLISHED", completed=True)
                    store.set_meta("report_errors", 1 if report_error else 0)
            except Exception:
                pass
            self.after(0, lambda: self._finish_success(output, report, total_before, len(active_entries), len(favorites), len(dead), len(initial_dupes), report_error))
        except UserCancelled as exc:
            self.after(0, lambda message=str(exc): self._finish_cancelled(message))
        except NetworkCancelled:
            self.after(0, lambda: self._finish_cancelled("Proceso detenido por el usuario."))
        except Exception as exc:
            tb = traceback.format_exc()
            self.after(0, lambda e=exc, t=tb: self._finish_error(e, t))

    def _finish_success(self, output, report, total, active, favorites, dead, dupes, report_error=None):
        self._set_running(False); self.progress.configure(value=100)
        self.status_var.set(f"Terminado. Activos: {active} | Favoritos: {favorites} | Caídos: {dead} | Duplicados: {dupes}")
        warning = f"\n\nADVERTENCIA: el reporte completo falló; se generó fallback.\n{report_error}" if report_error else ""
        messagebox.showinfo(APP_NAME, f"Proceso terminado correctamente.\n\nEntradas: {total}\nActivos: {active}\nFavoritos: {favorites}\nCaídos por validación: {dead}\nDuplicados URL exacta: {dupes}{warning}\n\nArchivo:\n{output}\n\nInforme:\n{report}")

    def _finish_cancelled(self, message):
        self._set_running(False)
        self.status_var.set(message)
        messagebox.showinfo(APP_NAME, message, parent=self)

    def _finish_error(self, exc, tb):
        self._set_running(False)
        self.status_var.set("Proceso detenido o terminado con error. Si fue detenido, puedes reanudarlo ejecutando de nuevo.")
        try:
            output = Path(self.output_var.get()).resolve() if self.output_var.get().strip() else Path.cwd() / "salida.m3u"
            log_path = output.with_name(output.stem + "_error.log")
            log_path.write_text(redact_text(tb), encoding="utf-8")
        except Exception:
            log_path = None
        suffix = f"\n\nRegistro técnico:\n{log_path}" if log_path else ""
        messagebox.showerror(APP_NAME, f"No se pudo completar la unificación:\n\n{redact_text(exc)}{suffix}")


def run_enterprise_self_tests():
    run_all_self_tests()
    assert FFPROBE_READ_SECONDS >= 1
    assert CHECKPOINT_BATCH > 0
    assert DEFAULT_WORKERS == 8
    assert MAX_ENTERPRISE_WORKERS == 8
    limiter = StableHostLimiter(max_per_host=2)
    token = limiter.acquire("http://example.com/live.m3u8")
    limiter.release(token)
    # Prueba determinista del fingerprint.
    e={"url":"http://example/live.m3u8","directives":[]}
    assert _fingerprint_entry(e) == _fingerprint_entry(dict(e))
    # Compatibilidad histórica: texto visible mojibake se repara sin tocar URLs.
    sample = '#EXTINF:0 tvg-name="UCL TelevisiÃ³n LATAM" tvg-logo="https://example.com/Ã³logo.png",UCL TelevisiÃ³n LATAM\nhttp://example/live.m3u8\n'
    repaired, changes = sanitize_generated_m3u_text(sample)
    assert "UCL Televisión LATAM" in repaired
    assert "https://example.com/Ã³logo.png" in repaired
    assert audit_text_encoding(repaired) == []
    assert changes
    # La auditoría debe seguir detectando mojibake que no fue reparable.
    bad = '#EXTINF:0 tvg-name="�",Canal\nhttp://example/live.m3u8\n'
    assert audit_text_encoding(bad)
    return True


if __name__ == "__main__":
    from runtime_profiles import apply_profile
    apply_profile()
    if "--self-test" in sys.argv:
        run_enterprise_self_tests(); print("SELF-TEST ENTERPRISE OK")
    else:
        app=EnterpriseApp(); app.mainloop()
