"""Exercise the installed wheel, never imports from the working checkout.

The test interpreter needs the project's locked build/install tools. Building and
installation use only local files; the caller's pip manages a pip-free venv.
No application data or playlists from the checkout are copied into the fixture.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRY_MODULES = (
    "run",
    "FAVORITOS_CATALOGO_ENTERPRISE",
    "M3U_PRO_REPARADOR",
    "M3U_PRO_VALIDATOR",
    "UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE",
    "prevalidation_manifest",
    "runtime_state",
    "network_runtime",
)
NETWORK_GUARD = """
import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'socket.gethostbyname', 'socket.gethostbyaddr'}:
        raise AssertionError('Distribution contract must not access the network')
sys.addaudithook(deny_network)
"""


def _run(python: Path, *args: str, cwd: Path, timeout: int = 120):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(
        M3U_CONFIG_DIR=str(cwd / 'test-user-config'),
        M3U_STATE_DIR=str(cwd / 'test-user-state'),
        PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1",
        PIP_NO_INPUT="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_NO_CACHE_DIR="1",
    )
    result = subprocess.run(
        [str(python), "-I", "-B", "-X", "utf8", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"Isolated command failed ({result.returncode}): {args!r}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory):
    temporary = tmp_path_factory.mktemp("installed-distribution")
    assert not temporary.resolve().is_relative_to(ROOT), (
        "Distribution tests require a pytest temporary directory outside the checkout"
    )
    source = temporary / "source"
    source.mkdir()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    modules = set(config["tool"]["setuptools"]["py-modules"]) | set(ENTRY_MODULES)
    for module in modules:
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module), module
        shutil.copy2(ROOT / f"{module}.py", source / f"{module}.py")
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copy2(ROOT / name, source / name)

    wheels = temporary / "wheels"
    wheels.mkdir()
    _run(
        Path(sys.executable),
        "-c",
        "import sys; from setuptools.build_meta import build_wheel; "
        "build_wheel(sys.argv[1])",
        str(wheels),
        cwd=source,
    )
    artifacts = list(wheels.glob("*.whl"))
    assert len(artifacts) == 1

    environment = temporary / "venv"
    _run(Path(sys.executable), "-m", "venv", "--without-pip", str(environment), cwd=temporary)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    outside = temporary / "consumer"
    outside.mkdir()
    _run(
        Path(sys.executable), "-m", "pip", "--python", str(python),
        "install", "--no-index", "--no-deps",
        "--no-compile", str(artifacts[0]), cwd=outside,
    )
    # Leaving only a renamed source tree also catches accidental relative cwd use.
    source.rename(temporary / "unused-source")
    return python, outside, environment


def _cli(installed_wheel, module: str, *arguments: str):
    python, outside, _ = installed_wheel
    return _run(
        python,
        "-c",
        NETWORK_GUARD + "\nimport runpy\n"
        "module = sys.argv[1]\nsys.argv = sys.argv[1:]\n"
        "runpy.run_module(module, run_name='__main__', alter_sys=True)\n",
        module,
        *arguments,
        cwd=outside,
    )


def test_installed_entry_imports_resolve_inside_the_clean_venv(installed_wheel):
    python, outside, environment = installed_wheel
    code = NETWORK_GUARD + """
