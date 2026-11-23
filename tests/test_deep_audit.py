from pathlib import Path
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import M3U_PRO_V9_ENGINE as engine
import UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE as enterprise


def test_large_parse_50000_entries():
    """Exercise parser scale as a named test, not during test collection."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "large.m3u"
        count = 50_000
        with path.open("w", encoding="utf-8") as handle:
            handle.write("#EXTM3U\n")
            for index in range(count):
                group = ["Colombia", "COLOMBIA", " Noticias ", "MÚSICA"][index % 4]
                handle.write(f'#EXTINF:-1 tvg-name="Canal {index}" group-title="{group}",Canal {index}\n')
                handle.write("#EXTGRP:" + group + "\n")
                handle.write(f"http://example.test/{index}.m3u8\n")
        started = time.perf_counter()
        items = engine.parse_m3u(path)
        elapsed = time.perf_counter() - started
        assert len(items) == count
        # Regression budget for the local parser only. Network/FFprobe are not
        # involved; the bound intentionally leaves room for slower CI agents.
        assert elapsed < 20, f"Parser throughput regression: {elapsed:.3f}s for {count} entries"
        print(f"LARGE PARSE OK: {count} entries in {elapsed:.3f}s")


def test_ffmpeg_zip_traversal_is_rejected():
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "bad.zip"
        install_dir = Path(td) / "out"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../escape.txt", "bad")
        try:
            engine._extract_ffmpeg_zip(archive, install_dir)
        except RuntimeError:
            pass
        else:
            raise AssertionError("ZIP traversal not blocked")


def test_checkpoint_save_load_is_atomic_and_isolated():
    with tempfile.TemporaryDirectory() as td:
        old_base = enterprise.CHECKPOINT_BASE_DIR
        old_file = enterprise.CHECKPOINT_FILE
        old_temp = enterprise.CHECKPOINT_TMP
        enterprise.CHECKPOINT_BASE_DIR = Path(td)
        try:
            job_id = "a" * 64
            path, temp_path = enterprise._checkpoint_paths(job_id)
            enterprise.CHECKPOINT_FILE = path
            enterprise.CHECKPOINT_TMP = temp_path
            enterprise._save_checkpoint({"job_id": job_id, "results": {"x": {"ok": True}}, "updated": time.time(), "completed": False})
            loaded = enterprise._load_checkpoint(job_id)
            assert loaded["results"]["x"]["ok"] is True
            assert path.exists()
            assert not temp_path.exists()
        finally:
            enterprise.CHECKPOINT_BASE_DIR = old_base
            enterprise.CHECKPOINT_FILE = old_file
            enterprise.CHECKPOINT_TMP = old_temp
