from pathlib import Path
import tempfile
import threading

import M3U_PRO_V9_ENGINE as engine
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise
from checkpoint_store import CheckpointStore


def test_checkpoint_cache_expires_failures_before_successes():
    with tempfile.TemporaryDirectory() as tmp:
        with CheckpointStore(Path(tmp), "a" * 64) as store:
            store.put_many([
                ("success", {"ok": True, "status": "VALID"}),
                ("failure", {"ok": False, "status": "TIMEOUT"}),
            ])
            fresh, expired = store.get_many_fresh(
                ["success", "failure"],
                success_ttl_seconds=60,
                failure_ttl_seconds=5,
                now=__import__("time").time() + 10,
            )
            assert fresh == {"success": {"ok": True, "status": "VALID"}}
            assert expired == 1


def test_expired_checkpoint_result_is_revalidated():
    original_base = enterprise.CHECKPOINT_BASE_DIR
    original_success_ttl = enterprise.VALIDATION_SUCCESS_CACHE_TTL_SECONDS
    calls = {"count": 0}
    with tempfile.TemporaryDirectory() as tmp:
        enterprise.CHECKPOINT_BASE_DIR = Path(tmp)
        enterprise.VALIDATION_SUCCESS_CACHE_TTL_SECONDS = 0
        try:
            def fake_probe(entry, timeout=6, **_kwargs):
                calls["count"] += 1
                return {"ok": True, "status": "VALID", "reason": "fixture", "latency_ms": 1, "streams": [{"codec_type": "audio"}]}
            original_probe = enterprise.enterprise_ffprobe
            enterprise.enterprise_ffprobe = fake_probe
            entries = [{"name": "Canal", "url": "http://fixture.test/audio", "directives": []}]
            cancel = threading.Event()
            enterprise.enterprise_validate_all(entries, True, 3, 1, None, cancel, "b" * 64)
            enterprise.enterprise_validate_all(entries, True, 3, 1, None, cancel, "b" * 64)
            assert calls["count"] == 2
            assert enterprise.enterprise_validate_all.last_metrics["checkpoint_expired"] == 1
        finally:
            enterprise.enterprise_ffprobe = original_probe
            enterprise.CHECKPOINT_BASE_DIR = original_base
            enterprise.VALIDATION_SUCCESS_CACHE_TTL_SECONDS = original_success_ttl


def test_favorite_scheduler_has_bounded_inflight_work():
    original_probe = engine.probe_favorite_quality
    original_ffprobe = engine.ffprobe_path
    try:
        engine.ffprobe_path = lambda: "fixture-ffprobe"
        engine.probe_favorite_quality = lambda entry, timeout=15, limiter=None: {
            "ok": True, "latency_ms": 1, "width": 0, "height": 0,
            "video_bitrate": 0, "audio_bitrate": 0, "video_codec": "",
            "audio_codec": "aac", "score": 1, "reason": "fixture",
        }
        favorites = [
            {"name": f"Canal {i}", "group": "00 - Favoritos Noticias", "url": f"http://fixture.test/{i}", "directives": []}
            for i in range(40)
        ]
        specs = [{"type": "name", "name": entry["name"], "group": entry["group"]} for entry in favorites]
        engine.optimize_favorites(favorites, specs, "00 - Favoritos Noticias", workers=3, repetitions=1)
        assert engine.optimize_favorites.last_metrics["max_in_flight"] == 6
        assert engine.optimize_favorites.last_metrics["completed"] == 40
    finally:
        engine.probe_favorite_quality = original_probe
        engine.ffprobe_path = original_ffprobe
