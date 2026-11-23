"""Execution regressions without a display, network or operational state."""
from concurrent.futures import ThreadPoolExecutor as RealExecutor
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
import M3U_PRO_V9_ENGINE as engine
from user_settings import SettingsError


class Value:
    def __init__(self, value):
        self.value = value

    def get(self, *args):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_failed_clear_preserves_unsaved_favorites_and_reports_error(monkeypatch):
    """A refused write must leave the user's editable selection available."""
    status, deleted, errors = [], [], []
    app = SimpleNamespace(favorites_text=SimpleNamespace(delete=lambda *args: deleted.append(args)),
                          status_var=SimpleNamespace(set=status.append))
    def denied(_text):
        raise SettingsError('No se pudo guardar el catálogo.')
    monkeypatch.setattr(engine, 'save_favorites_text', denied)
    monkeypatch.setattr(engine.messagebox, 'askyesno', lambda *a, **k: True)
    monkeypatch.setattr(engine.messagebox, 'showerror', lambda *a, **k: errors.append(a))
    engine.App.clear_favorites(app)
    assert errors and not deleted
    assert all('limpiados' not in message.casefold() for message in status)


def test_ffprobe_install_failure_reaches_gui_after_worker_returns(monkeypatch):
    callbacks, errors, status, probe_status = [], [], [], []
    app = SimpleNamespace(progress={}, after=lambda delay, callback: callbacks.append(callback),
                          status_var=SimpleNamespace(set=status.append),
                          ffprobe_status_var=SimpleNamespace(set=probe_status.append))
    app._ffprobe_install_error = engine.App._ffprobe_install_error.__get__(app)

    def refuse_install(**_callbacks):
        raise OSError("laboratory install denied")

    monkeypatch.setattr(engine, "detect_ffprobe", lambda: None)
    monkeypatch.setattr(engine, "install_ffprobe", refuse_install)
    monkeypatch.setattr(engine.threading, "Thread",
                        lambda *, target, daemon: SimpleNamespace(start=target))
    monkeypatch.setattr(engine.messagebox, "showerror", lambda title, msg: errors.append(msg))
    engine.App.download_ffprobe(app)
    (deferred_error,) = callbacks
    deferred_error()
    assert status[-1] == "No se pudo instalar FFprobe."
    assert "Error al instalar FFprobe" in probe_status[-1]
    assert len(errors) == 1 and "laboratory install denied" in errors[0]


def fake_app(output):
    values = {
        "manual_text": "http://example.test/synthetic.ts", "favorites_text": "",
        "favorite_group_var": "General", "validate_var": True, "deep_var": True,
        "timeout_var": 3, "fav_quality_var": False, "fav_sources_var": 1,
        "fav_repetitions_var": 1, "fav_interval_var": 0.0, "fav_fast_var": True,
        "fav_top_var": True, "output_var": str(output), "prevalidated_var": False,
        "metadata_enrich_var": False, "metadata_scope_var": "favorites",
        "metadata_registry_var": "", "metadata_download_xmltv_var": False,
    }
    return SimpleNamespace(
        **{name: Value(value) for name, value in values.items()}, files=[], running=False,
        cancel_event=threading.Event(), _set_running=lambda value: None,
        _merge_worker=lambda snapshot: None,
    )


@pytest.fixture
def merge_effects(monkeypatch):
    effects = {"messages": [], "saved": [], "started": []}
    monkeypatch.setattr(enterprise.messagebox, "showwarning", lambda *args: effects["messages"].append(args))
    monkeypatch.setattr(enterprise.messagebox, "showerror", lambda *args: effects["messages"].append(args))
    monkeypatch.setattr(enterprise, "load_app_config", lambda: {})
    monkeypatch.setattr(enterprise, "save_app_config", lambda data: effects["saved"].append(data))

    class NoWorker:
        def __init__(self, *, target, args, daemon):
            self.args = args

        def start(self):
            effects["started"].append(self.args)

    monkeypatch.setattr(enterprise.threading, "Thread", NoWorker)
    return effects


@pytest.mark.parametrize("output_kind", ["empty", "whitespace", "directory"])
def test_merge_rejects_missing_output_before_side_effects(tmp_path, merge_effects, output_kind):
    output = {"empty": "", "whitespace": "   ", "directory": str(tmp_path)}[output_kind]
    enterprise.EnterpriseApp.merge(fake_app(output))
    assert merge_effects["messages"], "Invalid output must produce a user-facing message"
    assert not merge_effects["saved"] and not merge_effects["started"]


