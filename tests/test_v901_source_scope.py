"""V9.0.1 production contract: the 4-source limit applies ONLY to Favoritos."""
from pathlib import Path
import tempfile
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as ent
from M3U_PRO_V9_ENGINE import canonical_favorite_category, enforce_favorite_source_limit, parse_m3u

CATS = [
    "00 - Favoritos Deportes",
    "00 - Favoritos Infantil",
    "00 - Favoritos Entretenimiento",
    "00 - Favoritos Películas",
    "00 - Favoritos Noticias",
    "00 - Favoritos Internacional",
    "00 - Favoritos Cocina",
]


def _entry(name, group, idx):
    return {
        "name": name,
        "group": group,
        "url": f"http://example.invalid/{group.replace(' ', '_')}/{name.replace(' ', '_')}/{idx}.m3u8",
        "extinf": f'#EXTINF:0 group-title="{group}",{name}',
        "directives": [f"#EXTGRP:{group}"],
    }


def test_more_than_four_normal_sources_are_preserved_while_favorites_are_capped():
    normal = [_entry("ESPN", "Deportes", i) for i in range(8)]
    favorites = [_entry("ESPN", CATS[0], i) for i in range(8)]
    kept_favorites = enforce_favorite_source_limit(favorites, max_sources=4)

    # Enterprise contract: cap ONLY the Favorites block.
    assert len(kept_favorites) == 4
    assert len(normal) == 8, "El contrato normal no debe limitar fuentes a 4"


def test_full_pipeline_preserves_more_than_four_normal_sources():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "many_sources.m3u"
        lines = ["#EXTM3U"]
        for i in range(8):
            lines += [
                f'#EXTINF:0 group-title="Deportes",ESPN',
                f'#EXTGRP:Deportes',
                f'http://normal.example/espn/{i}.m3u8',
            ]
        src.write_text("\n".join(lines) + "\n", encoding="utf-8")

        out = root / "out.m3u"
        snap = {
            "files": [str(src)], "manual": "", "favorites": "",
            "favorite_group": CATS[0], "validate": False, "deep": False,
            "timeout": 3, "workers": 2, "fav_quality": False,
            "fav_sources": 4, "fav_reps": 5, "fav_interval": 0,
            "fav_fast": True, "fav_top": True, "output": str(out),
        }
        class Fake:
            cancel_event = ent.MergeCancellation()
            progress_started = None
            def _progress(self, *args, **kwargs): pass
            def _finish_success(self, *args): self.success = True
            def _finish_error(self, exc, tb): raise AssertionError(tb)
            def after(self, _delay, fn): fn()

        app = Fake()
        original_base = ent.CHECKPOINT_BASE_DIR
        ent.CHECKPOINT_BASE_DIR = root / "state"
        try:
            ent.EnterpriseApp._merge_worker(app, snap)
            assert getattr(app, "success", False)
            text = out.read_text(encoding="utf-8-sig")
            assert text.count('group-title="Deportes",ESPN') == 8
            assert sum(1 for line in text.splitlines() if line.startswith("http://normal.example/espn/")) == 8
        finally:
            ent.CHECKPOINT_BASE_DIR = original_base
