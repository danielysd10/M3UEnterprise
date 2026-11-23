import json
import gzip
import threading
import time
from io import BytesIO
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import channel_metadata
from network_runtime import NetworkCancelled, NetworkDeadlineExceeded
from channel_metadata import audit_xmltv_coverage, download_xmltv, enrich_entries, epg_header, load_registry
from M3U_PRO_V9_ENGINE import build_m3u


def _entry(name, group, extinf):
    return {"name": name, "group": group, "extinf": extinf, "directives": [], "url": "https://example.test/live"}


def test_metadata_enrichment_defaults_to_favorites_and_preserves_existing_logo():
    registry = {"schema": 1, "epg_url": "https://epg.example.test/guide.xml", "channels": [{"name": "Canal Uno", "tvg_id": "uno", "epg_id": "uno.epg", "official_logo_url": "https://official.example.test/logo.png", "official_logo_source": "https://official.example.test/"}]}
    favorite = _entry("Canal Uno", "00 - Favoritos Colombia", '#EXTINF:-1 group-title="00 - Favoritos Colombia",Canal Uno')
    non_favorite = _entry("Canal Uno", "Colombia", '#EXTINF:-1,Canal Uno')
    result, summary = enrich_entries([favorite, non_favorite], registry)
    assert 'tvg-logo="https://official.example.test/logo.png"' in result[0]["extinf"]
    assert 'tvg-id="uno.epg"' in result[0]["extinf"]
    assert result[1]["extinf"] == non_favorite["extinf"]
    assert summary["logos_added"] == 1 and summary["epg_ids_added"] == 1


def test_metadata_does_not_replace_existing_logo_or_accept_unproven_logo():
    registry = {"schema": 1, "channels": [{"name": "Canal", "official_logo_url": "http://not-secure.example/logo.png", "official_logo_source": "https://official.example/"}]}
    entry = _entry("Canal", "00 - Favoritos Deportes", '#EXTINF:-1 tvg-logo="https://existing.example/logo.png",Canal')
    result, summary = enrich_entries([entry], registry)
    assert "existing.example" in result[0]["extinf"]
    assert summary["logos_preserved"] == 1
    assert epg_header("https://epg.example.test/guide.xml").startswith("#EXTM3U url-tvg=")


def test_m3u_header_exposes_xmltv_only_for_safe_https_url():
    entry = _entry("Canal", "General", "#EXTINF:-1,Canal")
    assert 'url-tvg="https://epg.example.test/guide.xml"' in build_m3u([entry], epg_url="https://epg.example.test/guide.xml").splitlines()[0]
    assert build_m3u([entry], epg_url="http://epg.example.test/guide.xml").startswith("#EXTM3U\n")


