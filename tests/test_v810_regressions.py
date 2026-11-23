from pathlib import Path
import sys, tempfile, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import M3U_PRO_V9_ENGINE as e
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent


def test_screen_case_legacy_unheaded_favorites_are_migrated_before_resolve():
    text = '''Win Sports\nWin Sports+\nEl Gourmet\nElGourmet\nFood Network\nCocina Familiar\nDiscovery Home & Health\n'''
    specs = e.parse_favorite_lines(text)
    assert specs
    groups = [x['group'] for x in specs]
    assert all(g in e.FAVORITE_CATEGORY_ORDER for g in groups)
    assert '00 - FAVORITOS' not in groups
    assert groups[0] == '00 - Favoritos Deportes'


def test_legacy_header_is_not_a_category_and_is_auto_migrated():
    specs = e.parse_favorite_lines('[00 - FAVORITOS]\nWin Sports\nCNN [USA]\n')
    assert [x['group'] for x in specs] == [
        '00 - Favoritos Deportes',
        '00 - Favoritos Noticias',
    ]


def test_strict_brackets_are_content_not_categories():
    specs = e.parse_favorite_lines('[00 - Favoritos Deportes]\nESPN [HD]\nESPN [4K]\nCNN [USA]\n')
    assert all(x['group'] == '00 - Favoritos Deportes' for x in specs)
    assert [x['name'] for x in specs] == ['ESPN [HD]', 'ESPN [4K]', 'CNN [USA]']


def test_completed_checkpoint_is_reusable_after_post_validation_failure():
    with tempfile.TemporaryDirectory() as td:
        old = ent.CHECKPOINT_BASE_DIR
        ent.CHECKPOINT_BASE_DIR = Path(td)
        try:
            job = 'c' * 64
            p, tmp = ent._checkpoint_paths(job)
            ent.CHECKPOINT_FILE, ent.CHECKPOINT_TMP = p, tmp
            ent._save_checkpoint({
                'job_id': job,
                'results': {'fp1': {'ok': True}},
                'updated': time.time(),
                'completed': True,
            })
            loaded = ent._load_checkpoint(job)
            assert loaded['completed'] is True
            assert loaded['results']['fp1']['ok'] is True
            assert loaded.get('reused_completed_checkpoint') is True
        finally:
            ent.CHECKPOINT_BASE_DIR = old


def test_favorite_order_after_legacy_migration_is_valid():
    specs = e.parse_favorite_lines('CNN\nWin Sports\nCaracol\n')
    # resolve only, with one matching source per favorite
    active = []
    for i, spec in enumerate(specs):
        name = spec['name']
        group = spec['group']
        active.append({'name': name, 'group': group, 'extinf': f'#EXTINF:0 group-title="{group}",{name}', 'directives': [], 'url': f'http://x/{i}', 'source':'test'})
    favorites, missing = e.resolve_favorites(specs, active)
    favorites = e.order_favorites_by_category_and_channel(favorites)
    assert not missing
    ok, seen, expected = e.validate_favorite_order(favorites)
    assert ok, (seen, expected)


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
    print('LEGACY REGRESSION TESTS OK')
