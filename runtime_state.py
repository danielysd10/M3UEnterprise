"""Per-user state locations and conservative, non-destructive legacy recovery."""
from __future__ import annotations

import hashlib
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from checkpoint_store import CheckpointError, checkpoint_path, validate_checkpoint, checkpoint_lock


def default_checkpoint_dir(*, environ=None, platform=None, user_home=None) -> Path:
    """Resolve configuration only: importing the application never creates state."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    user_home = Path.home() if user_home is None else Path(user_home)
    if "M3U_STATE_DIR" in environ:
        override = environ["M3U_STATE_DIR"]
        if not override or not Path(override).is_absolute():
            raise CheckpointError("M3U_STATE_DIR debe ser una ruta absoluta no vacía.")
        return Path(override) / "checkpoints"
    if platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or user_home / "AppData" / "Local")
        product = base / "M3UEnterprise" / "state"
    elif platform == "darwin":
        product = user_home / "Library" / "Application Support" / "M3UEnterprise" / "state"
    else:
        base = Path(environ.get("XDG_STATE_HOME") or user_home / ".local" / "state")
        product = base / "m3u-enterprise"
    if not product.is_absolute():
        raise CheckpointError("La carpeta de estado por usuario debe ser absoluta.")
    return product / "checkpoints"


def _plain(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise CheckpointError("La recuperación no admite enlaces en sus rutas.")


def _sidecars(path: Path) -> bool:
    return any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def recover_closed_checkpoint(base_dir: Path, job_id: str, legacy_dir: Path | None) -> Path:
    """Copy only the same job's closed DB. Never overwrite an existing target.

    A complete validated snapshot is published exclusively with a same-volume
    hard link, then its temporary name is removed. Concurrent recoveries choose
    the first existing destination without replacing it. Legacy WAL is refused;
    stop the old application cleanly before retrying.
    """
    destination = checkpoint_path(base_dir, job_id)
    _plain(destination)
    if destination.exists() or legacy_dir is None:
        return destination
    source = checkpoint_path(legacy_dir, job_id)
    _plain(source)
    if not source.exists() or source.absolute() == destination.absolute():
        return destination
    with checkpoint_lock(destination):
        if destination.exists():
            return destination
        return _copy_closed_checkpoint(source, destination, job_id)


def _copy_closed_checkpoint(source: Path, destination: Path, job_id: str) -> Path:
    if not source.is_file() or _sidecars(source):
        raise CheckpointError("Cierre la aplicación anterior: hay estado SQLite activo o no regular.")
    with source.open("rb") as stream:
        before = hashlib.file_digest(stream, "sha256").hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".checkpoint-recovery-", dir=destination.parent) as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        shutil.copyfile(source, snapshot)
        with source.open("rb") as stream:
            after = hashlib.file_digest(stream, "sha256").hexdigest()
        with snapshot.open("rb") as stream:
            copied = hashlib.file_digest(stream, "sha256").hexdigest()
        if before != after or copied != before or _sidecars(source):
            raise CheckpointError("El checkpoint anterior cambió; recuperación cancelada.")
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            validate_checkpoint(connection, job_id)
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise CheckpointError("Checkpoint anterior corrupto; se conserva sin reemplazar.")
        try:
            os.link(snapshot, destination)  # Atomic, exclusive on Windows and POSIX.
        except FileExistsError:
            pass  # Another process owns the destination; its state wins.
        except OSError as exc:
            raise CheckpointError("No fue posible publicar la recuperación sin sobrescribir.") from exc
    return destination

