from pathlib import Path

from tools.publish_playlist import publish


def test_publisher_writes_stable_path_and_manifest_atomically(tmp_path):
    source = Path(tmp_path) / "final.m3u"
    source.write_text("#EXTM3U\n#EXTINF:-1,Canal\nhttps://example.invalid/live.m3u8\n", encoding="utf-8")
    result = publish(source, Path(tmp_path) / "published", "https://iptv.example.test")
    target = Path(tmp_path) / "published" / "iptv" / "unificada.m3u"
    assert target.read_bytes() == source.read_bytes()
    assert result["playlist_url"] == "https://iptv.example.test/iptv/unificada.m3u"
    assert len(result["sha256"]) == 64


def test_publisher_rejects_non_m3u_input(tmp_path):
    source = Path(tmp_path) / "final.txt"
    source.write_text("#EXTM3U\n", encoding="utf-8")
    try:
        publish(source, Path(tmp_path) / "published")
    except RuntimeError as exc:
        assert "M3U" in str(exc)
    else:
        raise AssertionError("Debe rechazar una extensión no M3U")


def test_publisher_publishes_epg_atomically_and_rewrites_public_header(tmp_path):
    source = Path(tmp_path) / "final.m3u"
    guide = Path(tmp_path) / "guide.xml"
    source.write_text('#EXTM3U url-tvg="https://old.example/guide.xml"\n#EXTINF:-1,Canal\nhttps://example.invalid/live.m3u8\n', encoding="utf-8")
    guide.write_text('<tv></tv>\n', encoding="utf-8")
    result = publish(source, Path(tmp_path) / "published", "https://iptv.example.test", guide)
    published = Path(tmp_path) / "published" / "iptv"
    assert (published / "guia.xml").read_bytes() == guide.read_bytes()
    assert 'url-tvg="https://iptv.example.test/iptv/guia.xml"' in (published / "unificada.m3u").read_text(encoding="utf-8-sig")
    assert result["epg"]["sha256"] and result["epg_url"] == "https://iptv.example.test/iptv/guia.xml"
    assert '"epg"' in (published / "unificada.manifest.json").read_text(encoding="utf-8")
