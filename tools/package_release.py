#!/usr/bin/env python3
"""Build a reviewed source ZIP without modifying source or historical evidence."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import tomllib
import zipfile

try:
    from . import release_policy as policy
    from .generate_sbom import render
    from .verify_release import verify
except ImportError:  # python tools/package_release.py
    import release_policy as policy
    from generate_sbom import render
    from verify_release import verify

ROOT = Path(__file__).absolute().parents[1]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def files(root: Path | None = None):
    """Enumerate only approved existing sources; do not walk local data."""
    root = policy.plain_path(ROOT if root is None else root)
    if not root.is_dir():
        raise RuntimeError("La raíz de fuentes no es un directorio.")
    for relative in sorted(policy.approved_paths() - {policy.SBOM_NAME}, key=str.casefold):
        yield policy.source_file(root, relative)


def _publish_zip(staged_zip: Path, output: Path) -> None:
    """Expose a complete file in one step, refusing an existing destination."""
    if os.name == "nt":
        os.rename(staged_zip, output)  # Windows rename never replaces an existing file.
    else:
        os.link(staged_zip, output)  # The staging directory is on the destination volume.


def build_release(root: Path, output_dir: Path) -> dict:
    root = policy.plain_path(root)
    policy.validate_relative(root.name)
    paths = list(files(root))  # All required sources must exist before any output.
    payload = {path.relative_to(root).as_posix():
               policy.read_source(root, path.relative_to(root).as_posix()) for path in paths}
    project = tomllib.loads(payload["pyproject.toml"].decode("utf-8"))["project"]
    version = project["version"]
    if not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version) is None:
        raise RuntimeError("Versión de release inválida.")
    payload[policy.SBOM_NAME] = render(project)
    if set(payload) != policy.approved_paths():
        raise RuntimeError("Fuentes y política de distribución no coinciden.")
    if sum(map(len, payload.values())) > policy.MAX_TOTAL_BYTES:
        raise RuntimeError("Release demasiado grande.")
    rows = [f"{hashlib.sha256(data).hexdigest()}  {name}"
            for name, data in sorted(payload.items(), key=lambda item: item[0].casefold())]
    payload[policy.MANIFEST_NAME] = ("\n".join(rows) + "\n").encode("utf-8")
    output_dir = policy.plain_path(output_dir, allow_missing=True)
    if root.is_relative_to(output_dir) or (
        output_dir.is_relative_to(root) and not output_dir.is_relative_to(root / "artifacts")
    ):
        raise RuntimeError("Use artifacts/ o una salida separada de las fuentes.")
    zip_name = f"M3U_ENTERPRISE_PRODUCTION_READY_OPTIMIZED_V{version.replace('.', '_')}.zip"
    output = output_dir / zip_name
    sbom_output = output_dir / policy.SBOM_NAME
    for target in (output, sbom_output):
        policy.plain_path(target, allow_missing=True)
        if target.exists():
            raise FileExistsError("La salida ya existe; seleccione otra carpeta.")
    output_dir.mkdir(parents=True, exist_ok=True)
    policy.plain_path(output_dir)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output_dir) as staging:
        staged_zip = Path(staging) / "payload.tmp"
        with zipfile.ZipFile(staged_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, data in sorted(payload.items(), key=lambda item: item[0].casefold()):
                info = zipfile.ZipInfo(f"{root.name}/{relative}", (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data, compresslevel=9)
        verify(str(staged_zip))
        zip_digest = sha256(staged_zip)
        sbom_digest = hashlib.sha256(payload[policy.SBOM_NAME]).hexdigest()
        # All versions claim the same sidecar. Only its creator can publish a ZIP.
        # If interrupted here, the destination has a sidecar but no committed ZIP.
        with sbom_output.open("xb") as destination:
            destination.write(payload[policy.SBOM_NAME])
        if sha256(sbom_output) != sbom_digest:
            raise RuntimeError("Entrega incompleta: el SBOM guardado difiere del verificado.")
        _publish_zip(staged_zip, output)
    return {
        "release_zip": str(output),
        "release_sha256": zip_digest,
        "release_sbom": str(sbom_output),
        "release_sbom_sha256": sbom_digest,
        "files_manifested": len(rows),
    }


def main(output_dir: Path | None = None) -> None:
    result = build_release(ROOT, output_dir if output_dir is not None else ROOT / "artifacts" / "releases")
    for key, value in result.items():
        print(f"{key}={value}")
    github_output = os.getenv("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            for key in ("release_zip", "release_sha256", "release_sbom", "release_sbom_sha256"):
                output.write(f"{key}={result[key]}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="New output directory; defaults to artifacts/releases")
    main(parser.parse_args().output_dir)
