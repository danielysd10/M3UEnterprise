"""Bounded HTTP transport with SSRF checks for every redirect hop."""
from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from runtime_profiles import apply_profile
from url_safety import network_target_allowed

MAX_REDIRECTS = 4
REDIRECT_CODES = {301, 302, 303, 307, 308}
SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


class PublicHttpError(RuntimeError):
    """Network request rejected before a connection to an unsafe target."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = build_opener(_NoRedirectHandler())


def _open_no_redirect(request: Request, timeout: int):
    return _OPENER.open(request, timeout=timeout)


def _http_target_allowed(url: str) -> tuple[bool, str]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False, "El transporte HTTP sólo permite esquemas http/https."
    return network_target_allowed(url)


def open_public_http(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20,
                     method: str = "GET", max_redirects: int = MAX_REDIRECTS):
    """Open public HTTP(S), manually validating each redirect target.

    The caller owns the returned response and must close it.  Redirects never
    forward Authorization/Cookie headers across origins.
    """
    # Backstop for every HTTP caller. A CLI may forget to apply its profile,
    # but production must still fail closed before opening a socket.
    apply_profile()
    current = str(url)
    current_headers = dict(headers or {})
    previous_host = ""
    previous_url = ""
    for hop in range(max(0, int(max_redirects)) + 1):
        allowed, reason = _http_target_allowed(current)
        if not allowed:
            raise PublicHttpError(f"Destino HTTP bloqueado: {reason}")
        current_host = (urlsplit(current).hostname or "").casefold()
        current_origin = (urlsplit(current).scheme.casefold(), current_host, urlsplit(current).port)
        previous_origin = (urlsplit(previous_url).scheme.casefold(), previous_host, urlsplit(previous_url).port) if previous_host else None
        if previous_origin and current_origin != previous_origin:
            current_headers = {k: v for k, v in current_headers.items() if k.casefold() not in SENSITIVE_HEADERS}
        request = Request(current, headers=current_headers, method=method)
        try:
            return _open_no_redirect(request, max(1, int(timeout))), current
        except HTTPError as exc:
            if exc.code not in REDIRECT_CODES:
                raise
            location = (exc.headers or {}).get("Location")
            if not location:
                raise PublicHttpError(f"Redirección HTTP {exc.code} sin Location.") from exc
            if hop >= max_redirects:
                raise PublicHttpError(f"Se excedió el límite de {max_redirects} redirecciones.") from exc
            previous_host = current_host
            previous_url = current
            if urlsplit(current).scheme.casefold() == "https" and urlsplit(urljoin(current, location)).scheme.casefold() != "https":
                raise PublicHttpError("Redirección HTTPS a HTTP bloqueada.") from exc
            current = urljoin(current, location)
        except URLError:
            raise
    raise PublicHttpError("No se pudo completar la solicitud HTTP segura.")
