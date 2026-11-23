"""Synthetic release boundary tests; no operational playlists or network."""
from pathlib import Path
import hashlib
import json
import base64
import os
import stat
import struct
import subprocess
import zipfile
import uuid

import pytest
from tools import package_release, verify_release


def test_sbom_has_valid_content_identity_without_writing_files(tmp_path):
    from tools.generate_sbom import render
    project = {"name": "synthetic-release", "version": "0.0.1"}
    first = render(project)
    assert render(project) == first
    bom = json.loads(first)
    identity = uuid.UUID(bom["serialNumber"].removeprefix("urn:uuid:"))
    assert identity.version == 5
    assert identity.urn == bom["serialNumber"]
    assert bom["metadata"]["component"]["version"] == "0.0.1"
    assert json.loads(render({**project, "version": "0.0.2"}))["serialNumber"] != identity.urn
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    # The old selector is intentionally exercised before the new policy exists.
    try:
        from tools import release_policy
    except ImportError:
        pass
    else:
        monkeypatch.setattr(release_policy, "PAYLOAD_PATHS", frozenset({
            "run.py", "pyproject.toml", "SBOM.cdx.json",
        }))
    root = tmp_path / "app"
    root.mkdir()
    (root / "run.py").write_text("print('synthetic')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "synthetic"\nversion = "0.0.1"\n', encoding="utf-8")
    monkeypatch.setattr(package_release, "ROOT", root)
    return root


def test_selection_only_approved_sources(release_fixture):
    root = release_fixture
    for relative in (
        "local-data/playlists/private.m3u", "local-data/notes.txt",
        "docs/PRIVATE_DATA_INVENTORY.json", "playlist.M3U8", ".env",
        ".env.production", "unknown.py", "credentials.txt",
        "channel_metadata_registry.json", "checkpoint.sqlite3",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("SYNTHETIC_PRIVATE_MARKER", encoding="utf-8")
    assert {p.relative_to(root).as_posix() for p in package_release.files()} == {
        "run.py", "pyproject.toml",
    }


def test_release_is_reproducible_and_source_is_unchanged(release_fixture, tmp_path):
    root = release_fixture
    (root / "SHA256SUMS_OPTIMIZED.txt").write_text("historical evidence\n")
    (root / "SBOM.cdx.json").write_text("historical SBOM\n")
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    first = package_release.build_release(root, tmp_path / "one")
    second = package_release.build_release(root, tmp_path / "two")
    assert first["release_sha256"] == second["release_sha256"]
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert verify_release.verify(first["release_zip"]) == {"entries": 4, "manifest_rows": 3}
    with zipfile.ZipFile(first["release_zip"]) as archive:
        assert archive.read("app/SBOM.cdx.json") == Path(first["release_sbom"]).read_bytes()
        assert b"SYNTHETIC_PRIVATE_MARKER" not in b"".join(
            archive.read(entry) for entry in archive.infolist())


def test_existing_output_is_not_overwritten(release_fixture, tmp_path):
    first = package_release.build_release(release_fixture, tmp_path / "out")
    before = {p: p.read_bytes() for p in (tmp_path / "out").iterdir()}
    with pytest.raises((FileExistsError, RuntimeError)):
        package_release.build_release(release_fixture, tmp_path / "out")
    assert before == {p: p.read_bytes() for p in (tmp_path / "out").iterdir()}
    assert Path(first["release_zip"]).exists()


def test_published_pair_verifies_in_directory_with_unrelated_files(release_fixture, tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    unrelated = output / "notes.txt"
    unrelated.write_bytes(b"existing notes")

    result = package_release.build_release(release_fixture, output)
    archive = Path(result["release_zip"])
    sbom = Path(result["release_sbom"])
    assert set(output.iterdir()) == {archive, sbom, unrelated}
    assert unrelated.read_bytes() == b"existing notes"
    verified = verify_release.verify(archive, sbom_path=sbom)
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sbom_digest = hashlib.sha256(sbom.read_bytes()).hexdigest()
    assert verified == {"entries": 4, "manifest_rows": 3, "release_sha256": archive_digest,
                        "release_sbom_sha256": sbom_digest}
    assert result["release_sha256"] == archive_digest
    assert result["release_sbom_sha256"] == sbom_digest
    monkeypatch.chdir(output)
    assert verify_release.verify(archive.name, sbom_path=sbom.name) == verified


@pytest.mark.parametrize("case", ["missing", "empty", "mismatched"])
def test_published_pair_rejects_invalid_external_sbom(release_fixture, tmp_path, case):
    result = package_release.build_release(release_fixture, tmp_path / "out")
    archive = Path(result["release_zip"])
    sidecar = Path(result["release_sbom"])
    if case == "missing":
        sidecar.unlink()
    elif case == "empty":
        sidecar.write_bytes(b"")
    else:
        from tools.generate_sbom import render
        sidecar.write_bytes(render({"name": "synthetic", "version": "0.0.2"}))

    with pytest.raises(RuntimeError, match="SBOM"):
        verify_release.verify(archive, sbom_path=sidecar)
    assert verify_release.verify(archive) == {"entries": 4, "manifest_rows": 3}


def test_interrupted_publication_retries_in_new_directory(release_fixture, tmp_path, monkeypatch):
    output = tmp_path / "interrupted"
    sidecar = output / "SBOM.cdx.json"
    before_commit = {}

    def interrupt(staged_zip, target):
        assert not target.exists()
        with zipfile.ZipFile(staged_zip) as archive:
            before_commit["sbom"] = archive.read("app/SBOM.cdx.json")
        assert sidecar.read_bytes() == before_commit["sbom"]
        before_commit["zip_sha256"] = hashlib.sha256(staged_zip.read_bytes()).hexdigest()
        before_commit["target"] = target
        raise InterruptedError("synthetic interruption")

    with monkeypatch.context() as patch:
        patch.setattr(package_release, "_publish_zip", interrupt)
        with pytest.raises(InterruptedError, match="synthetic interruption"):
            package_release.build_release(release_fixture, output)

    assert set(output.iterdir()) == {sidecar}
    with pytest.raises(RuntimeError, match="incompleta"):
        verify_release.verify(before_commit["target"], sbom_path=sidecar)
    with pytest.raises(FileExistsError):
        package_release.build_release(release_fixture, output)
    assert set(output.iterdir()) == {sidecar}
    assert sidecar.read_bytes() == before_commit["sbom"]

    retry = package_release.build_release(release_fixture, tmp_path / "retry")
    verified = verify_release.verify(retry["release_zip"], sbom_path=retry["release_sbom"])
    assert verified["release_sha256"] == before_commit["zip_sha256"]
    assert Path(retry["release_sbom"]).read_bytes() == before_commit["sbom"]


def test_foreign_zip_appearing_before_commit_is_preserved(release_fixture, tmp_path, monkeypatch):
    publish_zip = package_release._publish_zip
    output = tmp_path / "out"
    foreign_bytes = b"existing file created during publication"

    def collide(staged_zip, target):
        assert not target.exists()
        with target.open("xb") as foreign:
            foreign.write(foreign_bytes)
        publish_zip(staged_zip, target)

    monkeypatch.setattr(package_release, "_publish_zip", collide)
    with pytest.raises(FileExistsError):
        package_release.build_release(release_fixture, output)
    (target,) = output.glob("*.zip")
    assert target.read_bytes() == foreign_bytes
    assert (output / "SBOM.cdx.json").stat().st_size > 0


def test_missing_approved_source_fails_before_output(release_fixture, tmp_path):
    (release_fixture / "run.py").unlink()
    with pytest.raises((FileNotFoundError, RuntimeError)):
        package_release.build_release(release_fixture, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("link_kind", ["file", "parent", "root", "output"])
def test_symlink_boundaries_fail_closed(release_fixture, tmp_path, monkeypatch, link_kind):
    from tools import release_policy
    root = release_fixture
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SYNTHETIC_OUTSIDE")
    output = tmp_path / "out"
    try:
        if link_kind == "file":
            (root / "run.py").unlink()
            (root / "run.py").symlink_to(outside / "secret.py")
        elif link_kind == "parent":
            (root / "linked").symlink_to(outside, target_is_directory=True)
            monkeypatch.setattr(release_policy, "PAYLOAD_PATHS", frozenset({
                "linked/secret.py", "pyproject.toml", "SBOM.cdx.json",
            }))
        elif link_kind == "root":
            alias = tmp_path / "alias"
            alias.symlink_to(root, target_is_directory=True)
            root = alias
        else:
            output.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Host cannot create symlinks: {exc.winerror if os.name == 'nt' else exc.errno}")
    with pytest.raises(RuntimeError):
        package_release.build_release(root, output)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.py"]


def test_hardlinked_source_is_rejected(release_fixture, tmp_path):
    original = release_fixture / "run.py"
    os.link(original, tmp_path / "another-name.py")
    with pytest.raises(RuntimeError):
        package_release.build_release(release_fixture, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("location", ["parent", "root", "output"])
def test_windows_junctions_are_rejected(release_fixture, tmp_path, monkeypatch, location):
    from tools import release_policy
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SYNTHETIC_OUTSIDE")
    root, output = release_fixture, tmp_path / "out"
    link = root / "linked" if location == "parent" else tmp_path / "junction"
    target = root if location == "root" else outside
    script = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Target '" + str(target).replace("'", "''") + "' -ErrorAction Stop | Out-Null"
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                   check=True, capture_output=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    assert link.is_junction()
    if location == "parent":
        monkeypatch.setattr(release_policy, "PAYLOAD_PATHS", frozenset({
            "linked/secret.py", "pyproject.toml", "SBOM.cdx.json",
        }))
    elif location == "root":
        root = link
    else:
        output = link
    with pytest.raises(RuntimeError):
        package_release.build_release(root, output)
    assert sorted(p.name for p in outside.iterdir()) == ["secret.py"]


def _zip(tmp_path, *, names=None, rows=None, extra=None, mode=None):
    data = {"run.py": b"print('fixture')\n", "pyproject.toml": b"[project]\n",
            "SBOM.cdx.json": b"{}"}
    manifest = "\n".join(
        f"{hashlib.sha256(content).hexdigest()}  {relative}"
        for relative, content in data.items()) + "\n"
    target = tmp_path / "fixture.zip"
    with zipfile.ZipFile(target, "w") as archive:
        for relative, content in data.items():
            name = (names or {}).get(relative, "app/" + relative)
            info = zipfile.ZipInfo(name)
            # Preserve hostile raw names: ZipInfo normalizes native separators
            # on Windows when constructed, which would invalidate this fixture.
            info.filename = info.orig_filename = name
            if mode is not None and relative == "run.py":
                info.create_system = 3
                info.external_attr = mode << 16
            archive.writestr(info, content)
        archive.writestr("app/SHA256SUMS_OPTIMIZED.txt", rows if rows is not None else manifest)
        if extra:
            archive.writestr(*extra)
    return target


@pytest.mark.parametrize("bad_name", [
    "../run.py", "/app/run.py", "app/../run.py", "app/./run.py",
    "app//run.py", r"app\run.py", "C:/app/run.py", "app/run.py:stream",
    "app/run.py.", "app/run.py ", "app/CON.py", "bad/run.py",
    "app/local-data/run.py", "app/.env", "app/playlist.M3U8",
])
def test_verifier_rejects_unsafe_or_unapproved_paths(release_fixture, tmp_path, bad_name):
    with pytest.raises(RuntimeError):
        verify_release.verify(str(_zip(tmp_path, names={"run.py": bad_name})))


@pytest.mark.parametrize("extra", [
    ("app/unknown.py", b"x"), ("app/local-data/private.m3u", b"x"),
    ("app/RUN.py", b"x"), ("app/run.py", b"x"),
    ("app/unused/", b""), ("app/nested/SHA256SUMS_OPTIMIZED.txt", b""),
])
def test_verifier_rejects_extra_duplicate_or_directory_entries(release_fixture, tmp_path, extra):
    with pytest.raises(RuntimeError):
        verify_release.verify(str(_zip(tmp_path, extra=extra)))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o755])
def test_verifier_rejects_nonregular_entries(release_fixture, tmp_path, mode):
    with pytest.raises(RuntimeError):
        verify_release.verify(str(_zip(tmp_path, mode=mode)))


@pytest.mark.parametrize("rows", [
    "z" * 64 + "  run.py\n", "0" * 64 + "  ../run.py\n",
    "0" * 64 + "  run.py\n" + "0" * 64 + "  run.py\n",
    "0" * 64 + "  run.py\n" + "0" * 64 + "  RUN.py\n", "",
])
def test_verifier_rejects_invalid_or_incomplete_manifest(release_fixture, tmp_path, rows):
    with pytest.raises(RuntimeError):
        verify_release.verify(str(_zip(tmp_path, rows=rows)))


def test_verifier_checks_hashes(release_fixture, tmp_path):
    target = _zip(tmp_path)
    with zipfile.ZipFile(target) as archive:
        rows = archive.read("app/SHA256SUMS_OPTIMIZED.txt").decode()
    rows = "0" * 64 + rows[64:]
    with pytest.raises(RuntimeError, match="Hash"):
        verify_release.verify(str(_zip(tmp_path, rows=rows)))


def test_verifier_rejects_encryption_flags(release_fixture, tmp_path):
    target = _zip(tmp_path)
    raw = bytearray(target.read_bytes())
    for marker, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = raw.index(marker)
        flags = struct.unpack_from("<H", raw, position + offset)[0]
        struct.pack_into("<H", raw, position + offset, flags | 1)
    target.write_bytes(raw)
    with pytest.raises(RuntimeError):
        verify_release.verify(str(target))


def test_verifier_bounds_uncompressed_content(release_fixture, tmp_path, monkeypatch):
    from tools import release_policy
    target = _zip(tmp_path)
    monkeypatch.setattr(release_policy, "MAX_FILE_BYTES", 1)
    with pytest.raises(RuntimeError):
        verify_release.verify(str(target))

