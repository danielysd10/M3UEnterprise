from pathlib import Path
import tempfile

from tools.run_live_validation_campaign import candidates
from url_safety import redact_url_for_export


def test_campaign_sampling_is_exact_url_deduplicated_and_host_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sample.m3u").write_text(
            "#EXTM3U\n"
            "https://one.example/a\nhttps://one.example/a\nhttps://one.example/b\n"
            "https://one.example/c\nhttps://two.example/a\nhttp://192.168.1.1/private\n",
            encoding="utf-8",
        )
        selected, stats = candidates(root, limit=10, per_host=2)
        assert len(selected) == 3
        assert {item["url"] for item in selected} == {"https://one.example/a", "https://one.example/b", "https://two.example/a"}
        assert stats["static_rejected"] == 1


def test_campaign_export_reference_never_retains_bearer_path_or_query():
    reference = redact_url_for_export("https://example.com/token/SECRET?access_token=VALUE")
    assert reference.startswith("https://example.com/[PATH_REDACTED]#sha256=")
    assert "SECRET" not in reference and "VALUE" not in reference


def test_campaign_ctrl_c_persists_partial_checkpoint_and_skips_final_metrics(tmp_path, monkeypatch):
    import json
    import signal
    import sqlite3
    import sys
    from contextlib import closing
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
    from tools import run_live_validation_campaign as campaign

    source = tmp_path / "input"
    source.mkdir()
    (source / "sample.m3u").write_text(
        "#EXTM3U\nhttps://test.invalid/one\nhttps://test.invalid/two\n", encoding="utf-8")
    output = tmp_path / "out"
    history = tmp_path / "history.sqlite3"
    monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS", raising=False)
    monkeypatch.setenv("M3U_PROFILE", "campaign")
    monkeypatch.setattr(sys, "argv", ["campaign", "--source", str(source), "--output", str(output),
                                     "--metrics-store", str(history), "--workers", "1"])
    monkeypatch.setattr(enterprise, "CHECKPOINT_BASE_DIR", tmp_path / "state")
    monkeypatch.setattr(enterprise, "enterprise_ffprobe",
                        lambda *a, **kw: {"ok": True, "reason": "synthetic", "latency_ms": 1})
    interrupted = []
    def interrupt_after_first_result(message, **kwargs):
        if message.startswith("progress=1/2") and not interrupted:
            interrupted.append(True)
            signal.raise_signal(signal.SIGINT)
    monkeypatch.setattr(campaign, "print", interrupt_after_first_result, raising=False)
    previous = signal.signal(signal.SIGINT, lambda *_: None)
    try:
        handler = signal.getsignal(signal.SIGINT)
        assert campaign.main() == 130
        assert signal.getsignal(signal.SIGINT) is handler
    finally:
        signal.signal(signal.SIGINT, previous)
    assert interrupted and not history.exists()
    assert not any((output / name).exists() for name in ("results.csv", "summary.json", "progress.json"))
    database, = (output / "checkpoint").rglob("*.sqlite3")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        meta = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM metadata")}
        assert meta["stage"] == "VALIDATION_INCOMPLETE" and not meta["completed"]
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1


def test_campaign_refuses_private_network_override_before_reading_source(tmp_path, monkeypatch):
    import sys
    import pytest
    from tools import run_live_validation_campaign as campaign

    output = tmp_path / "out"
    source = tmp_path / "missing-source"
    monkeypatch.setenv("M3U_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setattr(sys, "argv", ["campaign", "--source", str(source), "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        campaign.main()
    assert exc.value.code == 2 and not output.exists()
