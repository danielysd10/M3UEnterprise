from pathlib import Path
import hashlib
import zipfile

from tools import package_release, release_policy


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_zip_ships_operator_guides_without_checkout_records(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = package_release.build_release(root, tmp_path / "release")
    with zipfile.ZipFile(result["release_zip"]) as archive:
        documents = {name.removeprefix(root.name + "/") for name in archive.namelist()
                     if name.endswith(".md")}

    checkout_records = {
        "ROADMAP.md", "RESUME_DEVELOPMENT.md", "SESSION_HANDOFF.md", "NEXT_SESSION.md",
        "AUDIT_STATUS.md", "DEVELOPMENT_STATUS.md", "FORENSIC_AUDIT_REPORT.md",
        "docs/AUDIT_INVENTORY.md", "docs/LIVE_CAMPAIGN_REPORT.md", "docs/PYTHON_MATRIX.md",
    }
    assert not documents & checkout_records
    assert {
        "README.md", "docs/INSTALLATION_V9_0_2.md", "docs/USAGE_V9_0_2.md",
        "docs/OPERATIONS_RUNBOOK.md", "docs/RELEASE_PACKAGING.md",
        "docs/CONFIGURATION_AND_RECOVERY.md",
    } <= documents


def test_release_excludes_operational_artifacts_and_is_reproducible(tmp_path, monkeypatch):
    root = tmp_path / "release"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "fixture-release"\nversion = "0.0.1"\n', encoding="utf-8")
    (root / "run.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "results.csv").write_text("stream_ref,secret\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "runtime.dll").write_bytes(b"not distributable")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("not distributable\n", encoding="utf-8")
    (root / "channel_metadata_registry.json").write_text('{"epg_url":"https://private.example/guide.xml"}\n', encoding="utf-8")
    monkeypatch.setattr(release_policy, "PAYLOAD_PATHS", frozenset({
        "run.py", "pyproject.toml", "SBOM.cdx.json",
    }))
    result = package_release.build_release(root, tmp_path / "one")
    output = Path(result["release_zip"])
    first = _sha256(output)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert "release/run.py" in names
    assert not any("artifacts/" in name for name in names)
    assert not any(".venv/" in name for name in names)
    assert not any(".git/" in name for name in names)
    assert not any(name.endswith("channel_metadata_registry.json") for name in names)
    assert "release/SBOM.cdx.json" in names

    second = package_release.build_release(root, tmp_path / "two")
    assert _sha256(Path(second["release_zip"])) == first
