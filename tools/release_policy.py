"""Trusted exact source-release paths; never load policy from the ZIP under review."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import re
import stat

MANIFEST_NAME = "SHA256SUMS_OPTIMIZED.txt"
SBOM_NAME = "SBOM.cdx.json"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024

PAYLOAD_PATHS = frozenset({
    ".gitattributes",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".gitignore",
    "channel_metadata_registry.example.json",
    "channel_metadata.py",
    "checkpoint_store.py",
    "CODEOWNERS",
    "COMPATIBILITY.md",
    "docs/ARCHITECTURE.md",
    "docs/CONFIGURATION_AND_RECOVERY.md",
    "docs/EXTERNAL_METADATA_ONBOARDING.md",
    "docs/INSTALLATION_V9_0_2.md",
    "docs/METADATA_ENRICHMENT.md",
    "docs/OPERATIONS_PROFILES.md",
    "docs/OPERATIONS_RUNBOOK.md",
    "docs/PLAYLIST_PUBLICATION.md",
    "docs/PREVALIDATION_CONTRACT.md",
    "docs/RELEASE_PACKAGING.md",
    "docs/USAGE_V9_0_2.md",
    "enterprise_favorites.py",
    "FAVORITOS_CATALOGO_ENTERPRISE.py",
    "favoritos_maestro.txt",
    "favoritos.txt",
    "LICENSE",
    "M3U_PRO_REPARADOR.py",
    "M3U_PRO_V9_ENGINE.py",
    "M3U_PRO_VALIDATOR.py",
    "M3U_PRO/M3U_PRO_V9_ENGINE.py",
    "M3U_PRO/README.md",
    "M3U_PRO/RUN_ENTERPRISE_V9.bat",
    "M3U_PRO/run_self_tests.py",
    "M3U_PRO/SELF_TEST_V9_ENTERPRISE.bat",
    "M3U_PRO/UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py",
    "m3u_repair_rules.json",
    "metrics_store.py",
    "MIGRAR_FAVORITOS_V9.bat",
    "prevalidation_manifest.py",
    "pyproject.toml",
    "README.md",
    "REPARAR_M3U_V9.bat",
    "requirements.txt",
    "requirements-dev.lock",
    "tests/test_dependency_lock.py",
    "RUN_ENTERPRISE_V9.bat",
    "run_self_tests.py",
    "run.py",
    "runtime_profiles.py",
    "runtime_state.py",
    "user_settings.py",
    "tests/test_user_settings.py",
    "network_runtime.py",
    "tests/test_network_runtime.py",
    "tests/test_network_cancellation.py",
    "tests/test_validator_network_lifecycle.py",
    "tests/test_checkpoint_state.py",
    "safe_http.py",
    "SBOM.cdx.json",
    "SECURITY.md",
    "SELF_TEST_V9_ENTERPRISE.bat",
    "tests/test_channel_metadata.py",
    "tests/test_ci_release_contract.py",
    "tests/test_deep_audit.py",
    "tests/test_distribution_contract.py",
    "tests/test_gui_execution_contract.py",
    "tests/test_enterprise_requirements.py",
    "tests/test_external_behavior.py",
    "tests/test_live_campaign_contract.py",
    "tests/test_metadata_discovery.py",
    "tests/test_network_target_policy.py",
    "tests/test_operational_profiles.py",
    "tests/test_playlist_publisher.py",
    "tests/test_prevalidation_contract.py",
    "tests/test_release_boundary.py",
    "tests/test_release_packaging_contract.py",
    "tests/test_safe_http.py",
    "tests/test_url_hardening.py",
    "tests/test_v708_definitive.py",
    "tests/test_v810_regressions.py",
    "tests/test_v900_intensive.py",
    "tests/test_v901_source_scope.py",
    "tests/test_v902_performance_contract.py",
    "tools/discover_metadata_candidates.py",
    "tools/enrich_m3u_metadata.py",
    "tools/generate_sbom.py",
    "tools/Install-ValidationEgressGuard.ps1",
    "tools/metrics_report.py",
    "tools/New-PrivateDataInventory.ps1",
    "tools/package_release.py",
    "tools/publish_playlist.py",
    "tools/release_policy.py",
    "tools/run_live_validation_campaign.py",
    "tools/serve_published_playlist.py",
    "tools/stress_harness.py",
    "tools/verify_release.py",
    "UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py",
    "url_safety.py",
    "VALIDAR_M3U_V9.bat",
})


def validate_relative(name: str) -> tuple[str, ...]:
    """Reject aliases and nonportable names before any path normalization."""
    parts = name.split("/")
    if not name or any(
        part in {"", ".", ".."} or
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None or
        part.endswith((".", " ")) or ntpath.isreserved(part)
        for part in parts
    ):
        raise RuntimeError("Ruta de release no permitida.")
    return tuple(parts)


def approved_paths() -> frozenset[str]:
    paths = PAYLOAD_PATHS
    for name in paths:
        validate_relative(name)
        parts = name.casefold().split("/")
        if (
            any(p in {"local-data", "input", "data", "artifacts", ".git", ".venv",
                      ".runtime", "__pycache__", ".pytest_cache"} for p in parts)
            or any(p == ".env" or p.startswith(".env.") for p in parts)
            or parts[-1] in {"private_data_inventory.json", "channel_metadata_registry.json",
                             "sha256sums.txt", MANIFEST_NAME.casefold()}
            or Path(name).suffix.casefold() in {
                ".m3u", ".m3u8", ".sqlite3", ".sqlite", ".db", ".tmp",
                ".pyc", ".pfx", ".p12", ".key", ".pem",
            }
        ):
            raise RuntimeError("La política incluye una ruta privada o generada.")
    if len({p.casefold() for p in paths}) != len(paths):
        raise RuntimeError("La política contiene alias de ruta.")
    return paths


def plain_path(path: Path, *, allow_missing: bool = False) -> Path:
    """Check every existing ancestor; resolve() alone would hide reparse points."""
    absolute = Path(os.path.abspath(path))
    for current in (*reversed(absolute.parents), absolute):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError("No se permiten enlaces ni puntos de reanálisis.")
        if current != absolute and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Ancestro de release no es un directorio.")
    return absolute


def source_file(root: Path, relative: str) -> Path:
    validate_relative(relative)
    path = plain_path(root / relative)
    if not path.is_relative_to(root):
        raise RuntimeError("Fuente fuera del repositorio.")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("La fuente debe ser un archivo regular sin enlaces.")
    if info.st_size > MAX_FILE_BYTES:
        raise RuntimeError("Fuente demasiado grande para este release.")
    return path


def read_source(root: Path, relative: str) -> bytes:
    path = source_file(root, relative)
    before = path.lstat()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
            before.st_dev, before.st_ino, before.st_nlink
        ):
            raise RuntimeError("La fuente cambió antes de abrirla.")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES or (
        before.st_size, before.st_mtime_ns
    ) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("La fuente cambió durante la lectura.")
    return data

