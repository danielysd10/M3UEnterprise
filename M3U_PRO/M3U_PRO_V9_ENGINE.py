"""Compatibility shim for the canonical Enterprise engine.

The supported implementation lives at the package root. This module preserves
legacy imports from ``M3U_PRO`` without maintaining a second copy of the engine.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_impl = importlib.import_module("M3U_PRO_V9_ENGINE")

for _name in getattr(_impl, "__all__", ()):
    globals()[_name] = getattr(_impl, _name)

# Legacy code may access non-__all__ names; module-level __getattr__ forwards them.
def __getattr__(name: str):
    return getattr(_impl, name)
