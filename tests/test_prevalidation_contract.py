from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

import M3U_PRO_V9_ENGINE as engine
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent
from M3U_PRO_REPARADOR import repair_many
from M3U_PRO_VALIDATOR import validate_file
from prevalidation_manifest import MANIFEST_FILENAME, verify_manifest_for_file


def _write_sample(path: Path) -> None:
    path.write_text(
        "#EXTM3U\n"
        '#EXTINF:0 group-title="General",Good\n'
        "http://example.com/good\n"
        '#EXTINF:0 group-title="General",Repair\n'
        "hhttps://example.com/repair\n"
        '#EXTINF:0 group-title="General",Bad\n'
        "http://example.com:notaport/live\n",
        encoding="utf-8",
    )


def test_validator_repairer_manifest_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "source.m3u"
        out = root / "clean"
        _write_sample(src)
        validation = validate_file(src)
        assert validation["url_counts"]["VALID"] == 1
        assert validation["url_counts"]["REPAIRABLE"] == 1
        assert validation["url_counts"]["INVALID"] == 1

        result = repair_many([src], out, allow_partial=True, backup=False)
        row = result["results"][0]
        assert row["prevalidation_url_counts"]["VALID"] == 1
        assert row["prevalidation_url_counts"]["REPAIRABLE"] == 1
        assert row["prevalidation_url_counts"]["INVALID"] == 1
        manifest = Path(result["manifest"])
        assert manifest.name == MANIFEST_FILENAME
        clean = out / src.name
        context = verify_manifest_for_file(clean, manifest)
        assert context.verified is True
        assert context.reason == "VERIFIED"


