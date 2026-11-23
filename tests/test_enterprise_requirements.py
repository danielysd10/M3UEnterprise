from pathlib import Path
import sys, tempfile, zipfile, os, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enterprise_favorites import CATEGORIES, is_strict_category_header, migrate_favorites_directory, validate_catalog_file
import M3U_PRO_V9_ENGINE as e
from M3U_PRO_REPARADOR import repair_file
from M3U_PRO_VALIDATOR import validate


def test_strict_headers():
    assert is_strict_category_header('[00 - Favoritos Deportes]')
    for x in ('[HD]', '[4K]', '[USA]', '[FHD]', '[Opc.1]', '[SD]'):
        assert not is_strict_category_header(x), x
    specs = e.parse_favorite_lines('Canal [HD]\nCanal [4K] USA\n[00 - Favoritos Noticias]\nCNN')
    assert len(specs) == 3
    assert specs[0]['name'] == 'Canal [HD]'


def test_legacy_catalog_migration_and_backup():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root/'favoritos.txt'
        src.write_text('Caracol\nESPN [HD]\n[4K] HBO\nCNN News\n', encoding='utf-8')
        r = migrate_favorites_directory(root)
        assert r['migrated'] and r['legacy']
        assert (root/'favoritos_maestro.txt').exists()
        assert (root/'favoritos.txt.legacy.bak').exists()
        v = validate_catalog_file(root/'favoritos_maestro.txt')
        assert v['ok'], v
        text=(root/'favoritos_maestro.txt').read_text(encoding='utf-8')
        assert [x for x in CATEGORIES if ('['+x+']') in text] == list(CATEGORIES)
        assert '[HD]' not in [x.strip() for x in text.splitlines() if x.strip().startswith('[') and x.strip() != '[HD]']


def test_repair_00_favoritos_rescue():
    bad='''#EXTM3U\n#EXTINF:0 group-title="00 - FAVORITOS",Caracol\n#EXTGRP:00 - FAVORITOS\nhttp://a\n#EXTINF:0 group-title="00 - FAVORITOS",ESPN [HD]\n#EXTGRP:00 - FAVORITOS\nhttp://b\n#EXTINF:0 group-title="Musica",Uno\n#EXTGRP:Musica\nhttp://c\n'''
    with tempfile.TemporaryDirectory() as td:
        src=Path(td)/'legacy.m3u'; dst=Path(td)/'fixed.m3u'; src.write_text(bad,encoding='utf8')
        r=repair_file(src,dst); assert r['favorite_entries']==2
        out=dst.read_text(encoding='utf8')
        assert 'group-title="00 - Favoritos Colombia"' in out
        assert 'group-title="00 - Favoritos Deportes"' in out
        assert '00 - FAVORITOS' not in out
        assert validate(dst)['ok']


def test_validator_explicit_legacy():
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'legacy.m3u'
        p.write_text('#EXTM3U\n#EXTINF:0 group-title="00 - FAVORITOS",X\n#EXTGRP:00 - FAVORITOS\nhttp://x\n',encoding='utf8')
        r=validate(p)
        assert not r['ok']
        assert any('Favoritos legacy sin migrar' in x for x in r['issues'])


def test_four_source_invariant():
    entries=[]
    for i in range(8):
        entries.append({'name':'ESPN 1','group':'00 - Favoritos Deportes','url':f'http://x/{i}','extinf':f'#EXTINF:0 group-title="00 - Favoritos Deportes",ESPN 1','directives':['#EXTGRP:00 - Favoritos Deportes']})
    limited=e.enforce_favorite_source_limit(entries,4)
    assert len(limited)==4


def test_zip_symlink_and_traversal():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); out=root/'out'; z=root/'bad.zip'
        with zipfile.ZipFile(z,'w') as f:
            f.writestr('../escape.txt','bad')
        try: e._extract_ffmpeg_zip(z,out)
        except RuntimeError: pass
        else: raise AssertionError('traversal not blocked')
        z2=root/'link.zip'
        zi=zipfile.ZipInfo('link'); zi.external_attr=(0o120777 << 16)
        with zipfile.ZipFile(z2,'w') as f: f.writestr(zi,'/etc/passwd')
        try: e._extract_ffmpeg_zip(z2,out)
        except RuntimeError: pass
        else: raise AssertionError('symlink not blocked')


def test_checkpoint_isolated_paths():
    import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent
    with tempfile.TemporaryDirectory() as td:
        old=ent.CHECKPOINT_BASE_DIR
        ent.CHECKPOINT_BASE_DIR=Path(td)
        try:
            p1,t1=ent._checkpoint_paths('a'*64); p2,t2=ent._checkpoint_paths('b'*64)
            assert p1 != p2 and p1.parent == p2.parent
            ent.CHECKPOINT_FILE,ent.CHECKPOINT_TMP=p1,t1
            ent._save_checkpoint({'job_id':'a'*64,'results':{'x':1},'completed':False})
            assert ent._load_checkpoint('a'*64)['results']['x']==1
            assert not t1.exists()
            assert not p2.exists()
        finally: ent.CHECKPOINT_BASE_DIR=old


def test_five_stability_probes_exactly():
    original=e.probe_favorite_quality
    calls={'n':0}
    def fake(entry, timeout=e.DEFAULT_TIMEOUT, limiter=None):
        calls['n']+=1
        return {'ok': True, 'latency_ms': 10, 'width': 1920, 'height': 1080, 'video_bitrate': 4000000, 'audio_bitrate': 128000, 'video_codec':'h264','audio_codec':'aac','score':100,'reason':'mock'}
    e.probe_favorite_quality=fake
    try:
        r=e.assess_favorite_stability({'url':'http://mock'}, repetitions=5, interval=0)
        assert calls['n']==5 and r['attempts']==5 and r['success_count']==5 and r['stability_pct']==100.0
    finally:
        e.probe_favorite_quality=original


def test_categories_cleanup():
    for raw, expected in {' Musica ':'Música','MÚSICA':'Música',' Noticias ':'Noticias','NOTICIAS':'Noticias'}.items():
        assert e.normalize_source_category(raw)==expected

if __name__=='__main__':
    for name,v in sorted(globals().items()):
        if name.startswith('test_'):
            v()
    print('ENTERPRISE REQUIREMENTS OK')
