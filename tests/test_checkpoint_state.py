"""Runtime state contracts using only synthetic temporary checkpoints."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from checkpoint_store import CheckpointStore, CheckpointError, checkpoint_path
from runtime_state import default_checkpoint_dir, recover_closed_checkpoint


JOB = "a" * 64


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_state(base, job=JOB, *, timestamp=100.0):
    with CheckpointStore(base, job) as store:
        store.put_many([("fingerprint", {"ok": True, "status": "VALID"})])
        store.conn.execute("UPDATE results SET updated=?", (timestamp,))
        store.mark_stage("VALIDATION_INCOMPLETE", completed=False)
        store.set_meta("updated", timestamp)
        return store.path


def test_state_resolution_has_no_io_and_override_is_explicit(tmp_path):
    state = tmp_path / "user-state"
    assert default_checkpoint_dir(environ={"M3U_STATE_DIR": str(state)}) == state / "checkpoints"
    assert not state.exists()
    assert default_checkpoint_dir(environ={"LOCALAPPDATA": str(tmp_path)}, platform="win32") == (
        tmp_path / "M3UEnterprise" / "state" / "checkpoints")
    assert default_checkpoint_dir(environ={"XDG_STATE_HOME": str(tmp_path)}, platform="linux") == (
        tmp_path / "m3u-enterprise" / "checkpoints")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["", ".", "../state"])
def test_relative_or_empty_override_fails(value):
    with pytest.raises(CheckpointError):
        default_checkpoint_dir(environ={"M3U_STATE_DIR": value})


@pytest.mark.parametrize("job", ["../bad", "UPPER", "a" * 65, "", "not-hex"])
def test_invalid_job_cannot_alias_or_escape(tmp_path, job):
    with pytest.raises(CheckpointError):
        CheckpointStore(tmp_path / "state", job)
    assert not (tmp_path / "state").exists()


def test_open_existing_does_not_rewrite_identity_or_update_time(tmp_path):
    path = create_state(tmp_path)
    before = digest(path)
    with CheckpointStore(tmp_path, JOB) as store:
        assert store.get_meta("updated") == 100.0
        assert store.get_meta("job_id") == JOB
        assert store.get_meta("stage") == "VALIDATION_INCOMPLETE"
    assert digest(path) == before


@pytest.mark.parametrize("field,value", [("job_id", "b" * 64), ("schema_version", 999)])
def test_existing_incompatible_state_is_not_reset(tmp_path, field, value):
    path = create_state(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET value=? WHERE key=?", (json.dumps(value), field))
    before = digest(path)
    with pytest.raises(CheckpointError):
        CheckpointStore(tmp_path, JOB)
    assert digest(path) == before


def test_existing_corrupt_state_is_preserved(tmp_path):
    path = checkpoint_path(tmp_path, JOB)
    path.write_bytes(b"not-a-sqlite-database")
    before = path.read_bytes()
    with pytest.raises((sqlite3.DatabaseError, CheckpointError)):
        CheckpointStore(tmp_path, JOB)
    assert path.read_bytes() == before


def test_recovery_preserves_source_and_original_ttl(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    source = create_state(legacy, timestamp=100.0)
    before = digest(source)
    destination = recover_closed_checkpoint(target, JOB, legacy)
    assert digest(destination) == before == digest(source)
    assert not list(target.glob(".checkpoint-recovery-*"))
    with CheckpointStore(target, JOB) as store:
        cache, expired = store.get_many_fresh(
            ["fingerprint"], success_ttl_seconds=60, failure_ttl_seconds=5, now=1000)
        assert cache == {} and expired == 1
        assert store.get_meta("stage") == "VALIDATION_INCOMPLETE"
        assert store.get_meta("updated") == 100.0
    assert digest(source) == before


def test_existing_destination_wins_even_when_empty(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    create_state(legacy)
    with CheckpointStore(target, JOB) as current:
        current.set_meta("marker", "current")
    path = checkpoint_path(target, JOB)
    before = digest(path)
    assert recover_closed_checkpoint(target, JOB, legacy) == path
    assert digest(path) == before
    with CheckpointStore(target, JOB) as store:
        assert store.count() == 0
        assert store.get_meta("marker") == "current"


def test_invalid_destination_is_not_replaced_by_legacy(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    create_state(legacy)
    target.mkdir()
    path = checkpoint_path(target, JOB)
    path.write_bytes(b"preserve-broken")
    recover_closed_checkpoint(target, JOB, legacy)
    assert path.read_bytes() == b"preserve-broken"


def test_active_legacy_wal_requires_clean_shutdown(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    with CheckpointStore(legacy, JOB) as store:
        store.put_many([("x", {"ok": True})])
        assert Path(str(store.path) + "-wal").exists()
        with pytest.raises(CheckpointError, match="Cierre"):
            recover_closed_checkpoint(target, JOB, legacy)
        assert not checkpoint_path(target, JOB).exists()


def test_mismatched_legacy_snapshot_is_never_published(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    path = create_state(legacy)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET value=? WHERE key='job_id'", (json.dumps("b" * 64),))
    before = digest(path)
    with pytest.raises(CheckpointError):
        recover_closed_checkpoint(target, JOB, legacy)
    assert not checkpoint_path(target, JOB).exists()
    assert digest(path) == before


def test_concurrent_recovery_publishes_one_complete_snapshot(tmp_path):
    legacy, target = tmp_path / "legacy", tmp_path / "user"
    source = create_state(legacy)
    before = digest(source)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover_closed_checkpoint, target, JOB, legacy) for _ in range(2)]
        paths = [future.result() for future in futures]
    assert paths[0] == paths[1]
    assert digest(paths[0]) == before == digest(source)
    assert paths[0].stat().st_nlink == 1


@pytest.mark.parametrize("timestamp", [100.0, None, float("nan"), float("inf"), -1, 999999999999])
def test_json_import_never_rejuvenates_observations(tmp_path, timestamp):
    data = {"job_id": JOB, "completed": True, "updated": timestamp,
            "results": {"fingerprint": {"ok": True}}}
    with CheckpointStore(tmp_path, JOB) as store:
        assert store.import_legacy_json(data) == 1
        saved = store.conn.execute("SELECT updated FROM results").fetchone()[0]
        assert saved == (100.0 if timestamp == 100.0 else 0)
        cache, expired = store.get_many_fresh(
            ["fingerprint"], success_ttl_seconds=60, failure_ttl_seconds=5, now=1000)
        assert cache == {} and expired == 1


def test_completed_json_still_requires_job_identity(tmp_path):
    with CheckpointStore(tmp_path, JOB) as store:
        with pytest.raises(CheckpointError):
            store.import_legacy_json({"job_id": "b" * 64, "completed": True,
                                      "results": {"x": {"ok": True}}})
        assert store.count() == 0


def test_json_import_does_not_replace_existing_results(tmp_path):
    create_state(tmp_path)
    with CheckpointStore(tmp_path, JOB) as store:
        assert store.import_legacy_json({
            "job_id": JOB, "completed": True, "updated": time.time(),
            "results": {"fingerprint": {"ok": False}},
        }) == 0
        assert store.get("fingerprint")["ok"] is True


def test_overrides_never_discover_module_adjacent_state(tmp_path, monkeypatch):
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path)
    assert enterprise._legacy_checkpoint_dir() is None
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", enterprise.DEFAULT_CHECKPOINT_BASE_DIR)
    monkeypatch.setenv("M3U_STATE_DIR", str(tmp_path))
    assert enterprise._legacy_checkpoint_dir() is None


def test_legacy_completed_json_read_requires_identity_and_does_not_modify_it(tmp_path, monkeypatch):
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path)
    path = checkpoint_path(tmp_path, JOB).with_suffix(".json")
    data = {"job_id": "b" * 64, "completed": True, "updated": 100,
            "results": {"x": {"ok": True}}}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = digest(path)
    with pytest.raises(CheckpointError):
        enterprise._load_checkpoint(JOB)
    assert digest(path) == before
    data["job_id"] = JOB
    path.write_text(json.dumps(data), encoding="utf-8")
    before = digest(path)
    assert enterprise._load_checkpoint(JOB)["updated"] == 100
    assert digest(path) == before


def test_pipeline_does_not_import_json_over_an_existing_empty_store(tmp_path, monkeypatch):
    import threading
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path)
    with CheckpointStore(tmp_path, JOB) as store:
        store.set_meta("marker", "current")
    legacy = checkpoint_path(tmp_path, JOB).with_suffix(".json")
    legacy.write_text(json.dumps({"job_id": JOB, "updated": time.time(),
                                 "results": {"unrelated": {"ok": True}}}))
    enterprise.enterprise_validate_all([], False, 1, 1, None, threading.Event(), JOB)
    with CheckpointStore(tmp_path, JOB) as store:
        assert store.count() == 0
        assert store.get_meta("marker") == "current"


def test_concurrent_store_initialization_preserves_both_writers(tmp_path):
    import threading
    start = threading.Barrier(2)
    def write(index):
        start.wait(timeout=10)
        with CheckpointStore(tmp_path, JOB) as store:
            store.put_many([(str(index), {"ok": True})])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, i) for i in range(2)]
        for future in futures:
            future.result()
    with CheckpointStore(tmp_path, JOB) as store:
        assert store.count() == 2


@pytest.mark.parametrize("invalid", ["{", json.dumps({"job_id": "b" * 64, "completed": True, "results": {}})])
def test_failed_json_recovery_can_be_retried_without_leaving_empty_database(tmp_path, monkeypatch, invalid):
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path)
    source = checkpoint_path(tmp_path, JOB).with_suffix(".json")
    source.write_text(invalid, encoding="utf-8")
    before = digest(source)
    for _ in range(2):
        with pytest.raises(CheckpointError):
            enterprise._open_checkpoint_store(JOB)
        assert not checkpoint_path(tmp_path, JOB).exists()
        assert digest(source) == before
    source.write_text(json.dumps({"job_id": JOB, "updated": 100,
                                 "results": {"x": {"ok": True}}}), encoding="utf-8")
    before = digest(source)
    with enterprise._open_checkpoint_store(JOB) as store:
        assert store.count() == 1
        assert store.get_meta("legacy_imported") is True
        assert store.get_meta("updated") == 100
    assert digest(source) == before


def test_initialization_waits_before_connecting_to_an_uninitialized_database(tmp_path, monkeypatch):
    import threading
    import checkpoint_store
    opened = threading.Event()
    allow_initialization = threading.Event()
    second_attempt = threading.Event()
    second_connected = threading.Event()
    real_connect = checkpoint_store.sqlite3.connect
    first_thread = []
    def controlled_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if not first_thread:
            first_thread.append(threading.get_ident())
            opened.set()
            assert allow_initialization.wait(timeout=5)
        elif threading.get_ident() != first_thread[0]:
            second_connected.set()
        return conn
    monkeypatch.setattr(checkpoint_store.sqlite3, "connect", controlled_connect)
    def first():
        with CheckpointStore(tmp_path, JOB):
            pass
    def second():
        second_attempt.set()
        with CheckpointStore(tmp_path, JOB):
            pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        try:
            assert opened.wait(timeout=5)
            two = pool.submit(second)
            assert second_attempt.wait(timeout=5)
            assert not second_connected.wait(timeout=0.2)
        finally:
            allow_initialization.set()
        one.result()
        two.result()
    assert second_connected.is_set()

