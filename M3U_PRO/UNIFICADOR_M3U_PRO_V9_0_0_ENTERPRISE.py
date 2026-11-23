"""Compatibility launcher for the canonical Enterprise GUI entry point."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / "UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py"), run_name="__main__")
