from pathlib import Path
import json, os, tempfile, time, zipfile, stat
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import M3U_PRO_V9_ENGINE as e
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent
from enterprise_favorites import CATEGORIES, migrate_favorites_directory, validate_catalog_file, parse_catalog


def _fake_app():
    app = object.__new__(ent.EnterpriseApp)
    app.cancel_event = ent.MergeCancellation()
    app.progress_started = None
    app._progress = lambda *a: None
    app._set_running = lambda *a: None
    app.after = lambda _delay, fn: fn()
    app._finish_success = lambda *a: setattr(app, "success", True)
    app._finish_error = lambda exc, tb: setattr(app, "error", exc)
    app.output_var = type("V", (), {"get": lambda self: ""})()
    return app


def test_legacy_header_variants_and_strict_content():
    legacy_variants = [
        "00 - FAVORITOS\nWin Sports\nCNN\n",
        "[00 - FAVORITOS]\nWin Sports\nCNN\n",
        "[Favoritos]\nWin Sports\nCNN\n",
    ]
    for text in legacy_variants:
        specs = e.parse_favorite_lines(text)
        assert all(s['group'] in CATEGORIES for s in specs), specs
        assert not any(s['name'].casefold() == '00 - favoritos' for s in specs)
    strict = e.parse_favorite_lines('[00 - Favoritos Deportes]\nFOX Sports [HD]\n[4K]\nUSA Channel [USA]\n')
    assert [s['group'] for s in strict] == [CATEGORIES[3]] * 3
    assert [s['name'] for s in strict] == ['FOX Sports [HD]', '[4K]', 'USA Channel [USA]']


def test_preflight_is_early_and_resolves_raw_candidates():
    entries = [{"name":"Win Sports","url":"http://x/w.m3u8"}]
    specs, missing = ent.preflight_favorites('00 - FAVORITOS\nWin Sports\nCNN\n', entries)
    assert all(s['group'] in CATEGORIES for s in specs)
    assert any(s['name'] == 'CNN' for s in missing)


def test_checkpoint_completion_reuse_without_reprobe():
    original_probe = ent.enterprise_ffprobe
    original_base = ent.CHECKPOINT_BASE_DIR
    original_file = ent.CHECKPOINT_FILE
    original_tmp = ent.CHECKPOINT_TMP
    calls = {'n': 0}
    with tempfile.TemporaryDirectory() as td:
        ent.CHECKPOINT_BASE_DIR = Path(td)
        try:
            def fake_probe(entry, timeout=6, **kwargs):
                calls['n'] += 1
                return {'ok': True, 'reason': 'fake ok', 'latency_ms': 1, 'streams': [{'codec_type':'video'}]}
            ent.enterprise_ffprobe = fake_probe
            entries = [{'name':f'C{i}','url':f'http://host/{i}.m3u8','directives':[],'extinf':f'#EXTINF:0 group-title="General",C{i}','group':'General'} for i in range(7)]
            cancel = __import__('threading').Event()
            job='9'*64
            kept, dead, _ = ent.enterprise_validate_all(entries, True, 3, 2, None, cancel, job)
            assert len(kept)==7 and not dead and calls['n']==7
            # Simulate a failure after validation/publication staging; next run must reuse cache.
            cp = ent._load_checkpoint(job)
            assert cp.get('completed') is True and cp.get('stage') == 'VALIDATION_COMPLETE'
            kept2, dead2, _ = ent.enterprise_validate_all(entries, True, 3, 2, None, cancel, job)
            assert len(kept2)==7 and not dead2 and calls['n']==7
        finally:
            ent.enterprise_ffprobe = original_probe
            ent.CHECKPOINT_BASE_DIR = original_base
            ent.CHECKPOINT_FILE = original_file
            ent.CHECKPOINT_TMP = original_tmp


def test_catalog_self_heals_and_backs_up_legacy():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        legacy='00 - FAVORITOS\nWin Sports [HD]\nCNN\n\n'
        (root/'favoritos.txt').write_text(legacy, encoding='utf-8')
        result=migrate_favorites_directory(root)
        assert result['migrated']
        assert (root/'favoritos_maestro.txt').exists()
        assert (root/'favoritos.txt.legacy.bak').exists()
        v=validate_catalog_file(root/'favoritos_maestro.txt')
        assert v['ok'], v['issues']
        # Corrupt master and require self-healing from source/legacy backup path.
        (root/'favoritos_maestro.txt').write_text('[00 - Favoritos Noticias]\nCNN\n', encoding='utf-8')
        (root/'favoritos.txt').write_text(legacy, encoding='utf-8')
        result2=migrate_favorites_directory(root, force=False)
        assert validate_catalog_file(root/'favoritos_maestro.txt')['ok']


def test_group_sync_and_final_structure():
    entries=[{
        'name':'Canal','group':' Noticias ','extinf':'#EXTINF:0 group-title="NOTICIAS",Canal',
        'directives':['#EXTGRP:Musica','#EXTGRP:Noticias'], 'url':'http://x/a.m3u8'
    }]
    synced=e.synchronize_group_metadata(entries)
    assert synced[0]['group']=='Noticias'
    assert synced[0]['extinf'].count('group-title="Noticias"')==1
    assert synced[0]['directives'].count('#EXTGRP:Noticias')==1
    text=e.build_m3u(synced)
    issues=e.audit_generated_m3u(text)
    assert not issues, issues


def test_mojibake_category_cleanup():
    assert e.normalize_source_category('Musica') == 'Música'
    assert e.normalize_source_category('  Música') == 'Música'
    assert e.normalize_source_category('NOTICIAS') == 'Noticias'
    assert e.normalize_source_category(' Noticias ') == 'Noticias'


