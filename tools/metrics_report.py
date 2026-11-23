#!/usr/bin/env python3
"""Render privacy-preserving campaign history from the aggregate metrics store."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def render(store: Path) -> str:
    connection = sqlite3.connect(store)
    try:
        runs = connection.execute("SELECT id,executed_utc,profile,total,succeeded,p50_ms,p95_ms FROM runs ORDER BY id DESC").fetchall()
        if not runs:
            return "# Métricas históricas\n\nNo hay campañas registradas.\n"
        latest = runs[0]
        providers = connection.execute("SELECT COUNT(*),SUM(total),SUM(succeeded) FROM provider_runs WHERE run_id=?", (latest[0],)).fetchone()
    finally:
        connection.close()
    when = datetime.fromtimestamp(latest[1], tz=timezone.utc).isoformat()
    success_rate = (latest[4] / latest[3] * 100.0) if latest[3] else 0.0
    return "\n".join([
        "# Métricas históricas de campañas",
        "",
        "Los proveedores se almacenan como identificadores hash; este informe no contiene URLs ni secretos.",
        "",
        "## Última campaña",
        "",
        f"- Ejecutada (UTC): {when}",
        f"- Perfil: `{latest[2]}`",
        f"- Muestras: {latest[3]}",
        f"- Validaciones positivas: {latest[4]} ({success_rate:.2f}%)",
        f"- Latencia p50: {latest[5] if latest[5] is not None else 'N/D'} ms",
        f"- Latencia p95: {latest[6] if latest[6] is not None else 'N/D'} ms",
        f"- Proveedores pseudonimizados: {providers[0] or 0}",
        "",
        "## Historial",
        "",
        "| Run | Perfil | Muestras | Éxitos | p50 ms | p95 ms |",
        "|---:|---|---:|---:|---:|---:|",
        *[f"| {row[0]} | {row[2]} | {row[3]} | {row[4]} | {row[5] if row[5] is not None else 'N/D'} | {row[6] if row[6] is not None else 'N/D'} |" for row in reversed(runs)],
        "",
    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.store), encoding="utf-8")
