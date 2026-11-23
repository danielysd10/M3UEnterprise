# -*- coding: utf-8 -*-
"""CLI enterprise para migración/validación del catálogo maestro de Favoritos."""
import argparse
from pathlib import Path
import sys
from enterprise_favorites import migrate_favorites_directory, validate_catalog_file
from user_settings import MASTER_FILENAME, SettingsError, default_config_dir, import_legacy_settings

VERSION = '9.0.0-enterprise'

def main():
    ap=argparse.ArgumentParser(description='Gestor Enterprise de Favoritos')
    ap.add_argument('directory', nargs='?', type=Path,
                    help='Directorio de catálogo para migración o validación; sin directorio sólo se valida el catálogo personal.')
    ap.add_argument('--import-legacy', metavar='ORIGEN', type=Path,
                    help='Importar preferencias, bloques y favoritos a la carpeta personal, conservando los archivos existentes.')
    ap.add_argument('--force-migrate', action='store_true')
    ap.add_argument('--validate-only', action='store_true')
    args = ap.parse_args()
    if args.import_legacy is not None:
        if args.directory is not None or args.force_migrate or args.validate_only:
            ap.error('--import-legacy se utiliza solo.')
        try:
            outcomes = import_legacy_settings(args.import_legacy, default_config_dir())
        except SettingsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        labels = {'copied': 'copiado', 'unchanged': 'ya presente', 'conflict': 'conflicto; archivos conservados'}
        for name, status in outcomes.items():
            print(f'{name}: {labels[status]}')
        if not outcomes:
            print('No se encontraron preferencias, bloques ni favoritos en el origen.')
        return int('conflict' in outcomes.values())
    if args.force_migrate and args.directory is None:
        ap.error('--force-migrate requiere un directorio explícito.')
    try:
        root = args.directory.resolve() if args.directory is not None else default_config_dir()
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.directory is not None and not args.validate_only:
        r=migrate_favorites_directory(root, force=args.force_migrate)
        print(f'FAVORITOS CATALOG {VERSION}')
        print(f"Migrado: {'SI' if r['migrated'] else 'NO'}")
        print(f"Legacy detectado: {'SI' if r['legacy'] else 'NO'}")
        print(f"Maestro: {r['master']}")
        print(f"Backup legacy: {r['backup']}" )
    v=validate_catalog_file(root/MASTER_FILENAME)
    print(f"VALIDACION: {'PASS' if v['ok'] else 'FAIL'}")
    for x in v['issues']:
        print(' - '+x)
    return 0 if v['ok'] else 1

if __name__=='__main__': raise SystemExit(main())