import importlib, json
from importlib import metadata
from pathlib import Path
modules = json.loads(sys.argv[1])
environment = Path(sys.argv[2]).resolve()
assert {dist.metadata['Name'] for dist in metadata.distributions()} == {'m3u-enterprise-hardened'}, 'Runtime installation must not rely on development tools'
for name in modules:
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(environment), name
import tkinter
assert tkinter._default_root is None, 'Import must not create a GUI'
import M3U_PRO_V9_ENGINE as engine
assert not engine.REPAIR_RULES_FILE.exists()
assert engine.load_repair_rules() == engine.DEFAULT_REPAIR_RULES
assert not engine.FAVORITES_FILE.exists()
assert not engine.MASTER_FAVORITES_FILE.exists()
import os
assert not Path(os.environ['M3U_CONFIG_DIR']).exists(), 'Import must not create personal configuration'
"""
    _run(python, "-c", code, json.dumps(ENTRY_MODULES), str(environment), cwd=outside)


@pytest.mark.parametrize(
    "module", ("M3U_PRO_REPARADOR", "M3U_PRO_VALIDATOR", "FAVORITOS_CATALOGO_ENTERPRISE")
)
def test_installed_cli_help_runs_without_checkout_or_gui(installed_wheel, module):
    result = _cli(installed_wheel, module, "--help")
    assert "--help" in result.stdout


def test_installed_repairer_publishes_a_verifiable_prevalidation_manifest(installed_wheel):
    python, outside, _ = installed_wheel
    inputs = outside / "input"
    inputs.mkdir()
    source = inputs / "synthetic.m3u"
    source.write_text(
        '#EXTM3U\n#EXTINF:0 group-title="General",Synthetic\n'
        "https://example.invalid/live/1.ts\n",
        encoding="utf-8",
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = outside / "repaired"
    report = outside / "repair-summary.json"
    _cli(
        installed_wheel, "M3U_PRO_REPARADOR", str(source),
        "--output-dir", str(output), "--no-backup", "--report", str(report),
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["successful_files"] == 1
    assert (output / "PREVALIDATION_MANIFEST.json").is_file()
    code = NETWORK_GUARD + """
from pathlib import Path
from prevalidation_manifest import verify_manifest_for_file
context = verify_manifest_for_file(Path(sys.argv[1]))
assert context.verified, context.reason
"""
    _run(python, "-c", code, str(output / source.name), cwd=outside)


def test_installed_favorites_cli_initializes_explicit_user_directory(installed_wheel):
    _, outside, _ = installed_wheel
    catalog = outside / "user-catalog"
    catalog.mkdir()
    _cli(installed_wheel, "FAVORITOS_CATALOGO_ENTERPRISE", str(catalog))
    master = catalog / "favoritos_maestro.txt"
    assert master.is_file()
    _cli(installed_wheel, "FAVORITOS_CATALOGO_ENTERPRISE", str(catalog), "--validate-only")


def test_installed_cli_imports_once_and_reports_conflicts_without_values(installed_wheel, tmp_path):
    python, _, _ = installed_wheel
    source, config = tmp_path / 'legacy', tmp_path / 'profile'
    source.mkdir()
    original = b'# Synthetic private marker: marker-do-not-log\n[Custom]\nExample\n'
    legacy = source / 'favoritos.txt'
    legacy.write_bytes(original)
    env = {**os.environ, 'M3U_CONFIG_DIR': str(config)}
    command = [str(python), '-I', '-B', '-m', 'FAVORITOS_CATALOGO_ENTERPRISE', '--import-legacy', str(source)]
    def invoke():
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=10)
        assert b'marker-do-not-log' not in result.stdout + result.stderr
        return result
    assert invoke().returncode == 0
    master = config / 'favoritos_maestro.txt'
    assert master.read_bytes() == original
    assert b'ya presente' in invoke().stdout
    master.write_bytes(b'my edited selection')
    conflict = invoke()
    assert conflict.returncode == 1 and b'conflicto' in conflict.stdout
    assert master.read_bytes() == b'my edited selection' and legacy.read_bytes() == original


def test_installed_http_worker_imports_siblings_without_checkout(installed_wheel):
    python, outside, _ = installed_wheel
    code = NETWORK_GUARD + """
import time
from network_runtime import run_operation
# Syntactically invalid input cannot trigger any socket or DNS call in the child.
result = run_operation('http', {'entry': {'url': 'not-a-url'}, 'timeout': 1}, deadline=time.monotonic() + 10)
assert result[0] is False and 'URL_ERROR' in result[1], result
"""
    _run(python, '-c', code, cwd=outside)


def test_installed_validation_cancels_and_resumes_without_installation_writes(installed_wheel):
    python, outside, environment = installed_wheel
    code = NETWORK_GUARD + """