def test_sha_mismatch_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "source.m3u"
        out = root / "clean"
        _write_sample(src)
        result = repair_many([src], out, allow_partial=True, backup=False)
        clean = out / src.name
        manifest = Path(result["manifest"])
        clean.write_text(clean.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        context = verify_manifest_for_file(clean, manifest)
        assert context.verified is False
        assert context.reason == "SHA256_MISMATCH"


def test_manifest_missing_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        clean = Path(td) / "clean.m3u"
        clean.write_text("#EXTM3U\n#EXTINF:0 group-title=\"General\",X\nhttp://example.com/x\n", encoding="utf-8")
        context = verify_manifest_for_file(clean)
        assert not context.verified
        assert context.reason == "MANIFEST_MISSING"


def test_prevalidated_skips_policy_but_keeps_ffprobe():
    calls = {"policy": 0, "probe": 0}
    original_policy = ent.apply_url_policy
    original_probe = ent.enterprise_ffprobe
    original_base = ent.CHECKPOINT_BASE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ent.CHECKPOINT_BASE_DIR = Path(td)
            def fail_policy(entry):
                calls["policy"] += 1
                raise AssertionError("URL policy must not run for certified entry")
            def fake_probe(entry, timeout=6, **kwargs):
                calls["probe"] += 1
                assert kwargs.get("prevalidated") is True
                return {"ok": True, "reason": "fake ffprobe", "latency_ms": 1, "streams": [{"codec_type": "video"}]}
            ent.apply_url_policy = fail_policy
            ent.enterprise_ffprobe = fake_probe
            entry = {"name": "Good", "group": "General", "url": "http://example.com/live", "directives": [], "extinf": ""}
            entry["_prevalidated_verified"] = True
            kept, dead, _ = ent.enterprise_validate_all([entry], True, 2, 1, None, threading.Event(), "c" * 64)
            assert len(kept) == 1
            assert dead == []
            assert calls["policy"] == 0
            assert calls["probe"] == 1
    finally:
        ent.CHECKPOINT_BASE_DIR = original_base
        ent.apply_url_policy = original_policy
        ent.enterprise_ffprobe = original_probe


def test_normal_mode_still_uses_url_policy_before_probe():
    calls = {"policy": 0, "probe": 0}
    original_policy = ent.apply_url_policy
    original_probe = ent.enterprise_ffprobe
    original_base = ent.CHECKPOINT_BASE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ent.CHECKPOINT_BASE_DIR = Path(td)
            def policy(entry):
                calls["policy"] += 1
                return original_policy(entry)
            def fake_probe(entry, timeout=6, **kwargs):
                calls["probe"] += 1
                return {"ok": True, "reason": "fake ffprobe", "latency_ms": 1, "streams": [{"codec_type": "video"}]}
            ent.apply_url_policy = policy
            ent.enterprise_ffprobe = fake_probe
            entry = {"name": "Good", "group": "General", "url": "http://example.com/live", "directives": [], "extinf": ""}
            kept, dead, _ = ent.enterprise_validate_all([entry], True, 2, 1, None, threading.Event(), "d" * 64)
            assert len(kept) == 1 and dead == []
            assert calls["policy"] >= 1
            assert calls["probe"] == 1
    finally:
        ent.CHECKPOINT_BASE_DIR = original_base
        ent.apply_url_policy = original_policy
        ent.enterprise_ffprobe = original_probe


def test_manifest_corrupt_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clean = root / "clean.m3u"
        manifest = root / MANIFEST_FILENAME
        clean.write_text("#EXTM3U\n#EXTINF:0 group-title=\"General\",X\nhttp://example.com/x\n", encoding="utf-8")
        manifest.write_text("{not-json", encoding="utf-8")
        context = verify_manifest_for_file(clean, manifest)
        assert not context.verified and context.reason == "MANIFEST_CORRUPT"


def test_manifest_incompatible_version_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clean = root / "clean.m3u"
        manifest = root / MANIFEST_FILENAME
        clean.write_text("#EXTM3U\n#EXTINF:0 group-title=\"General\",X\nhttp://example.com/x\n", encoding="utf-8")
        manifest.write_text(json.dumps({"manifest_version": "99.0", "contract": "M3U_PREVALIDATION"}), encoding="utf-8")
        context = verify_manifest_for_file(clean, manifest)
        assert not context.verified and context.reason == "MANIFEST_VERSION_OR_CONTRACT_UNSUPPORTED"


def test_prevalidated_mode_does_not_call_inspect_url_for_pipeline_entry():
    calls = {"inspect": 0, "probe": 0}
    original_inspect = ent.inspect_url
    original_probe = ent.enterprise_ffprobe
    original_base = ent.CHECKPOINT_BASE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ent.CHECKPOINT_BASE_DIR = Path(td)
            def fail_inspect(value):
                calls["inspect"] += 1
                raise AssertionError("inspect_url must not be called for certified pipeline entries")
            def fake_probe(entry, timeout=6, **kwargs):
                calls["probe"] += 1
                assert kwargs.get("prevalidated") is True
                return {"ok": True, "reason": "fake ffprobe", "latency_ms": 1, "streams": [{"codec_type": "video"}]}
            ent.inspect_url = fail_inspect
            ent.enterprise_ffprobe = fake_probe
            entry = {"name": "Good", "group": "General", "url": "http://example.com/live", "directives": [], "extinf": "", "_prevalidated_verified": True}
            kept, dead, _ = ent.enterprise_validate_all([entry], True, 2, 1, None, threading.Event(), "e" * 64)
            assert len(kept) == 1 and not dead
            assert calls["inspect"] == 0
            assert calls["probe"] == 1
    finally:
        ent.CHECKPOINT_BASE_DIR = original_base
        ent.inspect_url = original_inspect
        ent.enterprise_ffprobe = original_probe


def test_report_contains_explicit_prevalidation_evidence():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "report.txt"
        ent.save_report(
            path,
            [("clean.m3u", 1)],
            0,
            1,
            1,
            [],
            [{"name": "X", "group": "General", "url": "http://example.com/x", "validation_enterprise": {"ok": True, "status": "VALID", "reason": "ok"}}],
            [],
            True,
            validation_metrics={"mode": "FFprobe profundo", "workers": 1, "input_entries": 1, "unique_fingerprints": 1, "pending_before_run": 1, "checkpoint_hits": 0, "elapsed_seconds": 0.1, "statuses": {"VALID": 1}, "url_errors": 0, "repaired_entries": 0},
            pipeline_metrics={
                "prevalidation_requested": True,
                "prevalidation_manifest_verified": 1,
                "prevalidation_verified_entries": 1,
                "prevalidation_sha256_matches": 1,
                "prevalidation_sha256_mismatches": 0,
                "prevalidation_url_entries_skipped": 1,
                "prevalidation_fallback_count": 0,
                "prevalidation_fallbacks": {},
            },
        )
        text = path.read_text(encoding="utf-8")
        assert "PREVALIDACIÓN CERTIFICADA" in text
        assert "Archivos certificados (manifest + SHA-256): 1" in text
        assert "SHA-256 mismatches: 0" in text
        assert "Entradas con URL prevalidación omitida: 1" in text
        assert "Fallback: NONE" in text


def test_report_records_prevalidation_fallback_reason():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "report.txt"
        ent.save_report(
            path,
            [("clean.m3u", 1)],
            0,
            1,
            1,
            [],
            [],
            [],
            True,
            pipeline_metrics={
                "prevalidation_requested": True,
                "prevalidation_manifest_verified": 0,
                "prevalidation_verified_entries": 0,
                "prevalidation_sha256_matches": 0,
                "prevalidation_sha256_mismatches": 1,
                "prevalidation_url_entries_skipped": 0,
                "prevalidation_fallback_count": 1,
                "prevalidation_fallbacks": {"C:/clean.m3u": "SHA256_MISMATCH"},
            },
        )
        text = path.read_text(encoding="utf-8")
        assert "SHA-256 mismatches: 1" in text
        assert "Archivos en fallback: 1" in text
        assert "C:/clean.m3u -> SHA256_MISMATCH" in text


def test_manifest_summary_distinguishes_warnings_from_full_pass():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clean = root / "clean.m3u"
        clean.write_text("#EXTM3U\n#EXTINF:0 group-title=\"General\",X\nhttp://example.com/x\n", encoding="utf-8")
        manifest = root / MANIFEST_FILENAME
        from prevalidation_manifest import write_manifest, sha256_file
        write_manifest(
            root,
            validator_version="10.1.0",
            repairer_version="10.1.0",
            files=[{
                "source_file": "source.m3u", "clean_file": "clean.m3u",
                "source_sha256": "a" * 64, "clean_sha256": sha256_file(clean),
                "original_entries": 1, "final_entries": 1, "valid": 1,
                "repairable": 0, "repaired": 0, "invalid": 0, "unsupported": 0,
                "excluded": 0, "quarantined": 0,
                "processing_status": "PASS_WITH_WARNINGS",
                "validation_status": "PASS_WITH_WARNINGS",
            }],
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        assert payload["summary"]["status"] == "PASS_WITH_WARNINGS"
