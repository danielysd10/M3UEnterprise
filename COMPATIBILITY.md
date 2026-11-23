# Compatibilidad / Antes y después

| Área | Antes | Después | Motivo | Riesgo |
|---|---|---|---|---|
| URL parsing | regex superficial | `url_safety.py` central | eliminar inconsistencias | Bajo |
| RTMP legacy | llega como URL completa | reparación determinista + `url_original` | recuperar datos conocidos | Bajo |
| puerto inválido | podía explotar en reporting | `INVALID_PORT` antes de red | evitar fallo tardío | Bajo |
| reporting | excepción podía abortar job | publicación primero + fallback | conservar resultado | Bajo |
| worker exceptions | future podía abortar validación | resultado aislado por registro | fault tolerance | Bajo |
| checkpoint | JSON creciente | SQLite transaccional | escalabilidad/recuperación | Medio |
| checkpoints legacy | lectura JSON | importación a SQLite | continuidad V9 | Medio |
| diagnósticos | riesgo de URL cruda | redacción central | seguridad | Bajo |
| código duplicado | implementación activa duplicada en `M3U_PRO/` | shim a implementación canónica | evitar split-brain | Bajo |
| validación M3U | regex URL independiente | usa `inspect_url()` | fuente única de verdad | Bajo |

No se cambió la intención funcional de favoritos, límites de fuentes o normalización de categorías salvo donde la seguridad de datos exigía bloquear una entrada inválida.
