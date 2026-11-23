"""Enterprise integration: policy, cancellation propagation and checkpoint semantics."""
import threading

import pytest

import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
from checkpoint_store import CheckpointStore


def test_bounded_dns_keeps_private_address_rejection(monkeypatch):
    monkeypatch.delenv('M3U_ALLOW_PRIVATE_NETWORKS', raising=False)
    monkeypatch.setattr(enterprise, 'ffprobe_path', lambda: 'unused-ffprobe')
    calls = []
    def resolved(operation, payload, **kwargs):
        calls.append((operation, payload))
        return [[2, 1, 6, '', ['10.0.0.1', 80]]]
    monkeypatch.setattr(enterprise, 'run_operation', resolved)
    monkeypatch.setattr(enterprise, '_run_owned_process', lambda *a, **k: pytest.fail('Unsafe target reached FFprobe'))
    result = enterprise.enterprise_ffprobe({'url': 'http://synthetic.invalid/live'})
    assert not result['ok'] and 'no pública' in result['reason']
    assert calls == [('dns', {'host': 'synthetic.invalid', 'port': 0})]


def test_dns_deadline_is_reported_as_timeout(monkeypatch):
    monkeypatch.delenv('M3U_ALLOW_PRIVATE_NETWORKS', raising=False)
    def deadline(*args, **kwargs):
        raise enterprise.NetworkDeadlineExceeded('synthetic')
    monkeypatch.setattr(enterprise, 'run_operation', deadline)
    result = enterprise.enterprise_ffprobe({'url': 'https://synthetic.invalid/live'})
    assert result['status'] == 'TIMEOUT' and '5s' in result['reason']
    assert result['latency_ms'] >= 0


@pytest.mark.parametrize('deep', [False, True])
def test_cancelled_operation_is_not_cached_as_failure_and_can_resume(tmp_path, monkeypatch, deep):
    monkeypatch.setattr(enterprise, 'CHECKPOINT_BASE_DIR', tmp_path / 'state')
    cancel = threading.Event()
    entry = {'url': 'https://synthetic.invalid/live', 'directives': [], 'name': 'synthetic'}
    job = ('a' if deep else 'b') * 64
    operation = 'enterprise_ffprobe' if deep else 'enterprise_http_validate'
    def cancelled(*args, **kwargs):
        assert kwargs['cancel_event'] is cancel
        cancel.set()
        raise enterprise.NetworkCancelled('synthetic active operation')
    monkeypatch.setattr(enterprise, operation, cancelled)
    with pytest.raises(RuntimeError, match='detenido'):
        enterprise.enterprise_validate_all([entry], deep, 3, 1, None, cancel, job)
    with CheckpointStore(tmp_path / 'state', job) as store:
        assert store.count() == 0
        assert store.get_meta('stage') == 'VALIDATION_INCOMPLETE'
        assert store.get_meta('completed') is False
    cancel.clear()
    result = {'ok': True, 'reason': 'synthetic', 'streams': []} if deep else (True, 'OK HTTP 200', entry['url'])
    monkeypatch.setattr(enterprise, operation, lambda *args, **kwargs: result)
    kept, dead, _ = enterprise.enterprise_validate_all([entry], deep, 3, 1, None, cancel, job)
    assert len(kept) == 1 and not dead
    assert enterprise.enterprise_validate_all.last_metrics['checkpoint_hits'] == 0
    with CheckpointStore(tmp_path / 'state', job) as store:
        assert store.count() == 1 and store.get_meta('completed') is True


def test_http_total_budget_includes_dns_open_redirects_and_body(monkeypatch):
    monkeypatch.setattr(enterprise.time, 'monotonic', lambda: 123.0)
    cancel = threading.Event()
    def expire(operation, payload, **kwargs):
        assert operation == 'http' and payload['timeout'] == 3
        assert kwargs == {'deadline': 131.0, 'cancel_event': cancel}
        raise enterprise.NetworkDeadlineExceeded('synthetic')
    monkeypatch.setattr(enterprise, 'run_operation', expire)
    result = enterprise.enterprise_http_validate({'url': 'http://synthetic.invalid/live'}, 3, cancel_event=cancel)
    assert result[0] is False and '8s' in result[1]


def _synthetic_public_resolution(operation, payload, **kwargs):
    assert operation == "dns" and payload["host"] == "fixture.invalid"
    # Only the classification of the numeric response is used. The leaf command is a fixture.
    return [[2, 1, 6, "", ["8.8.8.8", 0]]]


def test_favorite_ranking_cancels_live_child_and_drains_queue(tmp_path, monkeypatch):
    """The real scheduler and process owner reap one synthetic leaf, without network."""
    import time
    import M3U_PRO_V9_ENGINE as engine
    import network_runtime

    cancel = threading.Event()
    marker = tmp_path / "entered"
    child_code = ("import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(20)")
    launches = []
    launch = network_runtime.subprocess.Popen

    def record_child(*args, **kwargs):
        child = launch(*args, **kwargs)
        launches.append(child)
        return child

    monkeypatch.setattr(network_runtime.subprocess, "Popen", record_child)
    monkeypatch.setattr(engine, "run_operation", _synthetic_public_resolution)
    monkeypatch.setattr(engine, "ffprobe_path", lambda: network_runtime.WORKER_PYTHON)
    monkeypatch.setattr(engine, "_favorite_probe_command", lambda exe, entry, timeout:
                        [exe, "-I", "-B", "-c", child_code, str(marker)])
    group = "00 - Favoritos Noticias"
    favorites = [{"name": f"Canal {i}", "group": group, "url": f"https://fixture.invalid/{i}"}
                 for i in range(8)]
    finished = threading.Event()

    def cancel_when_running():
        end = time.monotonic() + 8
        while not finished.wait(.01) and time.monotonic() < end:
            if marker.exists():
                cancel.set()
                break

    watcher = threading.Thread(target=cancel_when_running)
    watcher.start()
    try:
        with pytest.raises(network_runtime.NetworkCancelled):
            engine.optimize_favorites(favorites, [], group, workers=1, repetitions=5, cancel_event=cancel)
    finally:
        finished.set()
        watcher.join(1)
    assert marker.exists() and cancel.is_set()
    assert len(launches) == 1 and launches[0].poll() is not None


