from __future__ import annotations

import json
import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "PREVALIDATION_MANIFEST.json"
MANIFEST_VERSION = "1.0"
CONTRACT_NAME = "M3U_PREVALIDATION"
COMPATIBLE_VALIDATOR_VERSIONS = {"10.1.0"}
COMPATIBLE_REPAIRER_VERSIONS = {"10.1.0"}
ACCEPTED_STATUSES = {"PASS", "PASS_WITH_WARNINGS", "PASS_PARTIAL", "PARTIAL"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _relative_safe(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.name


def _counts_from_validation(validation: dict[str, Any]) -> dict[str, int]:
    counts = validation.get("url_counts", {}) or {}
    return {str(k): int(v or 0) for k, v in counts.items()}


def build_file_certificate(*, source: Path, clean: Path, repair_result: dict[str, Any]) -> dict[str, Any]:
    validation_counts = _counts_from_validation(repair_result.get("validation", {}) if isinstance(repair_result.get("validation"), dict) else {})
    changes = repair_result.get("changes", []) or []
    repaired_urls = sum(1 for row in changes if row.get("action") == "repair_url")
    excluded = int(repair_result.get("excluded_entries", 0) or 0)
    quarantine = int(repair_result.get("quarantined_entries", excluded) or 0)
    return {
        "source_file": source.name,
        "clean_file": clean.name,
        "source_sha256": repair_result.get("source_sha256", sha256_file(source)),
        "clean_sha256": repair_result.get("output_sha256", sha256_file(clean)),
        "original_entries": int(repair_result.get("input_entries", 0) or 0),
        "final_entries": int(repair_result.get("published_entries", 0) or 0),
        "valid": int(validation_counts.get("VALID", 0)),
        "repairable": int(validation_counts.get("REPAIRABLE", 0)),
        "repaired": int(repaired_urls),
        "invalid": int(validation_counts.get("INVALID", 0)),
        "unsupported": int(validation_counts.get("UNSUPPORTED", 0)),
        "excluded": excluded,
        "quarantined": quarantine,
        "processing_status": str(repair_result.get("status", "FAILED")),
        "validation_status": str(repair_result.get("validation_status", "FAIL")),
    }


def write_manifest(output_dir: Path, *, validator_version: str, repairer_version: str, files: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> Path:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "contract": CONTRACT_NAME,
        "created_at": _utc_now(),
        "validator": {"name": "M3U_PRO_VALIDATOR", "version": validator_version},
        "repairer": {"name": "M3U_PRO_REPARADOR", "version": repairer_version},
        "files": files,
        "summary": summary or {
            "files": len(files),
            "source_entries": sum(int(row.get("original_entries", 0)) for row in files),
            "clean_entries": sum(int(row.get("final_entries", 0)) for row in files),
            "status": (
                "PASS"
                if all(row.get("processing_status") == "PASS" for row in files)
                else "PASS_WITH_WARNINGS"
                if all(row.get("processing_status") in ACCEPTED_STATUSES for row in files)
                else "PARTIAL"
            ),
        },
    }
    path = output_dir / MANIFEST_FILENAME
    _atomic_write_json(path, payload)
    return path


@dataclass(frozen=True)
class PrevalidationContext:
    requested: bool
    verified: bool
    reason: str
    manifest_path: str = ""
    manifest_version: str = ""
    validator_version: str = ""
    repairer_version: str = ""
    clean_sha256: str = ""


def _invalid(reason: str, *, requested: bool, manifest_path: Path | None = None) -> PrevalidationContext:
    return PrevalidationContext(requested=requested, verified=False, reason=reason, manifest_path=str(manifest_path or ""))


def verify_manifest_for_file(path: Path, manifest_path: Path | None = None, *, requested: bool = True) -> PrevalidationContext:
    path = Path(path).resolve()
    manifest_path = Path(manifest_path).resolve() if manifest_path else path.parent / MANIFEST_FILENAME
    if not manifest_path.exists():
        return _invalid("MANIFEST_MISSING", requested=requested, manifest_path=manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return _invalid("MANIFEST_CORRUPT", requested=requested, manifest_path=manifest_path)

    if payload.get("manifest_version") != MANIFEST_VERSION or payload.get("contract") != CONTRACT_NAME:
        return _invalid("MANIFEST_VERSION_OR_CONTRACT_UNSUPPORTED", requested=requested, manifest_path=manifest_path)
    validator_version = str((payload.get("validator") or {}).get("version") or "")
    repairer_version = str((payload.get("repairer") or {}).get("version") or "")
    if validator_version not in COMPATIBLE_VALIDATOR_VERSIONS:
        return _invalid("VALIDATOR_VERSION_UNSUPPORTED", requested=requested, manifest_path=manifest_path)
    if repairer_version not in COMPATIBLE_REPAIRER_VERSIONS:
        return _invalid("REPAIRER_VERSION_UNSUPPORTED", requested=requested, manifest_path=manifest_path)

    target_name = path.name.casefold()
    candidates = [row for row in payload.get("files", []) if isinstance(row, dict) and str(row.get("clean_file", "")).casefold() == target_name]
    if len(candidates) != 1:
        return _invalid("FILE_NOT_UNIQUELY_CERTIFIED", requested=requested, manifest_path=manifest_path)
    row = candidates[0]
    status = str(row.get("processing_status") or "").upper()
    if status not in ACCEPTED_STATUSES:
        return _invalid(f"PROCESSING_STATUS_{status or 'UNKNOWN'}", requested=requested, manifest_path=manifest_path)
    expected = str(row.get("clean_sha256") or "").lower()
    if not re_hex_sha256(expected):
        return _invalid("CLEAN_SHA256_MISSING_OR_INVALID", requested=requested, manifest_path=manifest_path)
    try:
        actual = sha256_file(path).lower()
    except OSError:
        return _invalid("CLEAN_FILE_UNREADABLE", requested=requested, manifest_path=manifest_path)
    if actual != expected:
        return PrevalidationContext(requested=requested, verified=False, reason="SHA256_MISMATCH", manifest_path=str(manifest_path), manifest_version=MANIFEST_VERSION, validator_version=validator_version, repairer_version=repairer_version, clean_sha256=actual)
    return PrevalidationContext(requested=requested, verified=True, reason="VERIFIED", manifest_path=str(manifest_path), manifest_version=MANIFEST_VERSION, validator_version=validator_version, repairer_version=repairer_version, clean_sha256=actual)


def re_hex_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
