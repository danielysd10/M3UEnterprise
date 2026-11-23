from pathlib import Path
import tempfile
import threading

import M3U_PRO_V9_ENGINE as e
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent
from url_safety import URLStatus, inspect_url, redact_url_for_export


def _entry(url, name="Canal"):
    return {
        "name": name,
        "group": "General",
        "extinf": f'#EXTINF:0 group-title="General",{name}',
        "directives": [],
        "url": url,
        "source": "test",
    }


def test_original_incident_regression_never_crashes_report():
    bad = "rtmp-raw=rtmp:"
    assert "PORT" not in e.redact_url_for_report(bad)
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "report.txt"
        dead = [{**_entry(bad), "validation_enterprise": {
            "ok": False,
            "status": "URL_ERROR",
            "reason": "URL inválida: falta protocolo",
        }}]
        e.save_report(report, [("incident.m3u", 1)], 0, 1, 0, dead, [], [], True,
                      validation_metrics={"statuses": {"URL_ERROR": 1}, "url_errors": 1},
                      pipeline_metrics={"total_seconds": 1, "stages": {"validacion": 0.1}, "input_entries": 1})
        text = report.read_text(encoding="utf-8")
        assert "rtmp-raw=rtmp:" not in text
        assert "URL_ERROR" in text


def test_rtmp_raw_wrapper_is_repaired_at_ingestion():
    raw = "rtmp://$OPT:rtmp-raw=rtmp://maaya.live-s.cdn.bitgravity.com/cdn-live-b1"
    inspected = inspect_url(raw)
    assert inspected.status == URLStatus.REPAIRED
    assert inspected.normalized_url == "rtmp://maaya.live-s.cdn.bitgravity.com/cdn-live-b1"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "x.m3u"
        src.write_text(
            '#EXTM3U\n#EXTINF:0 group-title="News",Tv9\n#EXTGRP:News\n' + raw + '\n',
            encoding="utf-8",
        )
        parsed = e.parse_m3u(src)
        assert len(parsed) == 1
        assert parsed[0]["url"] == inspected.normalized_url
        assert parsed[0]["url_original"] == raw
        assert parsed[0]["url_validation"]["status"] == "REPAIRED"


def test_invalid_port_is_quarantined_before_network_probe():
    calls = {"n": 0}
    original = ent.enterprise_ffprobe

    def fake_probe(entry, timeout=6, **kwargs):
        calls["n"] += 1
        return {"ok": True, "reason": "should not happen", "latency_ms": 1, "streams": [{"codec_type": "video"}]}

    ent.enterprise_ffprobe = fake_probe
    old_base = ent.CHECKPOINT_BASE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ent.CHECKPOINT_BASE_DIR = Path(td)
            entries = [_entry("http://example.com:not-a-port/live")]
            kept, dead, _ = ent.enterprise_validate_all(
                entries, True, 2, 2, None, threading.Event(), "a" * 64
            )
            assert not kept and len(dead) == 1
            assert dead[0]["validation_enterprise"]["status"] == "URL_ERROR"
            assert calls["n"] == 0
    finally:
        ent.CHECKPOINT_BASE_DIR = old_base
        ent.enterprise_ffprobe = original


def test_worker_exception_isolated_per_record():
    original = ent.enterprise_ffprobe
    old_base = ent.CHECKPOINT_BASE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ent.CHECKPOINT_BASE_DIR = Path(td)
            def fake_probe(entry, timeout=6, **kwargs):
                if entry["name"] == "bad":
                    raise RuntimeError("synthetic worker failure")
                return {"ok": True, "reason": "fake", "latency_ms": 1, "streams": [{"codec_type": "video"}]}
            ent.enterprise_ffprobe = fake_probe
            kept, dead, _ = ent.enterprise_validate_all(
                [_entry("http://example.com/good", "good"), _entry("http://example.com/bad", "bad")],
                True, 2, 2, None, threading.Event(), "b" * 64
            )
            assert [x["name"] for x in kept] == ["good"]
            assert [x["name"] for x in dead] == ["bad"]
            assert dead[0]["validation_enterprise"]["status"] == "UNEXPECTED_ERROR"
    finally:
        ent.CHECKPOINT_BASE_DIR = old_base
        ent.enterprise_ffprobe = original