def test_exact_four_sources_hard_limit():
    entries=[]
    for i in range(9):
        entries.append({'name':'ESPN','group':CATEGORIES[3],'url':f'http://x/{i}.m3u8','extinf':f'#EXTINF:0 group-title="{CATEGORIES[3]}",ESPN'})
    out=e.enforce_favorite_source_limit(entries,4)
    assert len(out)==4
    assert len({x['url'] for x in out})==4


def test_zip_security_traversal_and_symlink():
    with tempfile.TemporaryDirectory() as td:
        td=Path(td); install=td/'ff'
        z=td/'bad.zip'
        with zipfile.ZipFile(z,'w') as f: f.writestr('../escape.txt','x')
        try: e._extract_ffmpeg_zip(z,install)
        except RuntimeError: pass
        else: raise AssertionError('Traversal accepted')
        z2=td/'symlink.zip'
        info=zipfile.ZipInfo('evil')
        info.create_system=3
        info.external_attr=(stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(z2,'w') as f: f.writestr(info,'../../escape')
        try: e._extract_ffmpeg_zip(z2,install)
        except RuntimeError: pass
        else: raise AssertionError('Symlink accepted')


def test_full_worker_preflight_then_validation_and_publish():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src.m3u"
        src.write_text("#EXTM3U\n#EXTINF:0 group-title=\"Deportes\",Win Sports\n#EXTGRP:Deportes\nhttp://x/w.m3u8\n", encoding="utf-8")
        snap = {
            "files": [str(src)], "manual": "", "favorites": "00 - FAVORITOS\nWin Sports\n",
            "favorite_group": "00 - FAVORITOS", "validate": True, "deep": True, "timeout": 3,
            "workers": 2, "fav_quality": False, "fav_sources": 4, "fav_reps": 5,
            "fav_interval": 0, "fav_fast": True, "fav_top": True, "output": str(root / "out.m3u")
        }
        app = _fake_app(); calls = {"validator": 0}
        ov, ot, op = ent.enterprise_validate_all, ent.test_ffprobe, ent.ffprobe_path
        original_base = ent.CHECKPOINT_BASE_DIR
        ent.CHECKPOINT_BASE_DIR = root / "state"
        try:
            def fake_validate(entries, *args, **kwargs):
                calls["validator"] += 1
                return entries, [], {}
            ent.enterprise_validate_all = fake_validate
            ent.test_ffprobe = lambda path: (True, "fake")
            ent.ffprobe_path = lambda: "fake"
            app._merge_worker(snap)
            assert getattr(app, "success", False), getattr(app, "error", None)
            assert calls["validator"] == 1
            out = (root / "out.m3u").read_text(encoding="utf-8-sig")
            assert "00 - Favoritos Deportes" in out
        finally:
            ent.enterprise_validate_all, ent.test_ffprobe, ent.ffprobe_path = ov, ot, op
            ent.CHECKPOINT_BASE_DIR = original_base


def test_full_worker_without_validation_publishes_clean_m3u():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src.m3u"
        src.write_text("#EXTM3U\n#EXTINF:0 group-title=\"Musica\",Canal\n#EXTGRP:NOTICIAS\nhttp://x/c.m3u8\n", encoding="utf-8")
        snap = {
            "files": [str(src)], "manual": "", "favorites": "", "favorite_group": "00 - FAVORITOS",
            "validate": False, "deep": False, "timeout": 3, "workers": 2, "fav_quality": False,
            "fav_sources": 4, "fav_reps": 5, "fav_interval": 0, "fav_fast": True, "fav_top": True,
            "output": str(root / "out.m3u")
        }
        app = _fake_app()
        original_base = ent.CHECKPOINT_BASE_DIR
        ent.CHECKPOINT_BASE_DIR = root / "state"
        try:
            app._merge_worker(snap)
            assert getattr(app, "success", False), getattr(app, "error", None)
            out = (root / "out.m3u").read_text(encoding="utf-8-sig")
            assert 'group-title="Música"' in out
            assert '#EXTGRP:Música' in out
        finally:
            ent.CHECKPOINT_BASE_DIR = original_base


def test_enterprise_report_uses_enterprise_validation_reason_and_metrics():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        report = root / "report.txt"
        dead = [{
            "name": "Canal caido",
            "group": "General",
            "source": "test",
            "url": "http://x/dead.m3u8",
            "validation_enterprise": {
                "ok": False,
                "status": "TIMEOUT",
                "reason": "FFprobe timeout > 3s",
                "latency_ms": 3000,
            },
        }]
        e.save_report(
            report, [("src.m3u", 1)], 0, 1, 0, dead, [], [], True,
            validation_metrics={
                "mode": "FFprobe profundo",
                "workers": 2,
                "input_entries": 1,
                "unique_fingerprints": 1,
                "pending_before_run": 1,
                "checkpoint_hits": 0,
                "elapsed_seconds": 3.0,
                "statuses": {"TIMEOUT": 1},
                "avg_latency_ms": 3000,
                "max_latency_ms": 3000,
            },
            pipeline_metrics={
                "total_seconds": 3.5,
                "stages": {"validacion": 3.0},
                "input_entries": 1,
                "unique_candidates": 1,
                "exact_url_duplicates": 0,
                "active_entries": 0,
                "dead_entries": 1,
                "favorites": 0,
            },
        )
        text = report.read_text(encoding="utf-8")
        assert "MÉTRICAS DEL PIPELINE" in text
        assert "MÉTRICAS DE VALIDACIÓN" in text
        assert "TIMEOUT: FFprobe timeout > 3s" in text