@pytest.mark.parametrize("field,value", [
    ("timeout_var", "invalid"),
    ("timeout_var", enterprise.tk.TclError("expected integer")),
    ("fav_sources_var", "invalid"),
    ("fav_repetitions_var", "invalid"),
    ("fav_interval_var", "invalid"),
    ("fav_interval_var", float("nan")),
    ("fav_interval_var", float("inf")),
])
def test_merge_rejects_invalid_numbers_before_side_effects(tmp_path, merge_effects, field, value):
    app = fake_app(tmp_path / "output.m3u")
    setattr(app, field, Value(value))
    enterprise.EnterpriseApp.merge(app)
    assert merge_effects["messages"], "Invalid fields must produce a user-facing message"
    assert not merge_effects["saved"] and not merge_effects["started"]


def test_merge_valid_fields_keep_existing_limits(tmp_path, merge_effects):
    app = fake_app(tmp_path / "output.m3u")
    app.timeout_var = Value(1)
    app.fav_sources_var = Value(8)
    app.fav_repetitions_var = Value(9)
    app.fav_interval_var = Value(4.0)
    enterprise.EnterpriseApp.merge(app)
    assert not merge_effects["messages"]
    assert len(merge_effects["saved"]) == len(merge_effects["started"]) == 1
    snapshot = merge_effects["started"][0][0]
    assert (snapshot["timeout"], snapshot["fav_sources"], snapshot["fav_reps"], snapshot["fav_interval"]) == (3, 4, 5, 2.0)


@pytest.mark.parametrize("requested,deadline", [(1, 8), (12, 16)])
def test_ffprobe_timeout_reports_actual_elapsed_and_effective_limit(monkeypatch, requested, deadline):
    clock = {"value": 100.0}
    monkeypatch.setattr(enterprise, "ffprobe_path", lambda: "synthetic-ffprobe")
    monkeypatch.setattr(enterprise, "network_target_allowed", lambda url, **kwargs: (True, "test"))
    monkeypatch.setattr(enterprise.time, "perf_counter", lambda: clock["value"])
    monkeypatch.setattr(enterprise.time, "monotonic", lambda: clock["value"])

    def timed_out(command, **kwargs):
        assert kwargs["deadline"] - clock["value"] == deadline
        clock["value"] += deadline + 0.125
        raise enterprise.NetworkDeadlineExceeded("synthetic deadline")

    monkeypatch.setattr(enterprise, "_run_owned_process", timed_out)
    result = enterprise.enterprise_ffprobe({"url": "http://example.test/sample.ts", "directives": []}, requested)
    assert not result["ok"]
    assert result["latency_ms"] == pytest.approx((deadline + 0.125) * 1000)
    assert f"{deadline}s" in result["reason"]


def test_cancel_drains_waiting_workers_before_caller_can_resume(tmp_path, monkeypatch):
    """A cancelled invocation must not leave probes that a later clear reactivates."""
    cancel = threading.Event()
    waiter_started = threading.Event()
    release_waiter = threading.Event()
    waiter_finished = threading.Event()
    shutdown_started = threading.Event()
    finished = threading.Event()
    calls, errors = [], []
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path / "state")

    class ControlledLimiter:
        def acquire(self, url):
            if url.endswith("/1.ts"):
                waiter_started.set()
                if not release_waiter.wait(5):
                    raise AssertionError("Test did not release the waiting worker")
            return url

        def release(self, token):
            if token.endswith("/1.ts"):
                waiter_finished.set()

    class ObservedExecutor(RealExecutor):
        def shutdown(self, *args, **kwargs):
            shutdown_started.set()
            return super().shutdown(*args, **kwargs)

    def probe(entry, timeout, **kwargs):
        calls.append(entry["url"])
        if entry["url"].endswith("/0.ts"):
            assert waiter_started.wait(5)
            cancel.set()
        return {"ok": True, "reason": "synthetic", "streams": []}

    monkeypatch.setattr(enterprise, "StableHostLimiter", ControlledLimiter)
    monkeypatch.setattr(enterprise, "ThreadPoolExecutor", ObservedExecutor)
    monkeypatch.setattr(enterprise, "enterprise_ffprobe", probe)
    entries = [{"url": f"http://example.test/{i}.ts", "directives": [], "name": str(i)} for i in range(2)]

    def run():
        try:
            enterprise.enterprise_validate_all(entries, True, 3, 2, None, cancel, "c" * 64)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    returned_while_waiting = False
    try:
        assert shutdown_started.wait(5)
        returned_while_waiting = finished.wait(0.15)
        if returned_while_waiting:
            # This is what the existing GUI does when it allows an immediate new run.
            cancel.clear()
    finally:
        release_waiter.set()
        thread.join(5)
        assert waiter_finished.wait(5), "Waiting worker did not exit during test cleanup"
    assert finished.is_set(), "Cancellation must finish after in-flight work is released"
    assert not returned_while_waiting, f"Cancellation returned with an old worker still active; probes={calls}"
    assert len(errors) == 1 and "detenido" in str(errors[0]).lower()
    assert calls == [entries[0]["url"]], "A waiting worker must not probe after cancellation"


