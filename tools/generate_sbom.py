#!/usr/bin/env python3
"""Generate a minimal CycloneDX SBOM for the dependency-free runtime."""
from __future__ import annotations

import json
from pathlib import Path
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "SBOM.cdx.json"


def render(project: dict) -> bytes:
    """Render metadata without writing to the source checkout."""
    payload = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "application", "name": project["name"], "version": project["version"], "purl": f"pkg:pypi/{project['name']}@{project['version']}"}},
        "components": [{"type": "framework", "name": "Python Standard Library", "version": ">=3.13,<3.15", "scope": "required"}],
    }
    # Content-derived UUID keeps repeated builds deterministic while avoiding
    # reuse when the SBOM changes. Source revision is covered by the manifest.
    identity = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["serialNumber"] = uuid.uuid5(uuid.NAMESPACE_URL, identity).urn
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def generate(root: Path = ROOT) -> Path:
    root = Path(root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    output = root / "SBOM.cdx.json"
    output.write_bytes(render(project))
    return output


if __name__ == "__main__":
    print(generate())
