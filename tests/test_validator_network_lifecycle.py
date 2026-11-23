"""Synthetic loopback checks for optional network validation and report publication."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import signal
import threading
from types import SimpleNamespace

import pytest

import M3U_PRO_VALIDATOR as validator


@contextmanager
def fixture_server(tmp_path, monkeypatch):
    for key in ("M3U_OUTBOUND_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("M3U_PROFILE", "development")
    monkeypatch.setenv("M3U_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setenv("NO_PROXY", "*")
    started, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("User-Agent")))
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.end_headers()
            started.set()
            if self.path.startswith("/slow"):
                release.wait(4)
            try:
                self.wfile.write(b"wrong" if self.path == "/invalid" else b"#EXTM3U\n")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, started, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def playlist(tmp_path, base, routes):
    path = tmp_path / "test.m3u"
    path.write_text("#EXTM3U\n" + "".join(
        f'#EXTINF:-1 group-title="Test" http-user-agent="synthetic",Channel {i}\n{base}{route}\n'
        for i, route in enumerate(routes)), encoding="utf-8")
    return path


def test_stop_midfile_drops_queue_and_does_not_report_pass(tmp_path, monkeypatch):
    with fixture_server(tmp_path, monkeypatch) as (base, requests, started, release):
        path = playlist(tmp_path, base, ["/slow", "/queued", "/pending"])
        cancel = threading.Event()
        def stop_after_open():
            if started.wait(3):
                cancel.set()
        stopper = threading.Thread(target=stop_after_open)
        stopper.start()
        try:
            result = validator.validate_many([path], network=True, workers=1, timeout=8, cancel_event=cancel)
            assert started.is_set() and result["cancelled"]
            assert result["results"] == [] and result["processed_files"] == 0
            assert [route for route, _ in requests] == ["/slow"]
        finally:
            release.set()
            stopper.join()


def test_validator_cli_sigint_returns_130_keeps_report_and_restores_handler(tmp_path, monkeypatch):
    before = signal.getsignal(signal.SIGINT)
    result_path = tmp_path / "previous.json"
    result_path.write_bytes(b"previous")
    with fixture_server(tmp_path, monkeypatch) as (base, requests, started, release):
        path = playlist(tmp_path, base, ["/slow", "/queued"])
        import _thread
        def interrupt():
            if started.wait(3):
                _thread.interrupt_main()
        stopper = threading.Thread(target=interrupt)
        stopper.start()
        try:
            code = validator.main([str(path), "--report", str(result_path), "--network",
                                   "--workers", "1", "--timeout", "8"])
            assert code == 130 and result_path.read_bytes() == b"previous"
            assert [r for r, _ in requests] == ["/slow"]
            assert signal.getsignal(signal.SIGINT) is before
        finally:
            release.set()
            stopper.join()


def test_validator_network_keeps_headers_playlist_rules_and_timeout(tmp_path, monkeypatch):
    with fixture_server(tmp_path, monkeypatch) as (base, requests, started, release):
        path = playlist(tmp_path, base, ["/ok", "/invalid", "/slow"])
        report = validator.validate_many([path], network=True, workers=1, timeout=1)
        findings = [i for i in report["results"][0]["issues"] if i["code"] == "URL_NETWORK"]
        assert len(findings) == 2
        assert "M3U" in findings[0]["message"]
        assert "timeout" in findings[1]["message"].lower()
        assert requests == [(r, "synthetic") for r in ("/ok", "/invalid", "/slow")]
        assert not report["cancelled"]


def test_gui_cancel_before_done_preserves_existing_report(tmp_path):
    output = tmp_path / "report.json"
    output.write_bytes(b"previous")
    statuses = []
    button = SimpleNamespace(configure=lambda **kwargs: None)
    app = SimpleNamespace(running=True, close_requested=False, validate_button=button, cancel_button=button,
                          status=SimpleNamespace(set=statuses.append), cancel_event=threading.Event())
    validator.ValidatorApp.cancel(app)
    validator.ValidatorApp._done(app, {"cancelled": False}, output)
    assert not app.running and output.read_bytes() == b"previous"
    assert "cancelada" in statuses[-1].lower()


def test_validator_worker_retains_private_target_policy(tmp_path, monkeypatch):
    with fixture_server(tmp_path, monkeypatch) as (base, requests, started, release):
        monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS")
        path = playlist(tmp_path, base, ["/ok"])
        report = validator.validate_many([path], network=True, workers=1, timeout=5)
        assert any(item["code"] == "URL_NETWORK" for item in report["results"][0]["issues"])
        assert not requests


def test_cli_interrupt_before_report_replace_preserves_prior_bytes(tmp_path, monkeypatch):
    path = playlist(tmp_path, "https://example.invalid", ["/offline"])
    output = tmp_path / "report.json"
    output.write_bytes(b"previous")

    def interrupt_write(_fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(validator.os, "fsync", interrupt_write)
    assert validator.main([str(path), "--report", str(output)]) == 130
    assert output.read_bytes() == b"previous"
    assert set(tmp_path.iterdir()) == {path, output}
