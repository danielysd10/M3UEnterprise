"""Personal settings and legacy recovery using disposable user and installation trees."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

from runtime_state import default_checkpoint_dir
from user_settings import SettingsError, default_config_dir, import_legacy_settings


APP = 'unificador_m3u_config.json'
BLOCKS = 'bloques_urls.json'
LEGACY = 'favoritos.txt'
MASTER = 'favoritos_maestro.txt'
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('platform,root_key,expected', [
    ('win32', 'LOCALAPPDATA', 'M3UEnterprise/config'),
    ('linux', 'XDG_CONFIG_HOME', 'm3u-enterprise'),
    ('darwin', None, 'Library/Application Support/M3UEnterprise/config'),
])
def test_default_roots_are_per_user_and_resolution_is_pure(tmp_path, platform, root_key, expected):
    home = tmp_path / 'home'
    base = tmp_path / 'platform-config' if root_key else home
    environment = {root_key: str(base)} if root_key else {}
    assert default_config_dir(environ=environment, platform=platform, user_home=home) == base / expected
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('invalid', ['', 'relative/config'])
def test_invalid_override_does_not_fall_back_to_the_profile(tmp_path, invalid):
    with pytest.raises(SettingsError, match='M3U_CONFIG_DIR'):
        default_config_dir(environ={'M3U_CONFIG_DIR': invalid}, user_home=tmp_path)
    assert not list(tmp_path.iterdir())


def test_config_and_state_overrides_are_independent(tmp_path):
    config = tmp_path / 'config'
    state = tmp_path / 'runtime'
    environment = {'M3U_CONFIG_DIR': str(config), 'M3U_STATE_DIR': str(state)}
    assert default_config_dir(environ=environment) == config
    assert default_checkpoint_dir(environ=environment) == state / 'checkpoints'
    assert not list(tmp_path.iterdir())


def file_snapshot(directory):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in directory.iterdir() if path.is_file()}


@pytest.fixture
def launch(tmp_path):
    """Separate interpreter for each UI startup, with synthetic substitutes for the profile."""
    install = tmp_path / 'installation'
    caller = tmp_path / 'caller'
    install.mkdir()
    caller.mkdir()
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    for module in config['tool']['setuptools']['py-modules']:
        shutil.copyfile(ROOT / f'{module}.py', install / f'{module}.py')
    environment = dict(os.environ,
                       M3U_STATE_DIR=str(tmp_path / 'isolated-state'),
                       LOCALAPPDATA=str(tmp_path / 'local'), APPDATA=str(tmp_path / 'roaming'),
                       XDG_CONFIG_HOME=str(tmp_path / 'xdg-config'),
                       HOME=str(tmp_path / 'home'), USERPROFILE=str(tmp_path / 'home'))
    def run(code, override=True):
        env = environment.copy()
        if override:
            env['M3U_CONFIG_DIR'] = str(tmp_path / 'personal')
        else:
            env.pop('M3U_CONFIG_DIR', None)
        bootstrap = 'import sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n'
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap + code, str(install)],
                                env=env, cwd=caller, capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert result.returncode == 0, result.stderr
        return result.stdout
    return run


def test_gui_save_and_restart_are_isolated_from_installation_and_cwd(tmp_path, launch):
    install, caller = tmp_path / 'installation', tmp_path / 'caller'
    for directory in (install, caller):
        (directory / APP).write_text('{"which": "other install"}', encoding='utf-8')
        (directory / BLOCKS).write_text('{"foreign block": "synthetic"}', encoding='utf-8')
        (directory / LEGACY).write_text('Foreign-Catalog\n', encoding='utf-8')
    before = {directory: file_snapshot(directory) for directory in (install, caller)}
    personal = tmp_path / 'personal'
    launch('''
import M3U_PRO_V9_ENGINE as engine
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE
assert engine.load_app_config() == {}
assert engine.load_blocks() == {}
assert 'Foreign-Catalog' not in engine.load_favorites_text()
''')
    assert not personal.exists() and not (tmp_path / 'isolated-state').exists()
    launch('''
import M3U_PRO_V9_ENGINE as engine
engine.save_app_config({'favorite_group': 'Selección', 'metadata_scope': 'favorites'})
engine.save_blocks({'Prueba': 'https://fixture.invalid/1.ts'})
engine.save_favorites_text('[00 - Favoritos Noticias]\\nCanal Ñ\\n')
''')
    read_back = json.loads(launch('''
import json, M3U_PRO_V9_ENGINE as engine
print(json.dumps([engine.load_app_config(), engine.load_blocks(), engine.load_favorites_text()]))
'''))
    assert read_back[:2] == [{'favorite_group': 'Selección', 'metadata_scope': 'favorites'},
                              {'Prueba': 'https://fixture.invalid/1.ts'}]
    assert '[00 - Favoritos Noticias]\nCanal Ñ\n' in read_back[2]
    assert {APP, BLOCKS, MASTER} <= {path.name for path in personal.iterdir()}
    assert all(file_snapshot(directory) == snapshot for directory, snapshot in before.items())


def test_legacy_installation_can_be_read_without_changing_its_files(tmp_path, launch):
    install = tmp_path / 'installation'
    (install / APP).write_text('{"favorite_group":"Grupo existente"}', encoding='utf-8')
    (install / BLOCKS).write_text('{"existente":"sintético"}', encoding='utf-8')
    (install / LEGACY).write_text('Catálogo existente\n', encoding='utf-8')
    before = file_snapshot(install)
    launch('''
import M3U_PRO_V9_ENGINE as engine
assert engine.load_app_config()['favorite_group'] == 'Grupo existente'
assert engine.load_blocks()['existente'] == 'sintético'
assert 'Catálogo existente' in engine.load_favorites_text()
engine.save_app_config({'favorite_group': 'Propio'})
engine.save_favorites_text('Catálogo propio')
''', override=False)
    launch('''
import M3U_PRO_V9_ENGINE as engine
assert engine.load_app_config()['favorite_group'] == 'Propio'
assert 'Catálogo propio' in engine.load_favorites_text()
''', override=False)
    assert file_snapshot(install) == before


def test_import_preserves_existing_settings_and_private_source(tmp_path):
    source, destination = tmp_path / 'old', tmp_path / 'new'
    source.mkdir()
    destination.mkdir()
    (source / APP).write_text('{"favorite_group":"anterior"}', encoding='utf-8')
    (destination / APP).write_text('{"favorite_group":"actual"}', encoding='utf-8')
    private_value = 'https://fixture.invalid/stream?token=confidential-marker'
    (source / BLOCKS).write_text(json.dumps({'old': private_value}), encoding='utf-8')
    (source / LEGACY).write_text('Canal legado\n', encoding='utf-8-sig')
    before, personal_before = file_snapshot(source), file_snapshot(destination)
    first = import_legacy_settings(source, destination)
    assert first[APP] == 'conflict' and first[BLOCKS] == first[MASTER] == 'copied'
    assert private_value not in str(first)
    assert file_snapshot(destination)[APP] == personal_before[APP]
    assert json.loads((destination / BLOCKS).read_text(encoding='utf-8-sig')) == {'old': private_value}
    assert 'Canal legado' in (destination / MASTER).read_text(encoding='utf-8-sig')
    after_first = file_snapshot(destination)
    repeat = import_legacy_settings(source, destination)
    assert repeat[APP] == 'conflict' and repeat[BLOCKS] == repeat[MASTER] == 'unchanged'
    assert file_snapshot(destination) == after_first
    assert file_snapshot(source) == before


def test_divergent_source_catalogs_report_conflict_without_publishing(tmp_path):
    from enterprise_favorites import CATEGORIES, render_catalog
    source, destination = tmp_path / 'catalog', tmp_path / 'personal'
    source.mkdir()
    secret = 'confidential-marker'
    (source / LEGACY).write_text('Previous-channel\n', encoding='utf-8')
    (source / MASTER).write_text(render_catalog({CATEGORIES[0]: [secret]}), encoding='utf-8')
    previous = file_snapshot(source)
    result = import_legacy_settings(source, destination)
    assert result[MASTER] == 'conflict' and secret not in str(result)
    assert not (destination / MASTER).exists() and not (destination / LEGACY).exists()
    assert file_snapshot(source) == previous


def test_competing_imports_cannot_overwrite_the_first_complete_destination(tmp_path):
    folders = [tmp_path / 'first', tmp_path / 'second']
    destination = tmp_path / 'destination'
    for folder in folders:
        folder.mkdir()
        (folder / APP).write_text(json.dumps({'value': folder.name}), encoding='utf-8')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda folder: import_legacy_settings(folder, destination), folders))
    assert sorted(result[APP] for result in results) == ['conflict', 'copied']
    winner = next(folder for folder, result in zip(folders, results) if result[APP] == 'copied')
    assert json.loads((destination / APP).read_text(encoding='utf-8-sig')) == {'value': winner.name}


def test_comment_only_difference_is_a_conflict_without_disclosing_notes(tmp_path):
    source, destination = tmp_path / 'old', tmp_path / 'personal'
    source.mkdir()
    for name, note in [(MASTER, 'private note'), (LEGACY, 'other private note')]:
        (source / name).write_text(f'# {note}\n[00 - Favoritos Noticias]\nCanal\n', encoding='utf-8')
    before = file_snapshot(source)
    outcome = import_legacy_settings(source, destination)
    assert outcome == {MASTER: 'conflict'}
    assert not destination.exists()
    assert before == file_snapshot(source)


def test_import_of_single_flat_file_preserves_original_bytes_and_order(tmp_path):
    source, destination = tmp_path / 'old', tmp_path / 'personal'
    source.mkdir()
    content = 'Canal Ñ\r\nBBC News\r\nCanal Ñ\r\n'.encode('utf-8-sig')
    (source / LEGACY).write_bytes(content)
    assert import_legacy_settings(source, destination) == {MASTER: 'copied'}
    assert (destination / MASTER).read_bytes() == (source / LEGACY).read_bytes() == content


@pytest.mark.parametrize('previous', [b'', b'My selected favorite\n'])
def test_existing_personal_alias_cannot_be_shadowed_by_import(tmp_path, previous):
    source, destination = tmp_path / 'old', tmp_path / 'personal'
    source.mkdir()
    destination.mkdir()
    (destination / LEGACY).write_bytes(previous)
    (source / MASTER).write_text('Other favorite\n', encoding='utf-8')
    before = file_snapshot(destination)
    assert import_legacy_settings(source, destination) == {MASTER: 'conflict'}
    assert file_snapshot(destination) == before


def test_invalid_legacy_json_fails_before_publishing_any_file(tmp_path):
    source, destination = tmp_path / 'old', tmp_path / 'personal'
    source.mkdir()
    (source / APP).write_text('{"enabled": true}', encoding='utf-8')
    (source / BLOCKS).write_text('{"private-value', encoding='utf-8')
    before = file_snapshot(source)
    with pytest.raises(SettingsError, match='JSON') as exc:
        import_legacy_settings(source, destination)
    assert 'private-value' not in str(exc.value)
    assert not destination.exists() and file_snapshot(source) == before
