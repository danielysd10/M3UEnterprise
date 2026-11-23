#!/usr/bin/env python3
"""Verify exact release policy, portable ZIP paths and manifest hashes."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import zipfile

try:
    from . import release_policy as policy
except ImportError:
    import release_policy as policy


def _verify_archive(path, *, sbom_sha256: str | None = None) -> dict:
    expected_paths = policy.approved_paths()
    with zipfile.ZipFile(path) as archive:
        files = archive.infolist()
        if len(files) != len(expected_paths) + 1:
            raise RuntimeError("El ZIP no contiene exactamente los archivos aprobados.")
        seen = set()
        prefix = None
        entries = {}
        total = 0
        for entry in files:
            parts = policy.validate_relative(entry.orig_filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (
                entry.filename != entry.orig_filename or entry.is_dir() or
                len(parts) < 2 or mode not in (0, stat.S_IFREG) or
                bool(entry.flag_bits & 1) or
                entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) or
                entry.file_size < 0 or entry.file_size > policy.MAX_FILE_BYTES
            ):
                raise RuntimeError("Entrada ZIP no permitida.")
            alias = entry.filename.casefold()
            if alias in seen:
                raise RuntimeError("El ZIP contiene nombres duplicados o alias.")
            seen.add(alias)
            if prefix is None:
                prefix = parts[0]
            if parts[0] != prefix:
                raise RuntimeError("El ZIP contiene más de una raíz.")
            relative = "/".join(parts[1:])
            if relative not in expected_paths | {policy.MANIFEST_NAME}:
                raise RuntimeError("El ZIP contiene una ruta no aprobada.")
            entries[relative] = entry
            total += entry.file_size
        if total > policy.MAX_TOTAL_BYTES:
            raise RuntimeError("El contenido descomprimido excede el límite.")
        if set(entries) != expected_paths | {policy.MANIFEST_NAME}:
            raise RuntimeError("Faltan archivos aprobados o el manifiesto.")
        manifest = entries[policy.MANIFEST_NAME]
        if manifest.file_size > policy.MAX_MANIFEST_BYTES:
            raise RuntimeError("Manifiesto demasiado grande.")
        try:
            text = archive.read(manifest).decode("utf-8")
        except (UnicodeError, zipfile.BadZipFile) as exc:
            raise RuntimeError("Manifiesto inválido.") from exc
        rows = {}
        aliases = set()
        for line in text.splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
            if not match:
                raise RuntimeError("Fila de manifiesto inválida.")
            digest, relative = match.groups()
            policy.validate_relative(relative)
            if relative not in expected_paths or relative.casefold() in aliases:
                raise RuntimeError("Ruta de manifiesto duplicada o no aprobada.")
            aliases.add(relative.casefold())
            rows[relative] = digest.lower()
        if set(rows) != expected_paths:
            raise RuntimeError("El manifiesto no coincide con el contenido del ZIP.")
        for relative, expected in rows.items():
            value = hashlib.sha256()
            size = 0
            with archive.open(entries[relative]) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > policy.MAX_FILE_BYTES:
                        raise RuntimeError("Entrada descomprimida demasiado grande.")
                    value.update(chunk)
            if size != entries[relative].file_size or value.hexdigest() != expected:
                raise RuntimeError(f"Hash inválido: {relative}")
        if sbom_sha256 is not None and sbom_sha256 != rows.get(policy.SBOM_NAME):
            raise RuntimeError("El SHA-256 del SBOM externo difiere del incluido en el ZIP.")
        return {"entries": len(files), "manifest_rows": len(rows)}


def verify(path: str | Path, *, sbom_path: str | Path | None = None) -> dict:
    """Verify a standalone ZIP or verify both published artifacts together."""
    if sbom_path is None:
        return _verify_archive(path)

    sidecar = Path(sbom_path).absolute()
    try:
        zip_path = policy.plain_path(Path(path))
        if not stat.S_ISREG(zip_path.lstat().st_mode):
            raise RuntimeError("La ruta del ZIP debe ser un archivo regular.")
        with zip_path.open("rb") as zip_stream:
            before = os.fstat(zip_stream.fileno())
            sbom = policy.read_source(sidecar.parent, sidecar.name)
            if not sbom:
                raise RuntimeError("Entrega incompleta: el SBOM externo está vacío.")
            sbom_sha256 = hashlib.sha256(sbom).hexdigest()
            result = _verify_archive(zip_stream, sbom_sha256=sbom_sha256)
            zip_stream.seek(0)
            zip_sha256 = hashlib.file_digest(zip_stream, "sha256").hexdigest()
            after = os.fstat(zip_stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError("El ZIP cambió durante la verificación.")
            return {**result, "release_sha256": zip_sha256, "release_sbom_sha256": sbom_sha256}
    except FileNotFoundError as exc:
        raise RuntimeError("Entrega incompleta: falta el ZIP o el SBOM externo.") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True)
    parser.add_argument("--sbom", help="Comprueba el SBOM externo junto con el ZIP.")
    args = parser.parse_args()
    try:
        print(verify(args.zip, sbom_path=args.sbom))
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"{exc}\n")