def test_redaction_survives_malformed_urls_and_credentials():
    samples = [
        "http://user:password@example.com:abc/path?token=secret&quality=hd",
        "http://[broken/live",
        "http://example.com:65536/live",
        "rtsp://user:pass@[::1]:8554/live?api_key=secret",
        None,
        "",
        "not-a-url",
    ]
    for value in samples:
        text = e.redact_url_for_report(value)
        assert isinstance(text, str) and text
        assert "password" not in text.lower()
        assert "secret" not in text.lower()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("URL HARDENING TESTS OK")


def test_url_classification_matrix_includes_ipv6_and_empty_port():
    cases = {
        "http://example.com/live": URLStatus.VALID,
        "https://example.com/live.m3u8": URLStatus.VALID,
        "rtmp://example.com/live": URLStatus.VALID,
        "rtsp://example.com:8554/live": URLStatus.VALID,
        "udp://239.0.0.1:1234": URLStatus.VALID,
        "http://[::1]:8080/live": URLStatus.PRIVATE_NETWORK,
        "http://[broken/live": URLStatus.INVALID_SYNTAX,
        "http://example.com:/live": URLStatus.INVALID_PORT,
        "http://example.com:70000/live": URLStatus.INVALID_PORT,
        "http://example.com:notnum/live": URLStatus.INVALID_PORT,
        "ftp://example.com/file": URLStatus.UNSUPPORTED_SCHEME,
        "not-a-url": URLStatus.INVALID_SYNTAX,
        "": URLStatus.EMPTY,
        None: URLStatus.EMPTY,
        123: URLStatus.NON_STRING,
    }
    for raw, expected in cases.items():
        assert inspect_url(raw).status == expected, (raw, inspect_url(raw))


def test_unknown_protocol_is_ingested_but_quarantined():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "unknown.m3u"
        src.write_text(
            '#EXTM3U\n#EXTINF:0 group-title="General",X\nftp://example.com/x\n',
            encoding="utf-8",
        )
        entries = e.parse_m3u(src)
        assert len(entries) == 1
        assert entries[0]["url_validation"]["status"] == "UNSUPPORTED_SCHEME"
        valid, dead = ent._quarantine_invalid_urls(entries)
        assert valid == []
        assert len(dead) == 1
        assert dead[0]["validation_enterprise"]["status"] == "URL_UNSUPPORTED"


def test_report_never_requires_url_or_name_keys():
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "report.txt"
        dead = [{"validation_enterprise": {"ok": False, "status": "URL_ERROR", "reason": "bad"}}]
        e.save_report(report, [], 0, 1, 0, dead, [], [], False)
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert "URL_ERROR" in text


def test_validation_helpers_accept_legacy_string_urls():
    ok_http, reason_http, shown_http = e.validate_url_http("http://host:notaport/live")
    assert not ok_http and "URL_ERROR" in reason_http and shown_http
    ok_probe, reason_probe, shown_probe, source_probe = e.validate_url_ffprobe("http://host:notaport/live", timeout=2)
    assert not ok_probe and "URL_ERROR" in reason_probe and shown_probe and source_probe == "URL"


def test_direct_ffprobe_gate_skips_subprocess_for_bad_url():
    calls = {"n": 0}
    original = e.ffprobe_path
    def fake_path():
        return "ffprobe-not-used"
    e.ffprobe_path = fake_path
    try:
        result = e.validate_url_ffprobe(_entry("http://host:notaport/live"), timeout=2)
        assert result[0] is False
        assert result[1].startswith("URL_ERROR:")
    finally:
        e.ffprobe_path = original


