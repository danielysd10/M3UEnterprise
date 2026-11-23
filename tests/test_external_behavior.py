from pathlib import Path
import inspect
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import M3U_PRO_V9_ENGINE as engine
from M3U_PRO_REPARADOR import repair_file
from M3U_PRO_VALIDATOR import validate


def test_source_categories_are_canonicalized():
    cases = {
        " Colombia ": "Colombia", "COLOMBIA": "Colombia",
        " Noticias ": "Noticias", "NOTICIAS": "Noticias",
        "Musica": "Música", "MÚSICA": "Música", " Música ": "Música",
        "Peliculas": "Películas", "PELÍCULAS": "Películas",
    }
    for raw, expected in cases.items():
        assert engine.normalize_source_category(raw) == expected


def test_group_title_and_extgrp_are_synchronized():
    entries = [{
        "name": "Baby TV", "group": "00 - Favoritos Infantil",
        "extinf": '#EXTINF:0 group-title="00 - Favoritos Infantil",Baby TV',
        "directives": ["#EXTGRP:Entretenimiento"], "url": "http://x/baby.m3u8",
    }]
    synchronized = engine.synchronize_group_metadata(entries)[0]
    assert synchronized["group"] == "00 - Favoritos Infantil"
    assert "#EXTGRP:00 - Favoritos Infantil" in synchronized["directives"]
    assert 'group-title="00 - Favoritos Infantil"' in synchronized["extinf"]


def test_favorite_probe_defaults_are_stable():
    assert inspect.signature(engine.optimize_favorites).parameters["max_sources"].default == 4
    assert inspect.signature(engine.optimize_favorites).parameters["repetitions"].default == 5
    assert inspect.signature(engine.assess_favorite_stability).parameters["repetitions"].default == 5


def test_repairer_publishes_valid_normalized_m3u():
    bad = """#EXTM3U
#EXTINF:0 group-title=" Entretenimiento ",Baby TV
#EXTGRP:Entretenimiento
http://x/baby.m3u8
#EXTINF:0 group-title=" MÚSICA ",Uno
#EXTGRP:Musica
http://x/uno.m3u8
#EXTINF:0 group-title="00 - favoritos Infantil",Canal infantil
#EXTGRP:Deportes
http://x/kids.m3u8
#EXTINF:0 group-title=" NOTICIAS ",Noticias Uno
#EXTGRP:NOTICIAS
http://x/news.m3u8
"""
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "bad.m3u"
        destination = Path(td) / "fixed.m3u"
        source.write_text(bad, encoding="utf-8")
        report = repair_file(source, destination)
        assert report["favorite_entries"] == 1
        validation = validate(destination)
        assert validation["ok"], validation["issues"]
        output = destination.read_text(encoding="utf-8-sig")
        assert output.find('group-title="00 - Favoritos Infantil"') < output.find('group-title="Entretenimiento"')
        assert 'group-title="Música"' in output
        assert 'group-title="Noticias"' in output
        assert "#EXTGRP:Música" in output
        assert "#EXTGRP:Noticias" in output
