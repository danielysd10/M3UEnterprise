# Publicación reproducible de la lista IPTV

La URL estable de un reproductor debe ser siempre:

```text
https://<dominio-controlado>/iptv/unificada.m3u
```

El dominio no se inventa ni se publica desde GitHub: la lista puede contener endpoints, parámetros de acceso o contenido cuya distribución pública requiere autorización. El flujo separa la generación de la lista de su hosting.

## Publicar el M3U final

Cuando el unificador haya producido el archivo final, publíquelo atómicamente:

```powershell
.\.venv\Scripts\python.exe tools\publish_playlist.py `
  --input ".\salida\unificada_final.m3u" `
  --publish-root ".\publicacion" `
  --public-base-url https://iptv.midominio.example
```

El publicador reemplaza únicamente `iptv/unificada.m3u` y escribe `iptv/unificada.manifest.json` con SHA-256, tamaño y fecha. Una actualización nunca deja un archivo parcialmente escrito.

## Prueba local

```powershell
.\.venv\Scripts\python.exe tools\serve_published_playlist.py --root ".\publicacion"
```

La URL local será `http://127.0.0.1:8080/iptv/unificada.m3u`; sólo es válida para reproductores en esa misma estación. Para una aplicación IPTV externa use hosting HTTPS administrado —proxy inverso, CDN u object storage privado— que entregue el mismo path público y aplique autenticación cuando corresponda.

## Controles de producción

1. El hosting debe permitir únicamente HTTPS, deshabilitar listado de directorios y registrar accesos sin guardar secretos.
2. Proteja el origen con credenciales de despliegue, no con tokens embebidos en la URL M3U.
3. Compare el SHA-256 del manifiesto después de cada publicación.
4. No suba listas ni archivos bajo `artifacts/` a GitHub.
# Publicación conjunta M3U y XMLTV

El publicador escribe de forma atómica `iptv/unificada.m3u` y, opcionalmente,
`iptv/guia.xml`. Cuando se proporciona una guía, exige `--public-base-url` HTTPS,
publica ambos archivos y actualiza la cabecera `url-tvg` de la lista para apuntar
a la URL pública propia. El manifiesto `iptv/unificada.manifest.json` conserva
hash SHA-256 y tamaño independientes para la lista y la guía.

Ejemplo:

```powershell
python tools/publish_playlist.py --input lista.m3u --epg-input lista_guia.xml --publish-root ".\publicacion" --public-base-url https://iptv.example
```

El comando no configura DNS, TLS, proxy ni firewall. Esas capas deben existir
antes de presentar la URL a clientes IPTV.