def test_favorite_probe_gate_rejects_bad_url_before_ffprobe():
    original = e.ffprobe_path
    e.ffprobe_path = lambda: "ffprobe-not-used"
    try:
        result = e.probe_favorite_quality(_entry("http://host:notaport/live"), timeout=2)
        assert not result["ok"]
        assert result["reason"].startswith("URL_ERROR:")
    finally:
        e.ffprobe_path = original


def test_redact_text_redacts_unknown_scheme_credentials_too():
    text = e.redact_url_for_report("ftp://user:TEST_SECRET@example.com/file")
    assert "TEST_SECRET" not in text
    diagnostic = __import__('url_safety').redact_text("failure ftp://user:TEST_SECRET@example.com/file")
    assert "TEST_SECRET" not in diagnostic


def test_export_redaction_never_serializes_path_query_or_credentials():
    raw = "https://user:password@example.com/private-token/ABC123?token=SECRET#fragment"
    exported = redact_url_for_export(raw)
    assert exported.startswith("https://example.com/[PATH_REDACTED]#sha256=")
    for secret in ("user", "password", "private-token", "ABC123", "SECRET", "fragment"):
        assert secret not in exported


def test_validation_disabled_pipeline_quarantines_malformed_url():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "bad.m3u"
        src.write_text(
            '#EXTM3U\n'
            '#EXTINF:0 group-title="General",Bad\n'
            'http://example.com:notaport/live\n'
            '#EXTINF:0 group-title="General",Good\n'
            'http://example.com/good\n', encoding='utf-8')
        valid, dead = ent._quarantine_invalid_urls(e.parse_m3u(src))
        assert [x["name"] for x in valid] == ["Good"]
        assert [x["name"] for x in dead] == ["Bad"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("URL HARDENING TESTS OK")


def test_validator_uses_central_url_classification():
    from M3U_PRO_VALIDATOR import validate
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "validator_bad.m3u"
        src.write_text(
            '#EXTM3U\n#EXTINF:0 group-title="General",Bad\n'
            'http://example.com:notaport/live\n', encoding='utf-8')
        result = validate(src)
        assert result["ok"] is False
        assert result["url_issues"] == 1
        assert any("INVALID_PORT" in issue for issue in result["issues"])


def test_report_failure_does_not_rollback_published_output():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src.m3u"
        src.write_text('#EXTM3U\n#EXTINF:0 group-title="General",Good\nhttp://example.com/good\n', encoding='utf-8')
        out = root / "final.m3u"
        snap = {
            "files": [str(src)], "manual": "", "favorites": "",
            "favorite_group": "00 - Favoritos", "validate": False, "deep": False,
            "timeout": 3, "workers": 2, "fav_quality": False,
            "fav_sources": 4, "fav_reps": 1, "fav_interval": 0,
            "fav_fast": True, "fav_top": True, "output": str(out),
        }
        class Fake:
            cancel_event = ent.MergeCancellation()
            progress_started = None
            def _progress(self, *args, **kwargs): pass
            def _finish_success(self, *args): self.success_args = args
            def _finish_error(self, exc, tb): raise AssertionError(tb)
            def after(self, _delay, fn): fn()
        old_base = ent.CHECKPOINT_BASE_DIR
        old_report = ent.save_report
        try:
            ent.CHECKPOINT_BASE_DIR = root / "state"
            def explode(*args, **kwargs):
                raise RuntimeError("synthetic report failure")
            ent.save_report = explode
            app = Fake()
            ent.EnterpriseApp._merge_worker(app, snap)
            assert out.exists()
            fallback = root / "final_informe_fallback.txt"
            assert fallback.exists()
            assert "Reporte completo no disponible" in fallback.read_text(encoding="utf-8")
            assert app.success_args[-1] is not None
        finally:
            ent.CHECKPOINT_BASE_DIR = old_base
            ent.save_report = old_report
