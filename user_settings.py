"""Personal settings, read-only legacy defaults and explicit, non-replacing import."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from enterprise_favorites import FAVORITES_FILENAME, MASTER_FILENAME, parse_catalog, render_catalog

APP_CONFIG_NAME = "unificador_m3u_config.json"
BLOCKS_NAME = "bloques_urls.json"


class SettingsError(RuntimeError):
    """Safe to show in a dialog or CLI. Messages do not contain user data."""


def default_config_dir(*, environ=None, platform=None, user_home=None) -> Path:
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if user_home is None else Path(user_home)
    if "M3U_CONFIG_DIR" in environ:
        path = Path(environ["M3U_CONFIG_DIR"])
        if not path.is_absolute():
            raise SettingsError("M3U_CONFIG_DIR debe ser una ruta absoluta no vacía.")
        return path
    if platform == "win32":
        path = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "M3UEnterprise" / "config"
    elif platform == "darwin":
        path = home / "Library" / "Application Support" / "M3UEnterprise" / "config"
    else:
        path = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "m3u-enterprise"
    if not path.is_absolute():
        raise SettingsError("La carpeta de configuración por usuario debe ser absoluta.")
    return path


def _plain(path):
    # Treat missing files differently from dangling links. A redirected leaf
    # must not become a source of credentials or an accidental write target.
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise SettingsError("La ruta de configuración contiene un enlace. Elija una carpeta directa.")


def _read(path):
    try:
        _plain(path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise SettingsError(f"{path.name} debe ser un archivo regular.")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise SettingsError(f"No se pudo leer {path.name}. Compruebe sus permisos.") from None


def _decode(data, name):
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        raise SettingsError(f"{name} debe estar codificado en UTF-8. Se conservó el archivo.") from None


def _mapping(data, name):
    try:
        value = json.loads(_decode(data, name))
    except (ValueError, RecursionError):
        raise SettingsError(f"{name} contiene JSON inválido. Se conservó el archivo.") from None
    if not isinstance(value, dict):
        raise SettingsError(f"{name} debe contener un objeto JSON. Se conservó el archivo.")
    return value


def load_mapping(path: Path, legacy_dir: Path | None):
    data = _read(path)
    if data is None and legacy_dir is not None:
        data = _read(legacy_dir / path.name)
    return {} if data is None else _mapping(data, path.name)


def _catalog_source(directory, *, display_flat=False):
    """Read a former catalog pair. Only display requests may format a flat list."""
    master = _read(directory / MASTER_FILENAME)
    old = _read(directory / FAVORITES_FILENAME)
    master_text = _decode(master, MASTER_FILENAME) if master is not None else None
    old_text = _decode(old, FAVORITES_FILENAME) if old is not None else None
    if master is not None:
        # Parsing drops user comments and reorganizes categories. Only BOM and
        # platform line endings can differ without changing the selected text.
        conflict = old_text is not None and master_text.replace("\r\n", "\n") != old_text.replace("\r\n", "\n")
        return master, conflict
    if old_text is None:
        return None, False
    # Preserve annotated or sectioned catalogs verbatim. Legacy flat lists
    # used to acquire the standard categories on first GUI load.
    if display_flat and old_text.strip() and not any(line.lstrip().startswith(("#", "[")) for line in old_text.splitlines()):
        return render_catalog(parse_catalog(old_text)[0]).encode("utf-8"), False
    return old, False


def load_catalog(directory: Path, legacy_dir: Path | None):
    # A saved empty master is an intentional selection and beats all defaults.
    master = _read(directory / MASTER_FILENAME)
    if master is not None:
        return _decode(master, MASTER_FILENAME)
    old, _ = _catalog_source(directory, display_flat=True)
    if old is not None:
        return _decode(old, FAVORITES_FILENAME)
    if legacy_dir is not None:
        data, conflict = _catalog_source(legacy_dir, display_flat=True)
        if conflict:
            raise SettingsError("Los dos catálogos de origen difieren. Revise ambos antes de importar "
                                "o guarde una selección personal desde el editor.")
        if data is not None:
            return _decode(data, MASTER_FILENAME)
    return ""


def _publish(path, data, *, exclusive):
    """Stage complete bytes in a unique sibling, then publish atomically."""
    staged = None
    try:
        _plain(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".m3u-settings-",
                                         suffix=".tmp", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(staged, path)
            except FileExistsError:
                return False
        else:
            os.replace(staged, path)
        return True
    except OSError:
        raise SettingsError(f"No se pudo guardar {path.name}. Compruebe la carpeta y sus permisos.") from None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def write_text(path, text):
    _publish(Path(path), text.encode("utf-8"), exclusive=False)


def import_legacy_settings(source: Path, destination: Path) -> dict[str, str]:
    """Recover each absent file once. Retrying a partial import is safe."""
    source, destination = Path(source).absolute(), Path(destination).absolute()
    _plain(source)
    _plain(destination)
    if not source.is_dir() or source.resolve() == destination.resolve():
        raise SettingsError("Seleccione una carpeta de origen existente y distinta del destino.")
    payloads = {}
    outcomes = {}
    for name in (APP_CONFIG_NAME, BLOCKS_NAME):
        data = _read(source / name)
        if data is not None:
            _mapping(data, name)
            payloads[name] = data
    catalog, conflict = _catalog_source(source)
    if conflict:
        outcomes[MASTER_FILENAME] = "conflict"
    elif catalog is not None:
        payloads[MASTER_FILENAME] = catalog
    for name, content in payloads.items():
        path = destination / name
        existing = _read(path)
        if name == MASTER_FILENAME and existing is None:
            # An existing personal flat catalog is authoritative as well.
            existing, _ = _catalog_source(destination)
        if existing is None and _publish(path, content, exclusive=True):
            outcomes[name] = "copied"
        else:
            existing = existing if existing is not None else _read(path)
            outcomes[name] = "unchanged" if existing == content else "conflict"
    return outcomes
