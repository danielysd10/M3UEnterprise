# -*- coding: utf-8 -*-
"""Self-test runner Enterprise sin dependencias externas.

Ejecuta primero las pruebas internas de la V9 activa y luego las suites
historicas ubicadas en la carpeta Enterprise padre, si existen.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import time
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENTERPRISE_ROOT = ROOT.parent
TEST_DIRS = [path for path in (ROOT / "tests", ENTERPRISE_ROOT / "tests") if path.exists()]
# The active release suite is always loaded first; legacy parent suites are optional.
ACTIVE_TEST_DIR = ROOT / "tests"


def _load_module(path: Path):
    name = f"selftest_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se pudo cargar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _iter_test_files():
    seen = set()
    for directory in TEST_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("test_*.py")):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(1, str(ENTERPRISE_ROOT))

    print("=== SELF TEST ENTERPRISE V9 PRODUCCION ===")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Version activa: {ROOT}")
    print(f"Suite historica opcional: {ENTERPRISE_ROOT / 'tests'}")
    print()

    failures = []
    executed = 0
    skipped = 0
    delegated_modules = 0
    active_tests = 0
    pytest_absent = importlib.util.find_spec('pytest') is None
    started = time.perf_counter()

    try:
        from UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE import run_enterprise_self_tests

        print("[SUITE] pruebas internas V9 activa")
        t0 = time.perf_counter()
        run_enterprise_self_tests()
        executed += 1
        print(f"  [TEST] run_enterprise_self_tests ... PASS ({time.perf_counter() - t0:.3f}s)")
        print()
    except Exception as exc:
        failures.append(("internal", "run_enterprise_self_tests", exc))
        print("  [TEST] run_enterprise_self_tests ... FAIL")
        print(f"         {type(exc).__name__}: {exc}")
        print()

    files = list(_iter_test_files())

    for path in files:
        print(f"[SUITE] {path}")
        try:
            try:
                module = _load_module(path)
            except ModuleNotFoundError as exc:
                origin = traceback.extract_tb(exc.__traceback__)[-1].filename
                if not (pytest_absent and exc.name == "pytest" and Path(origin).resolve() == path.resolve()):
                    raise
                delegated_modules += 1
                print("  [SUITE] Delegada a pytest (importa pytest directamente).")
                continue
            funcs = [
                (name, obj)
                for name, obj in sorted(vars(module).items())
                if name.startswith("test_") and callable(obj)
            ]
            for name, func in funcs:
                # This runner intentionally has no pytest dependency.  Tests
                # declaring fixtures belong to the full pytest suite, where
                # tmp_path/monkeypatch and other fixtures are provided.
                required = [
                    parameter.name for parameter in inspect.signature(func).parameters.values()
                    if parameter.default is inspect.Parameter.empty
                    and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                ]
                if required:
                    skipped += 1
                    print(f"  [TEST] {name} ... SKIP (requiere pytest fixtures: {', '.join(required)})")
                    continue
                if path.parent == ACTIVE_TEST_DIR:
                    active_tests += 1
                executed += 1
                print(f"  [TEST] {name} ...", end=" ")
                t0 = time.perf_counter()
                try:
                    func()
                    print(f"PASS ({time.perf_counter() - t0:.3f}s)")
                except Exception as exc:
                    failures.append((path.name, name, exc))
                    print("FAIL")
                    print(f"         {type(exc).__name__}: {exc}")
        except Exception as exc:
            failures.append((path.name, "<import/suite>", exc))
            print(f"  [SUITE FAIL] {type(exc).__name__}: {exc}")
        print()

    if not active_tests:
        failures.append(("discovery", str(ACTIVE_TEST_DIR),
                         RuntimeError("No se ejecutaron autocontroles de los módulos de la suite activa.")))
    elapsed = time.perf_counter() - started
    print("=== RESULTADO ===")
    print(f"Tests ejecutados: {executed}")
    print(f"Funciones delegadas a pytest por sus parámetros: {skipped}")
    print(f"Módulos delegados completos por falta de pytest: {delegated_modules}")
    print(f"Fallos:           {len(failures)}")
    print(f"Tiempo total:     {elapsed:.3f}s")
    print()

    if failures:
        print("FALLOS DETECTADOS:")
        for suite, name, exc in failures:
            print(f"- {suite} :: {name} :: {type(exc).__name__}: {exc}")
        print()
        print("SELF TEST: FAIL")
        return 1

    print("SELF TEST: PASS")
    print("Resultado de los autocontroles independientes. Para la suite completa, use pytest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