def test_registry_rejects_non_https_epg_and_xmltv_cache_does_not_touch_network(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema": 1, "epg_url": "http://example.test/guide.xml", "channels": []}), encoding="utf-8")
    try:
        load_registry(invalid)
        assert False, "El registro HTTP debía rechazarse"
    except RuntimeError as exc:
        assert "HTTPS" in str(exc)

    cached = tmp_path / "guide.xml"
    cached.write_bytes(b"<tv></tv>")
    result = download_xmltv("https://not-contacted.example.test/guide.xml", cached, max_age_seconds=3600)
    assert result["cached"] is True


def test_registry_rejects_ambiguous_identity(tmp_path):
    import pytest

    registry = tmp_path / "ambiguous.json"
    registry.write_text(json.dumps({"schema": 2, "channels": [{"name": "Canal Uno", "aliases": ["Uno"]}, {"name": "Canal Dos", "aliases": ["Uno"]}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ambigua"):
        load_registry(registry)


def test_registry_rejects_duplicate_epg_id(tmp_path):
    import pytest

    registry = tmp_path / "duplicate-epg.json"
    registry.write_text(json.dumps({"schema": 2, "channels": [{"name": "Canal Uno", "epg_id": "shared"}, {"name": "Canal Dos", "epg_id": "shared"}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="epg_id duplicado"):
        load_registry(registry)


def test_xmltv_gzip_is_saved_as_bounded_plain_xml_and_coverage_is_aggregated(tmp_path, monkeypatch):
    payload = gzip.compress(b'<tv><channel id="uno.epg"/><programme channel="uno.epg"/></tv>')
    monkeypatch.setattr(channel_metadata, "open_public_http", lambda *_args, **_kwargs: (BytesIO(payload), "https://epg.example.test/guide.xml"))
    destination = tmp_path / "guide.xml"
    result = download_xmltv("https://epg.example.test/guide.xml", destination, max_age_seconds=0, max_uncompressed_bytes=1024)
    assert result["compressed_source"] is True
    assert destination.read_bytes().startswith(b"<tv")
    entry = _entry("Canal Uno", "00 - Favoritos Colombia", '#EXTINF:-1 tvg-id="uno.epg",Canal Uno')
    coverage = audit_xmltv_coverage(destination, [entry])
    assert coverage["channel_ids_found"] == 1 and coverage["ids_with_programme"] == 1


def test_xmltv_gzip_expansion_limit_is_enforced(tmp_path, monkeypatch):
    import pytest

    payload = gzip.compress(b"<tv>" + b"x" * 4096 + b"</tv>")
    monkeypatch.setattr(channel_metadata, "open_public_http", lambda *_args, **_kwargs: (BytesIO(payload), "https://epg.example.test/guide.xml"))
    with pytest.raises(RuntimeError, match="descomprimido"):
        download_xmltv("https://epg.example.test/guide.xml", tmp_path / "guide.xml", max_age_seconds=0, max_uncompressed_bytes=128)


def test_xmltv_rejects_dtd_and_limits_parsed_elements(tmp_path):
    import pytest

    guide = tmp_path / "guide.xml"
    guide.write_text('<!DOCTYPE tv [<!ENTITY x "y">]><tv></tv>', encoding="utf-8")
    with pytest.raises(RuntimeError, match="DTD"):
        audit_xmltv_coverage(guide, [])
    guide.write_text('<tv><channel id="one"/><programme channel="one"/></tv>', encoding="utf-8")
    with pytest.raises(RuntimeError, match="elementos"):
        audit_xmltv_coverage(guide, [], max_elements=1)


def test_xmltv_owned_download_stop(tmp_path, monkeypatch):
    _exercise_owned_download(tmp_path, monkeypatch, "stop")


def test_xmltv_owned_download_expire(tmp_path, monkeypatch):
    _exercise_owned_download(tmp_path, monkeypatch, "expire")


def test_xmltv_owned_download_complete(tmp_path, monkeypatch):
    _exercise_owned_download(tmp_path, monkeypatch, "complete")


def _exercise_owned_download(tmp_path, monkeypatch, action):
    import pytest

    started, release, cancel = (threading.Event() for _ in range(3))

    class DelayedGuide(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<tv>")
            self.wfile.flush()
            started.set()
            release.wait(8)
            try:
                self.wfile.write(b"</tv>")
            except OSError:
                pass

        def log_message(self, *args):
            pass

    for key in ("M3U_OUTBOUND_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("M3U_PROFILE", "development")
    monkeypatch.setenv("M3U_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setenv("NO_PROXY", "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), DelayedGuide)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    def control_download():
        if not started.wait(5):
            return
        if action == "stop":
            cancel.set()
        elif action == "complete":
            release.set()

    stopper = threading.Thread(target=control_download)
    stopper.start()
    destination = tmp_path / "guide.xml"
    destination.write_bytes(b"<tv><!-- previous --></tv>")
    try:
        url = f"http://127.0.0.1:{server.server_port}/guide"
        if action == "complete":
            result = download_xmltv(url, destination, max_age_seconds=0, cancel_event=cancel)
            assert result == {"cached": False, "path": str(destination), "bytes": 9, "compressed_source": False}
            assert destination.read_bytes() == b"<tv></tv>"
            cached = download_xmltv("https://not-contacted.example.invalid/guide", destination, cancel_event=cancel)
            assert cached == {"cached": True, "path": str(destination), "bytes": 9}
        else:
            error = NetworkCancelled if action == "stop" else NetworkDeadlineExceeded
            deadline = time.monotonic() + (12 if action == "stop" else 3)
            with pytest.raises(error):
                download_xmltv(url, destination, max_age_seconds=0, cancel_event=cancel, deadline=deadline)
            assert destination.read_bytes() == b"<tv><!-- previous --></tv>"
        assert started.is_set()
        assert set(tmp_path.iterdir()) == {destination}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join()
        stopper.join()


def test_xmltv_deadline_before_download_leaves_output_absent(tmp_path):
    import pytest

    destination = tmp_path / "guide.xml"
    with pytest.raises(NetworkDeadlineExceeded):
        download_xmltv("https://example.invalid/guide", destination, deadline=time.monotonic() - 1)
    assert list(tmp_path.iterdir()) == []


def test_xmltv_cancelled_after_worker_stages_result(tmp_path, monkeypatch):
    import pytest

    cancel = threading.Event()
    def finish_worker(operation, payload, **kwargs):
        assert operation == "xmltv" and kwargs["cancel_event"] is cancel
        Path(payload["destination"]).write_bytes(b"<tv/>")
        cancel.set()
        return {}

    monkeypatch.setattr(channel_metadata, "run_operation", finish_worker)
    with pytest.raises(NetworkCancelled):
        download_xmltv("https://example.invalid/guide", tmp_path / "guide.xml", cancel_event=cancel)
    assert list(tmp_path.iterdir()) == []


def test_cancelled_local_metadata_stops_before_io(tmp_path):
    import pytest

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(NetworkCancelled):
        load_registry(tmp_path / "missing.json", cancel_event=cancel)
    with pytest.raises(NetworkCancelled):
        enrich_entries([], {}, cancel_event=cancel)
    with pytest.raises(NetworkCancelled):
        audit_xmltv_coverage(tmp_path / "missing.xml", [], cancel_event=cancel)


def test_metadata_cli_sigint_during_xmltv_preserves_output(tmp_path, monkeypatch, capsys):
    import signal
    from tools import enrich_m3u_metadata as cli

    playlist = tmp_path / "input.m3u"
    playlist.write_text('#EXTM3U\n#EXTINF:-1,Canal\nhttp://example.invalid/test\n', encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schema": 2, "epg_url": "https://example.invalid/guide",
                                    "channels": []}), encoding="utf-8")
    output, guide = tmp_path / "result.m3u", tmp_path / "guide.xml"
    output.write_bytes(b"previous M3U")
    guide.write_bytes(b"previous XMLTV")
    previous = signal.getsignal(signal.SIGINT)

    def interrupt_worker(op, payload, *, cancel_event, deadline):
        assert op == "xmltv"
        # Synchronously dispatch SIGINT to the scoped handler; never signal the host group.
        signal.raise_signal(signal.SIGINT)
        assert cancel_event.is_set()
        Path(payload["destination"]).write_bytes(b"<tv/>")
        return {"bytes": 5}

    monkeypatch.setattr(channel_metadata, "run_operation", interrupt_worker)
    code = cli.main(["--input", str(playlist), "--output", str(output),
                     "--registry", str(registry), "--xmltv-output", str(guide), "--refresh-hours", "0"])
    assert code == 130
    assert "cancel" in capsys.readouterr().err.lower()
    assert output.read_bytes() == b"previous M3U" and guide.read_bytes() == b"previous XMLTV"
    assert signal.getsignal(signal.SIGINT) is previous
    assert not list(tmp_path.glob(".xmltv-*"))


def test_metadata_cli_sigint_after_assembly_prevents_publication(tmp_path, monkeypatch):
    import signal
    from tools import enrich_m3u_metadata as cli

    playlist = tmp_path / "input.m3u"
    playlist.write_text('#EXTM3U\n#EXTINF:-1,Canal\nhttp://example.invalid/test\n', encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text('{"schema":2,"channels":[]}', encoding="utf-8")
    output = tmp_path / "result.m3u"
    argv = ["--input", str(playlist), "--registry", str(registry), "--output", str(output)]
    assert cli.main(argv) == 0
    assert output.read_bytes().startswith(b"\xef\xbb\xbf#EXTM3U")
    output.write_bytes(b"previous result")
    build = cli.build_m3u

    def interrupted(*args, **kwargs):
        text = build(*args, **kwargs)
        signal.raise_signal(signal.SIGINT)
        return text

    monkeypatch.setattr(cli, "build_m3u", interrupted)
    assert cli.main(argv) == 130
    assert output.read_bytes() == b"previous result"
