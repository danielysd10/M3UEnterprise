"""Prevent accidental open requirements in the approved Windows toolchain."""
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _locked_requirements():
    # Restrict the reviewed lock to exact releases with SHA-256 wheel hashes.
    # URL requirements, nested files and index options need a separate review.
    text = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    logical_lines = text.replace("\\\n", " ").splitlines()
    locked = {}
    for line in logical_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)==([0-9]+(?:\.[0-9]+)+)"
            r"((?:\s+--hash=sha256:[a-f0-9]{64})+)", line
        )
        assert match, f"Lock entry must have one exact release and SHA-256 hashes: {line!r}"
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        assert name not in locked, f"Duplicate locked distribution: {name}"
        locked[name] = match.group(2)
    assert locked, "A lock without requirements does not constrain tools"
    return locked


def test_build_and_test_requirements_are_pinned_in_the_hashed_lock():
    locked = _locked_requirements()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["dependencies"] == [], "Tooling must not become a runtime dependency"
    tools = config["build-system"]["requires"] + config["project"]["optional-dependencies"]["test"]
    for requirement in tools:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9]+(?:\.[0-9]+)+)", requirement)
        assert match, f"Build/test requirement must be exactly pinned: {requirement}"
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        assert locked.get(name) == match.group(2), f"Project and lock disagree: {requirement}"
    assert {"pip", "pytest", "setuptools", "wheel"} <= locked.keys()


def test_standalone_runner_reports_optional_modules_and_preserves_failures(tmp_path):
    import shutil
    import subprocess
    import sys

    release = tmp_path / "release"
    suites = release / "tests"
    suites.mkdir(parents=True)
    script = release / "run_self_tests.py"
    shutil.copyfile(ROOT / script.name, script)
    (release / "UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py").write_text(
        "def run_enterprise_self_tests(): pass\n", encoding="utf-8")
    (suites / "test_optional.py").write_text("import pytest\n", encoding="utf-8")

    def run():
        # -I -S excludes the development venv and installed packages.
        return subprocess.run([sys.executable, "-I", "-S", "-B", str(script)],
                              cwd=release, capture_output=True, timeout=20)

    empty = run()
    assert empty.returncode == 1 and b"discovery" in empty.stdout

    (suites / "test_plain.py").write_text("def test_independent(): pass\n", encoding="utf-8")
    valid = run()
    assert valid.returncode == 0 and b"[TEST] test_independent ... PASS" in valid.stdout
    assert b"Delegada a pytest" in valid.stdout

    (suites / "test_broken.py").write_text("import missing_runtime_dependency\n", encoding="utf-8")
    broken = run()
    assert broken.returncode == 1 and b"missing_runtime_dependency" in broken.stdout

    # A production helper accidentally importing pytest is a product failure.
    (release / "broken_helper.py").write_text("import pytest\n", encoding="utf-8")
    (suites / "test_broken.py").write_text("import broken_helper\n", encoding="utf-8")
    transitive = run()
    assert transitive.returncode == 1 and b"test_broken.py :: <import/suite>" in transitive.stdout