import os, threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

state_root = Path(sys.argv[1]).resolve()
installation = Path(sys.argv[2]).resolve()
os.environ['M3U_STATE_DIR'] = str(state_root)

def inside_installation(value):
    if isinstance(value, int) or value is None:
        return False
    path = os.fsdecode(value)
    if path.startswith('file:'):
        path = unquote(urlsplit(path).path)
        if os.name == 'nt' and len(path) > 2 and path[0] == '/' and path[2] == ':':
            path = path[1:]
    return Path(path).resolve().is_relative_to(installation)

def deny_installation_writes(event, args):
    if event == 'open':
        path, mode, flags = args
        writing = bool(set(str(mode or '')) & set('wax+')) or bool(
            (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
        )
        if writing and inside_installation(path):
            raise AssertionError('Installed modules must remain read-only')
    elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'sqlite3.connect'}:
        if inside_installation(args[0]):
            raise AssertionError('Runtime state must not be created in the installation')
    elif event == 'os.rename':
        if any(inside_installation(path) for path in args[:2]):
            raise AssertionError('Installed files must not be moved or replaced')

sys.addaudithook(deny_installation_writes)

import runtime_state
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
from checkpoint_store import CheckpointStore

expected = state_root / 'checkpoints'
assert runtime_state.default_checkpoint_dir().resolve() == expected
assert enterprise.CHECKPOINT_BASE_DIR.resolve() == expected
assert not state_root.exists(), 'Importing installed modules must not create runtime directories'
assert Path(enterprise.__file__).resolve().is_relative_to(installation)

calls = []
def fake_probe(entry, timeout=6, **kwargs):
    calls.append(entry['name'])
    return {'ok': True, 'status': 'VALID', 'reason': 'synthetic',
            'latency_ms': 1, 'streams': [{'codec_type': 'audio'}]}

enterprise.enterprise_ffprobe = fake_probe
entries = [
    {'name': f'Synthetic {index}', 'url': f'https://fixture{index}.invalid/live',
     'directives': []}
    for index in range(3)
]
cancel = threading.Event()
job = 'f' * 64

kept, dead, _ = enterprise.enterprise_validate_all(
    entries[:2], True, 3, 1, None, cancel, job)
assert len(kept) == 2 and not dead and len(calls) == 2

# Cancel a subsequent pass with the same persisted progress and one new item.
cancel.set()
try:
    enterprise.enterprise_validate_all(entries, True, 3, 1, None, cancel, job)
except RuntimeError as exc:
    assert 'detenido' in str(exc).casefold(), str(exc)
else:
    raise AssertionError('Cancellation must be reported to the caller')
assert len(calls) == 2, 'A cancelled job must not probe pending input'
with CheckpointStore(expected, job) as store:
    assert store.count() == 2
    assert store.get_meta('completed') is False
    assert store.get_meta('stage') == 'VALIDATION_INCOMPLETE'

cancel.clear()
kept, dead, _ = enterprise.enterprise_validate_all(
    entries, True, 3, 1, None, cancel, job)
assert len(kept) == 3 and not dead
assert len(calls) == 3, 'Resume must reuse both persisted results'
assert enterprise.enterprise_validate_all.last_metrics['checkpoint_hits'] == 2
with CheckpointStore(expected, job) as store:
    assert store.count() == 3
    assert store.get_meta('completed') is True
    assert store.path.resolve().is_relative_to(expected)
assert not list(Path(enterprise.__file__).parent.glob('validacion_checkpoint_*'))
"""
    # SQLite's Windows path handling is more restrictive than Python's; keep
    # this disposable state outside pytest's deeply nested wheel fixture.
    with tempfile.TemporaryDirectory(prefix="m3u-runtime-") as short_root:
        state_root = Path(short_root) / "state"
        assert not state_root.exists()
        _run(python, "-c", code, str(state_root), str(environment), cwd=outside)

