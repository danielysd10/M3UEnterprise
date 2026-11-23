# -*- coding: utf-8 -*-
"""M3U PRO Reparador — generación 10.1.0.

Repara únicamente transformaciones demostrablemente seguras y publica mediante
write-temp → validate → atomic replace. El original nunca se sobrescribe por
accidente. Puede procesar uno, varios o cientos de archivos.
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Sequence

from M3U_PRO_VALIDATOR import (
    FAVORITE_CATEGORY_ORDER,
    M3UEntry,
    RepairError,
    atomic_write_text,
    assert_output_outside_inputs,
    build_m3u,
    canonical_favorite_category,
    discover_inputs,
    normalize_entries,
    parse_m3u,
    semantic_text,
    set_attribute,
    sha256_file,
    validate_file,
    assess_entry_url,
    redact_url,
    write_json_report,
)

from prevalidation_manifest import write_manifest

VERSION = "10.1.0"
APP_NAME = "M3U PRO REPARADOR — Enterprise Production Generation"


class RepairReport(dict):
    """Contenedor dict para compatibilidad de automatización y reportes."""


def _legacy_favorite_rescue(entry: M3UEntry) -> str:
    raw = semantic_text(entry.group)
    if raw in {"favoritos", "00 favoritos", "00 - favoritos", "00_favoritos", "00 favoritos legacy"}:
        haystack = semantic_text(entry.name)
        if any(token in haystack for token in ("espn", "sports", "futbol", "nfl", "nba", "mlb")):
            return FAVORITE_CATEGORY_ORDER[3]
        if any(token in haystack for token in ("cnn", "news", "noticias", "bbc")):
            return FAVORITE_CATEGORY_ORDER[6]
        if any(token in haystack for token in ("kids", "disney", "nick", "cartoon")):
            return FAVORITE_CATEGORY_ORDER[0]
        if any(token in haystack for token in ("colombia", "caracol", "rcn", "teleantioquia", "city tv")):
            return FAVORITE_CATEGORY_ORDER[1]
        if any(token in haystack for token in ("hbo", "cinema", "movie", "pelicula", "tnt")):
            return FAVORITE_CATEGORY_ORDER[4]
        if any(token in haystack for token in ("discovery", "hgtv", "gourmet", "entretenimiento")):
            return FAVORITE_CATEGORY_ORDER[2]
        return FAVORITE_CATEGORY_ORDER[5]
    return entry.group


def repair_entries(entries: list[M3UEntry]) -> tuple[list[M3UEntry], list[dict]]:
    repaired: list[M3UEntry] = []
    changes: list[dict] = []
    for entry in entries:
        clone = entry.clone()
        original = clone.to_dict()
        clone.group = canonical_favorite_category(clone.group) or _legacy_favorite_rescue(clone)
        clone.name = clone.name.strip() or "Canal sin nombre"
        clone.attributes = set_attribute(clone.attributes, "group-title", clone.group)
        assessment = assess_entry_url(clone, allow_repair=True)
        if assessment.status == "REPAIRABLE" and assessment.repaired_url:
            old_safe = redact_url(clone.url)
            clone.url = assessment.repaired_url
            changes.append({
                "line": clone.line,
                "action": "repair_url",
                "code": assessment.code,
                "from": old_safe,
                "to": redact_url(clone.url),
                "name": clone.name,
            })
        elif assessment.status in {"INVALID", "UNSUPPORTED"}:
            changes.append({
                "line": clone.line,
                "action": "excluded",
                "reason": assessment.code,
                "safe_url": assessment.safe_url,
                "name": clone.name,
            })
            continue
        if clone.raw_unparsed:
            changes.append({"line": clone.line, "action": "excluded", "reason": "unexpected_content", "name": clone.name})
            continue
        clone.attributes = [(k, v) for k, v in clone.attributes]
        clone.directives = [d for d in clone.directives if d.strip()]
        repaired.append(clone)
        if clone.to_dict() != original:
            changes.append({"line": clone.line, "action": "repair", "name": clone.name, "group": clone.group})
    normalized = normalize_entries(repaired)
    order = {group.casefold(): idx for idx, group in enumerate(FAVORITE_CATEGORY_ORDER)}
    normalized.sort(key=lambda item: (order.get(item.group.casefold(), len(order)), semantic_text(item.group), semantic_text(item.name), item.url))
    return normalized, changes


def repair_file(source: Path, destination: Path, *, allow_partial: bool = False, backup: bool = True) -> dict:
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if source == destination:
        raise RepairError("El destino no puede ser el mismo archivo que la entrada.")
    if not source.exists() or not source.is_file():
        raise RepairError(f"Entrada no encontrada: {source}")
    source_validation = validate_file(source, network=False, allow_repairs=True)
    entries, issues, metadata = parse_m3u(source, repair_obvious_urls=True)
    prevalidation_url_counts = dict(source_validation.get("url_counts", {}))
    blocking = [i for i in issues if i.code in {"INVALID_EXTINF", "DUPLICATE_ATTRIBUTE", "LINE_TOO_LONG"}]
    structural_loss = [i for i in issues if i.code in {"MISSING_URL", "ORPHAN_URL", "UNEXPECTED_CONTENT", "UNEXPECTED_TOP_LEVEL_TEXT"}]
    if issues and not allow_partial and (blocking or structural_loss):
        summary = "; ".join(f"{i.code}@{i.line}" for i in issues[:10])
        raise RepairError(f"No se publica reparación estricta ante incidencias estructurales: {summary}")
    if not entries:
        raise RepairError("No existen entradas completas y seguras para publicar.")
    unsafe_lines = {issue.line for issue in issues if issue.code in {"DUPLICATE_ATTRIBUTE", "UNEXPECTED_CONTENT"}}
    if allow_partial:
        entries = [entry for entry in entries if entry.line not in unsafe_lines and not entry.raw_unparsed]
    final_entries, changes = repair_entries(entries)
    if not final_entries:
        raise RepairError("Todas las entradas quedaron excluidas durante la reparación segura.")
    text = build_m3u(final_entries, bom=True)
    temp_path = destination.with_name(f".{destination.name}.precommit")
    atomic_write_text(temp_path, text, backup=False)
    try:
        validation = validate_file(temp_path, network=False)
        if not validation.get("ok"):
            raise RepairError(f"La salida reparada no pasa validación previa al commit: {validation.get('issues', [])[:5]}")
        backup_path = atomic_write_text(destination, text, backup=backup)
    finally:
        temp_path.unlink(missing_ok=True)
    input_entries = metadata["entries"]
    published = len(final_entries)
    report = RepairReport(
        version=VERSION,
        source=str(source),
        destination=str(destination),
        input_entries=input_entries,
        published_entries=published,
        favorite_entries=sum(1 for entry in final_entries if entry.favorite),
        excluded_entries=max(0, input_entries - published),
        parse_issue_count=len(issues),
        partial=bool(issues),
        changes=changes,
        backup=str(backup_path) if backup_path else "",
        source_sha256=sha256_file(source),
        output_sha256=sha256_file(destination),
        validation_status=validation["status"],
        prevalidation_url_counts=dict(prevalidation_url_counts),
        validation_url_counts=dict(validation.get("url_counts", {})),
        issues=[item.to_dict() for item in issues],
    )
    report_path = destination.with_suffix(destination.suffix + ".repair.json")
    write_json_report(report_path, report)
    return report


def repair_many(paths: Sequence[str | Path], output_dir: Path, *, allow_partial: bool = True, backup: bool = False, progress=None, cancel_event: threading.Event | None = None) -> dict:
    from M3U_PRO_VALIDATOR import setup_logging
    logger = setup_logging()
    files = discover_inputs(paths)
    output_dir = Path(output_dir).resolve()
    logger.info("stage=discovery operation=repair_many files=%d result=READY", len(files))
    assert_output_outside_inputs(paths, output_dir)
    results = []
    for index, source in enumerate(files, 1):
        if cancel_event and cancel_event.is_set():
            break
        destination = output_dir / source.name
        try:
            result = repair_file(source, destination, allow_partial=allow_partial, backup=backup)
            result["status"] = "PASS_PARTIAL" if result["partial"] else "PASS"
        except Exception as exc:
            result = {"source": str(source), "destination": str(destination), "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        logger.info("stage=repair operation=repair_file file=%s result=%s published=%s", source, result.get("status"), result.get("published_entries", 0))
        if progress:
            progress(index, len(files), "Reparación")
    certificates = []
    for row in results:
        counts = row.get("prevalidation_url_counts") or row.get("validation_url_counts") or {}
        certificates.append({
            "source_file": Path(row["source"]).name,
            "clean_file": Path(row["destination"]).name,
            "source_sha256": row.get("source_sha256", ""),
            "clean_sha256": row.get("output_sha256", ""),
            "original_entries": int(row.get("input_entries", 0) or 0),
            "final_entries": int(row.get("published_entries", 0) or 0),
            "valid": int(counts.get("VALID", 0)),
            "repairable": int(counts.get("REPAIRABLE", 0)),
            "repaired": sum(1 for change in (row.get("changes") or []) if change.get("action") == "repair_url"),
            "invalid": int(counts.get("INVALID", 0)),
            "unsupported": int(counts.get("UNSUPPORTED", 0)),
            "excluded": int(row.get("excluded_entries", 0) or 0),
            "quarantined": int(row.get("excluded_entries", 0) or 0),
            "processing_status": row.get("status", "FAILED"),
            "validation_status": row.get("validation_status", "FAIL"),
        })
    manifest_path = write_manifest(output_dir, validator_version="10.1.0", repairer_version=VERSION, files=certificates) if results else None
    return {
        "version": VERSION,
        "total_files": len(files),
        "processed_files": len(results),
        "successful_files": sum(1 for row in results if row.get("status", "").startswith("PASS")),
        "results": results,
        "manifest": str(manifest_path) if manifest_path else "",
        "cancelled": bool(cancel_event and cancel_event.is_set()),
    }


class RepairerApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("900x640")
        self.root.minsize(800, 540)
        self.paths: list[Path] = []
        self.running = False
        self.cancel_event = threading.Event()
        self.output_var = tk.StringVar(value=str(Path.cwd() / "REPARADOS"))
        self.partial_var = tk.BooleanVar(value=True)
        self.backup_var = tk.BooleanVar(value=True)
        self.progress = tk.DoubleVar(value=0)
        self.status = tk.StringVar(value="Listo.")
        self._build()

    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = ttk.Frame(self.root, padding=12); outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="M3U PRO REPARADOR", font=("TkDefaultFont", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text=f"Enterprise Production v{VERSION}").pack(anchor="w", pady=(0, 10))
        bar = ttk.Frame(outer); bar.pack(fill="x")
        ttk.Button(bar, text="Agregar archivos", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Agregar carpeta", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Limpiar", command=self.clear).pack(side="left")
        ttk.Checkbutton(bar, text="Permitir reparación parcial segura", variable=self.partial_var).pack(side="left", padx=12)
        ttk.Checkbutton(bar, text="Backup del destino previo", variable=self.backup_var).pack(side="left")
        ttk.Label(outer, text="Directorio de salida").pack(anchor="w", pady=(12, 2))
        outbar = ttk.Frame(outer); outbar.pack(fill="x")
        ttk.Entry(outbar, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        ttk.Button(outbar, text="Elegir", command=self.pick_output).pack(side="left", padx=(6, 0))
        self.tree = ttk.Treeview(outer, columns=("path", "status"), show="headings", selectmode="extended")
        self.tree.heading("path", text="Entrada"); self.tree.heading("status", text="Estado")
        self.tree.column("path", width=680); self.tree.column("status", width=140)
        self.tree.pack(fill="both", expand=True, pady=10)
        buttons = ttk.Frame(outer); buttons.pack(fill="x")
        self.run_button = ttk.Button(buttons, text="REPARAR", command=self.start); self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(buttons, text="Cancelar", command=self.cancel, state="disabled"); self.cancel_button.pack(side="left", padx=6)
        ttk.Progressbar(outer, variable=self.progress, maximum=100).pack(fill="x", pady=6)
        ttk.Label(outer, textvariable=self.status).pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def refresh(self) -> None:
        for item in self.tree.get_children(): self.tree.delete(item)
        for path in self.paths: self.tree.insert("", "end", values=(str(path), "Pendiente"))

    def add_files(self) -> None:
        from tkinter import filedialog
        for value in filedialog.askopenfilenames(title="Selecciona M3U/M3U8", filetypes=[("M3U/M3U8", "*.m3u *.m3u8"), ("Todos", "*.*")]):
            p = Path(value).resolve()
            if p not in self.paths: self.paths.append(p)
        self.paths.sort(key=lambda p: str(p).casefold()); self.refresh()

    def add_folder(self) -> None:
        from tkinter import filedialog
        value = filedialog.askdirectory(title="Selecciona carpeta")
        if value:
            for p in discover_inputs([value]):
                if p not in self.paths: self.paths.append(p)
            self.paths.sort(key=lambda p: str(p).casefold()); self.refresh()

    def clear(self) -> None:
        self.paths.clear(); self.refresh()

    def pick_output(self) -> None:
        from tkinter import filedialog
        value = filedialog.askdirectory(title="Directorio de salida")
        if value: self.output_var.set(value)

    def start(self) -> None:
        if self.running or not self.paths: return
        self.running = True; self.cancel_event.clear()
        self.run_button.configure(state="disabled"); self.cancel_button.configure(state="normal")
        self.progress.set(0)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            result = repair_many(self.paths, Path(self.output_var.get()), allow_partial=self.partial_var.get(), backup=self.backup_var.get(), progress=self._progress, cancel_event=self.cancel_event)
            self.root.after(0, lambda: self._done(result))
        except Exception as exc:
            self.root.after(0, lambda: self._error(exc))

    def _progress(self, done, total, stage) -> None:
        value = 100 if not total else done * 100 / total
        self.root.after(0, lambda: (self.progress.set(value), self.status.set(f"{stage}: {done}/{total}")))

    def _done(self, result) -> None:
        self.running=False; self.run_button.configure(state="normal"); self.cancel_button.configure(state="disabled"); self.progress.set(100)
        self.status.set(f"Finalizado: {result['successful_files']}/{result['processed_files']} archivos publicados.")

    def _error(self, exc) -> None:
        self.running=False; self.run_button.configure(state="normal"); self.cancel_button.configure(state="disabled"); self.status.set(f"ERROR: {type(exc).__name__}: {exc}")

    def cancel(self) -> None:
        self.cancel_event.set()
        self.status.set("Cancelación solicitada; se preservará cualquier archivo ya comprometido.")

    def close(self) -> None:
        self.cancel_event.set(); self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{VERSION}")
    parser.add_argument("paths", nargs="*", help="M3U/M3U8 o carpetas")
    parser.add_argument("-o", "--output-dir", default=str(Path.cwd() / "REPARADOS"))
    parser.add_argument("--strict", action="store_true", help="No permitir reparación parcial")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--report", help="Reporte JSON")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args(argv)
    if args.gui or not args.paths:
        RepairerApp().run(); return 0
    result = repair_many(args.paths, Path(args.output_dir), allow_partial=not args.strict, backup=not args.no_backup)
    if args.report: write_json_report(Path(args.report), result)
    failed = [row for row in result["results"] if row.get("status") == "FAILED"]
    print(f"Archivos: {result['processed_files']}/{result['total_files']}")
    print(f"Publicados: {result['successful_files']}")
    print(f"Fallidos: {len(failed)}")
    print(f"Reportes JSON: cada salida incluye .repair.json")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
