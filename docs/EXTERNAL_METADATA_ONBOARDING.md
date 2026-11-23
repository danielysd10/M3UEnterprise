# Incorporación segura de metadatos externos

## Objetivo y límites

Este procedimiento descubre candidatos de logos y EPG para los canales favoritos sin abrir URLs de reproducción. No convierte datos comunitarios en datos oficiales, no descarga streams y no modifica `channel_metadata_registry.json`.

La fuente de descubrimiento es el índice público `iptv-org`. Es útil para proponer identificadores y sitios que revisar, pero no acredita titularidad del logo, licencia de programación ni autorización de distribución.

## Ejecución inicial sin infraestructura corporativa

Use el Python aislado del proyecto y el perfil `campaign`. Este perfil limita la operación a HTTP público; no requiere proxy, pero tampoco equivale a una certificación de producción.

```powershell
$env:M3U_PROFILE = 'campaign'
.\.venv\Scripts\python.exe tools\discover_metadata_candidates.py --profile campaign --output artifacts\campaign\metadata_candidates.json
```

El informe es evidencia local ignorada por Git. Clasifica cada favorito como `REVIEW_REQUIRED`, `AMBIGUOUS` o `UNMATCHED`. Sólo los candidatos únicos llegan a revisión humana.

## Promoción manual al registro curado

Para cada candidato `REVIEW_REQUIRED`:

1. Abra el sitio web candidato y verifique que pertenece al canal o a su licenciatario.
2. Obtenga un logo bajo una URL HTTPS autorizada y conserve la URL HTTPS de la página oficial que prueba su procedencia.
3. Confirme por contrato o términos de uso que puede consumir la programación XMLTV. No use una URL de EPG comunitaria como si fuese la fuente oficial.
4. Añada manualmente una sola entrada al registro local siguiendo `channel_metadata_registry.example.json`.
5. Ejecute el enriquecimiento sobre `favorites`; inspeccione métricas y el M3U generado antes de considerar `all`.

Los registros `AMBIGUOUS` y `UNMATCHED` requieren una decisión humana o un alias explícito en el registro; nunca se resuelven por similitud aproximada.

## EPG sin proveedor contratado

No existe una fuente universal de EPG con derechos comprobados para todos los canales. Como opción técnica de laboratorio, el proyecto `iptv-org/epg` puede construir XMLTV desde sitios configurados y mapear IDs; antes de usarlo en operación debe revisar el origen por canal, los términos de cada sitio y el volumen de solicitudes. Para producción, prefiera el endpoint XMLTV incluido en el contrato de su proveedor legítimo o un proveedor comercial que autorice expresamente el territorio y la redistribución previstos.

## Paso de producción

La producción exige un proxy de salida administrado, la guarda de egreso activa y ejecutables dedicados para Python y FFprobe. Revise la simulación con `-WhatIf` antes de aplicar las reglas. [Procedimiento y perfiles operativos](OPERATIONS_PROFILES.md).
