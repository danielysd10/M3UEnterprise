from pathlib import Path
import sys, tempfile, inspect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from M3U_PRO_V9_ENGINE import (
    FAVORITE_CATEGORY_ORDER, normalize_source_category,
    synchronize_group_metadata, sort_favorite_entries,
    validate_favorite_order, parse_m3u, build_m3u,
    assess_favorite_stability, optimize_favorites,
)
from M3U_PRO_VALIDATOR import validate

def test_categories():
    cases = {
        ' Colombia ': 'Colombia', 'COLOMBIA': 'Colombia',
        'Noticias': 'Noticias', ' NOTICIAS ': 'Noticias',
        'Musica': 'Música', 'MÚSICA': 'Música', ' Música ': 'Música',
        'Peliculas': 'Películas', 'PELÍCULAS': 'Películas',
    }
    for raw, expected in cases.items():
        assert normalize_source_category(raw) == expected, (raw, normalize_source_category(raw))

def test_group_sync_and_output():
    entries = [
        {'name':'Canal 1','group':'00 - Favoritos Infantil','extinf':'#EXTINF:0 group-title="00 - Favoritos Infantil",Canal 1','directives':['#EXTGRP:Entretenimiento'],'url':'http://a'},
        {'name':'Canal 2','group':'Música','extinf':'#EXTINF:0 group-title="Música",Canal 2','directives':['#EXTGRP:Musica'],'url':'http://b'},
    ]
    synced = synchronize_group_metadata(entries)
    for e in synced:
        assert f'#EXTGRP:{e["group"]}' in e['directives']
        assert f'group-title="{e["group"]}"' in e['extinf']
    text = build_m3u(synced)
    assert '#EXTGRP:00 - Favoritos Infantil' in text
    assert '#EXTGRP:Música' in text

def test_favorite_order():
    entries=[]
    for i,g in enumerate(FAVORITE_CATEGORY_ORDER):
        entries.append({'name':g.split(' - ')[-1], 'group':g, 'url':f'http://{i}.m3u8', 'favorite_composite_score':100-i, 'favorite_stability_pct':100, 'favorite_latency_ms':10})
    ordered=sort_favorite_entries(entries)
    ok, seen, expected=validate_favorite_order(ordered)
    assert ok, (seen, expected)

def test_validator_catches_bad_metadata():
    bad = '#EXTM3U\n#EXTINF:0 group-title="Música",Uno\n#EXTGRP:Musica\nhttp://uno\n#EXTINF:0 group-title=" Musica ",Dos\n#EXTGRP:Musica\nhttp://dos\n'
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'bad.m3u'; p.write_text(bad, encoding='utf-8')
        r=validate(p)
        assert not r['ok']
        assert any('#EXTGRP desincronizado' in x for x in r['issues']) or any('Categoría duplicada' in x for x in r['issues'])

def test_stability_defaults():
    sig=inspect.signature(assess_favorite_stability)
    assert sig.parameters['repetitions'].default == 5
    sig2=inspect.signature(optimize_favorites)
    assert sig2.parameters['max_sources'].default == 4
    assert sig2.parameters['repetitions'].default == 5

def test_parse_canonicalizes_group():
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'input.m3u'
        p.write_text('#EXTM3U\n#EXTINF:0 group-title="MÚSICA",Test\n#EXTGRP:MUSICA\nhttp://x\n', encoding='utf-8')
        parsed=parse_m3u(p)
        assert parsed[0]['group']=='Música'

def main():
    test_categories(); test_group_sync_and_output(); test_favorite_order(); test_validator_catches_bad_metadata(); test_stability_defaults(); test_parse_canonicalizes_group()
    print('LEGACY COMPATIBILITY TESTS OK')

if __name__=='__main__': main()