@pytest.mark.parametrize("archive,sidecar,accepted", [
    ("primary", "matching", True),
    ("mirror", "matching", True),
    ("primary", "mismatch", False),
    ("mirror", "mismatch", False),
    ("mirror", "offline", True),
    ("primary", "offline", False),
    ("historic-primary", "offline", True),
])
def test_portable_checksum_follows_selected_archive(monkeypatch, tmp_path, archive, sidecar, accepted):
    """Model a rolling primary and a version-pinned mirror without external I/O."""
    from hashlib import sha256

    current, historic = b"current synthetic payload", b"historic synthetic payload"
    payload = current if archive == "primary" else historic
    expected_url = engine.FFMPEG_SHA_FALLBACK_URL if archive == "mirror" else engine.FFMPEG_SHA_URL
    checksum_requests = []
    checked = []
    monkeypatch.setattr(engine, "FFMPEG_INSTALL_DIR", tmp_path / "portable")
    monkeypatch.setattr(engine, "FFMPEG_CACHE_ZIP", tmp_path / "download.zip")
    monkeypatch.setattr(engine, "FFMPEG_KNOWN_SHA256", sha256(historic).hexdigest())

    def download(url, dest, **kwargs):
        if url == engine.FFMPEG_ZIP_URL and archive == "mirror":
            raise OSError("primary unavailable")
        if url in (engine.FFMPEG_ZIP_URL, engine.FFMPEG_ZIP_FALLBACK_URL):
            dest.write_bytes(payload)
            return
        checksum_requests.append(url)
        if sidecar == "offline":
            raise OSError("sidecar unavailable")
        advertised = current if url == engine.FFMPEG_SHA_URL else historic
        if sidecar == "mismatch":
            advertised = b"other synthetic release"
        dest.write_text(sha256(advertised).hexdigest(), encoding="ascii")

    def extract(path, target):
        checked.append(path.read_bytes())
        return target / "ffprobe.exe"

    monkeypatch.setattr(engine, "_download_file", download)
    monkeypatch.setattr(engine, "_extract_ffmpeg_zip", extract)
    monkeypatch.setattr(engine, "test_ffprobe", lambda _: (True, "fixture"))
    monkeypatch.setattr(engine, "load_app_config", lambda: {})
    monkeypatch.setattr(engine, "save_app_config", lambda _: None)
    if accepted:
        engine.install_ffprobe()
        assert checked == [payload]
    else:
        with pytest.raises(RuntimeError, match="SHA-256"):
            engine.install_ffprobe()
        assert not checked
    assert checksum_requests == [expected_url]


@pytest.mark.parametrize("point", ["last_audit", "writing"])
def test_stop_at_publication_boundary(monkeypatch, tmp_path, merge_effects, point):
    output = tmp_path / "result.m3u"
    previous = b"#previous playlist"
    output.write_bytes(previous)
    report = tmp_path / "result_informe.txt"
    report.write_text("previous report")
    status, finished = [], []
    app = fake_app(output)
    app.validate_var = Value(False)
    app.status_var = SimpleNamespace(set=status.append)
    app._set_running = lambda value: setattr(app, "running", value)
    app._progress = lambda *args: None
    app.after = lambda delay, callback: callback()
    app._finish_cancelled = lambda msg: finished.append("cancelled")
    app._finish_success = lambda *args: finished.append("published")
    app._finish_error = lambda exc, tb: pytest.fail(tb)
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path / "state")
    audit, write = enterprise.audit_generated_m3u, enterprise.atomic_write_utf8

    def final_audit(text):
        if point == "last_audit":
            enterprise.EnterpriseApp.cancel_run(app)
        return audit(text)

    def write_output(path, text, **kwargs):
        if Path(path) == output and point == "writing":
            enterprise.EnterpriseApp.cancel_run(app)
        return write(path, text, **kwargs)

    monkeypatch.setattr(enterprise, "audit_generated_m3u", final_audit)
    monkeypatch.setattr(enterprise, "atomic_write_utf8", write_output)
    enterprise.EnterpriseApp.merge(app)
    (snapshot,) = merge_effects["started"][0]
    enterprise.EnterpriseApp._merge_worker(app, snapshot)
    if point == "last_audit":
        assert finished == ["cancelled"]
        assert output.read_bytes() == previous and report.read_text() == "previous report"
        assert not list((tmp_path / "state").glob("*.sqlite3"))
    else:
        assert finished == ["published"] and not app.cancel_event.is_set()
        assert "public" in status[-1].lower()
        assert output.read_bytes() != previous and report.read_text(encoding="utf-8-sig") != "previous report"