def test_favorite_cancel_during_host_wait_releases_slot(monkeypatch):
    import M3U_PRO_V9_ENGINE as engine
    from network_runtime import NetworkCancelled

    cancel = threading.Event()
    released = []
    class Limiter:
        def acquire(self, _url):
            cancel.set()
            return "slot"

        def release(self, slot):
            released.append(slot)

    monkeypatch.setattr(engine, "ffprobe_path", lambda: "synthetic")
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: pytest.fail("Started after stop"))
    with pytest.raises(NetworkCancelled):
        engine.probe_favorite_quality({"url": "https://fixture.invalid/1"}, limiter=Limiter(),
                                     cancel_event=cancel)
    assert released == ["slot"]


def test_favorite_cancel_during_repeat_interval_stops_next_probe(monkeypatch):
    import M3U_PRO_V9_ENGINE as engine
    from network_runtime import NetworkCancelled

    class StopDuringInterval(threading.Event):
        def wait(self, timeout):
            assert timeout == 1
            self.set()
            return True

    calls = []
    def probe(*args, **kwargs):
        calls.append(args[0])
        return {"ok": False}

    monkeypatch.setattr(engine, "probe_favorite_quality", probe)
    with pytest.raises(NetworkCancelled):
        engine.assess_favorite_stability({"url": "https://fixture.invalid/1"}, repetitions=3,
                                         interval=1, cancel_event=StopDuringInterval())
    assert len(calls) == 1


def test_live_favorite_ranking_keeps_quality_fields(monkeypatch):
    import json
    import M3U_PRO_V9_ENGINE as engine
    import network_runtime

    payload = json.dumps({"streams": [{"codec_type": "video", "width": 1280, "height": 720}]})
    monkeypatch.setattr(engine, "run_operation", _synthetic_public_resolution)
    monkeypatch.setattr(engine, "ffprobe_path", lambda: network_runtime.WORKER_PYTHON)
    monkeypatch.setattr(engine, "_favorite_probe_command", lambda exe, entry, timeout:
                        [exe, "-I", "-B", "-c", "import sys; print(sys.argv[1])", payload])
    result = engine.assess_favorite_stability({"url": "https://fixture.invalid/1"}, repetitions=2,
                                             interval=0, cancel_event=threading.Event())
    assert result["attempts"] == result["success_count"] == 2
    assert (result["width"], result["height"]) == (1280, 720)


@pytest.mark.parametrize("from_gui", [False, True])
def test_favorite_rejected_destination_is_not_probed(monkeypatch, from_gui):
    import time
    import M3U_PRO_V9_ENGINE as engine

    event = threading.Event() if from_gui else None
    resolved = []
    def resolver(operation, payload, *, deadline, cancel_event):
        resolved.append(payload["host"])
        assert operation == "dns" and cancel_event is event
        assert 0 < deadline - time.monotonic() <= 5
        return [[2, 1, 6, "", ["10.0.0.1", 0]]]

    monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS", raising=False)
    monkeypatch.setattr(engine, "run_operation", resolver)
    monkeypatch.setattr(engine, "ffprobe_path", lambda: "synthetic")
    monkeypatch.setattr(engine, "_favorite_probe_command",
                        lambda *a: pytest.fail("FFprobe preparation must follow approval"))
    result = engine.probe_favorite_quality({"url": "https://fixture.invalid/sample"}, cancel_event=event)
    assert resolved == ["fixture.invalid"]
    assert result["ok"] is False and "no pública" in result["reason"]


@pytest.mark.parametrize("outcome", ["stop", "expire"])
def test_favorite_dns_interruption_precedes_probe(monkeypatch, outcome):
    import M3U_PRO_V9_ENGINE as engine
    from network_runtime import NetworkCancelled, NetworkDeadlineExceeded

    cancel = threading.Event()
    def interrupted(operation, payload, *, cancel_event, deadline):
        assert operation == "dns" and cancel_event is cancel
        if outcome == "stop":
            cancel_event.set()
            raise NetworkCancelled("synthetic stop during DNS")
        raise NetworkDeadlineExceeded("synthetic DNS deadline")

    monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS", raising=False)
    monkeypatch.setattr(engine, "run_operation", interrupted)
    monkeypatch.setattr(engine, "ffprobe_path", lambda: "synthetic")
    monkeypatch.setattr(engine, "_favorite_probe_command", lambda *a: pytest.fail("Unexpected FFprobe"))
    entry = {"url": "https://fixture.invalid/sample"}
    if outcome == "stop":
        with pytest.raises(NetworkCancelled):
            engine.probe_favorite_quality(entry, cancel_event=cancel)
    else:
        failure = engine.probe_favorite_quality(entry, cancel_event=cancel)
        assert failure["ok"] is False and "DNS" in failure["reason"] and "5 s" in failure["reason"]
