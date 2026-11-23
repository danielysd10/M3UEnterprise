# Enriquecimiento de logos y guía XMLTV

## Propósito y límites

Este componente completa metadatos IPTV sin participar en la decisión de si un stream está activo. Conserva cada URL de reproducción, cada logo existente y el orden que produjo el pipeline principal. Su única salida M3U añade, cuando hay evidencia, `tvg-logo`, `tvg-id` y una cabecera `url-tvg`.

No existe una fuente universal que permita afirmar de manera fiable que un logo encontrado por búsqueda libre es oficial. Por esa razón el producto no hace scraping ni búsquedas abiertas: sólo acepta una asignación cuando un registro curado aporta a la vez la URL HTTPS del logo y una URL HTTPS de la procedencia oficial. Las coincidencias faltantes son un resultado normal y seguro, no un error que se deba completar por aproximación.

## Registro controlado

Copie `channel_metadata_registry.example.json` a `channel_metadata_registry.json` fuera del control de versiones. El archivo local está ignorado y excluido del ZIP de release. El esquema actual es `2`: rechaza alias, `tvg_id` y `epg_id` duplicados para evitar asignaciones ambiguas.

```json
{
  "schema": 2,
  "epg_url": "https://epg.proveedor-autorizado.example/guia.xml",
  "channels": [
    {
      "name": "Nombre oficial del canal",
      "aliases": ["Nombre que figura en la lista"],
      "tvg_id": "identificador.m3u",
      "epg_id": "identificador.xmltv",
      "official_logo_url": "https://www.canal.example/recursos/logo.png",
      "official_logo_source": "https://www.canal.example/"
    }
  ]
}
```

La coincidencia es determinista: `tvg-id`, nombre visible, `tvg-name` y luego alias normalizados. No usa similitud difusa. Si existe `tvg-logo`, no lo cambia; la validación del logo existente es sintáctica/local (HTTPS, host y credenciales ausentes), para no revelar el catálogo completo mediante miles de consultas remotas.

## Rendimiento y seguridad

- Alcance GUI por defecto: `favorites`, grupos que empiezan por `00 - Favoritos`.
- `all` es manual y explícito; úselo sólo después de medir un lote pequeño.
- XMLTV se descarga una vez por ejecución y se reutiliza mientras su archivo de caché tenga menos de 12 horas.
- El límite de descarga comprimida es 32 MiB, el de XML expandido es 128 MiB y existe un timeout de 20 segundos.
- Antes de descargar y antes de cada redirección se aplica la política SSRF; los destinos privados, loopback y reservados se bloquean igual que en la validación de streams.
- Una guía gzip se descomprime de forma acotada y se guarda como XML real, no como contenido comprimido bajo extensión `.xml`.
- Una descarga XMLTV local es evidencia operativa, no una autorización para publicarla. Para que un reproductor externo acceda a una copia local debe publicarse mediante la infraestructura aprobada y con la URL HTTPS pública correspondiente en `epg_url`.

## Operación

1. Complete el registro con fuentes oficiales verificadas y una URL XMLTV autorizada por su proveedor.
2. En la GUI, seleccione el registro, marque “Completar sólo Favoritos” y deje `favorites` como alcance inicial.
3. Active “Descargar/cachar XMLTV” si desea la copia local; la salida conserva `url-tvg` para clientes IPTV compatibles.
4. Revise las métricas del informe: `logos_added`, `logos_still_missing`, `unmatched`, `epg_ids_added`, cobertura de IDs XMLTV, canales con programación y el estado de caché XMLTV.
5. Sólo tras revisar resultados use `all`; no se elimina ni reemplaza contenido por esta función.
