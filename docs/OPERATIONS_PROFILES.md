# Perfiles operativos y salida controlada

## Perfiles

| Perfil | Workers / host | Proxy | Uso |
|---|---:|---|---|
| `development` | 4 / 1 | Opcional | Desarrollo local sin campañas reales. |
| `campaign` | 8 / 2 | Opcional | Muestra pública acotada y evidencia local. |
| `production` | 8 / 2 | Obligatorio | Operación controlada detrás de proxy y guarda de egreso. |

La producción falla antes de cualquier validación si faltan `M3U_OUTBOUND_PROXY` o `M3U_EGRESS_GUARD=1`, o si se intenta permitir redes privadas. El proxy se propaga a `urllib` y FFprobe mediante las variables estándar `http_proxy` y `https_proxy`; no se registra su valor.

Ejemplo desde la raíz extraída. Sustituya `muestra-autorizada` por la carpeta de entradas aprobadas para su proveedor. Cada ejecución necesita un directorio de resultados nuevo.

```powershell
python tools/run_live_validation_campaign.py --source ".\muestra-autorizada" --profile campaign --limit 64 --per-host 2 --workers 8 --timeout 8
```

Para producción, el proxy debe ser proporcionado por la organización y la guarda de red debe haberse aplicado y revisado antes de declarar la variable:

```powershell
$env:M3U_OUTBOUND_PROXY = 'http://proxy.organizacion.example:8080'
$env:M3U_EGRESS_GUARD = '1'
python tools/run_live_validation_campaign.py --source ".\muestra-autorizada" --profile production --limit 64 --per-host 2 --workers 8 --timeout 8
```

## Firewall dedicado

`tools/Install-ValidationEgressGuard.ps1` crea reglas de salida IPv4 e IPv6 que bloquean RFC1918, link-local y ULA para **dos ejecutables explícitos**: el Python aislado de esta aplicación y FFprobe. Windows requiere separar las familias de direcciones, por lo que la ejecución instala cuatro reglas deterministas (dos por ejecutable). El loopback se bloquea de forma fail-closed en la política SSRF del proceso: Windows Firewall no admite `::1` como `RemoteAddress` en esta clase de regla. Primero inspecciónelo con `-WhatIf`; no se debe apuntar al Python compartido de otros proyectos.

```powershell
.\tools\Install-ValidationEgressGuard.ps1 -PythonPath ".\python-dedicado\python.exe" -FfprobePath ".\ffmpeg\bin\ffprobe.exe" -WhatIf
```

Sustituya las dos rutas del ejemplo por los ejecutables dedicados que realizan la validación. Tras revisar la simulación, el administrador puede aplicar las reglas desde una consola elevada.

## Métricas y evidencia

Cada campaña persiste `history.sqlite3` bajo `artifacts/<perfil>/metrics/`. Sólo contiene conteos, estados, percentiles p50/p95 e identificadores hash de proveedor. No guarda URLs, rutas, queries, tokens ni credenciales. Las campañas y métricas están excluidas del ZIP mediante una regla de empaquetado y una verificación automática.

Para generar un informe legible sin abrir SQLite ni revelar endpoints:

```powershell
python tools/metrics_report.py --store artifacts/campaign/metrics/history.sqlite3 --output artifacts/campaign/metrics/latest_report.md
```

Mantenga la carpeta de resultados y el almacén de métricas fuera del paquete que distribuye. [Política de empaquetado](RELEASE_PACKAGING.md).
