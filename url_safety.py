# -*- coding: utf-8 -*-
"""Single source of truth for M3U URL safety.

URL syntax/classification/repair is deliberately separate from network reachability
and FFprobe. Every caller treats the returned inspection as untrusted-data safe.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

SUPPORTED_SCHEMES = frozenset({"http", "https", "rtmp", "rtmps", "rtsp", "udp", "p2p"})
SENSITIVE_QUERY_KEYS = frozenset({
    "token", "access_token", "auth", "key", "apikey", "api_key", "signature",
    "sig", "hash", "password", "passwd", "pass", "pwd", "username", "user",
    "credential", "credentials", "secret", "session", "sid",
})
SENSITIVE_PATH_TOKENS = re.compile(r"(?i)(?:token|auth|sig|signature|key|password|passwd|credential|session)[=/:.-][^/?#\s]+")
URL_LIKE_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"']+")


class URLStatus(str, Enum):
    EMPTY = "EMPTY"
    NON_STRING = "NON_STRING"
    VALID = "VALID"
    REPAIRED = "REPAIRED"
    INVALID_SYNTAX = "INVALID_SYNTAX"
    INVALID_PORT = "INVALID_PORT"
    INVALID_HOST = "INVALID_HOST"
    PRIVATE_NETWORK = "PRIVATE_NETWORK"
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"


@dataclass(frozen=True)
class URLInspection:
    raw: str
    normalized_url: str
    status: URLStatus
    reason: str
    scheme: str = ""
    host: str = ""
    port: int | None = None
    has_credentials: bool = False
    repaired: bool = False
    code: str = ""

    @property
    def usable_for_network(self) -> bool:
        return self.status in (URLStatus.VALID, URLStatus.REPAIRED)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def _clean_input(value: Any) -> tuple[str, URLStatus | None, str]:
    if value is None:
        return "", URLStatus.EMPTY, "URL ausente (None)"
    if not isinstance(value, str):
        return "", URLStatus.NON_STRING, f"URL no textual: {type(value).__name__}"
    text = value.strip().replace("\x00", "")
    if not text:
        return "", URLStatus.EMPTY, "URL vacía"
    return text, None, ""


def _repair_obvious_scheme(text: str) -> tuple[str, str | None]:
    replacements = (
        (r"^hhttps://", "https://", "HHTTP_SCHEME_REPAIRED"),
        (r"^http\s+:\s+//", "http://", "HTTP_SCHEME_REPAIRED"),
        (r"^https\s+:\s+//", "https://", "HTTPS_SCHEME_REPAIRED"),
    )
    for pattern, replacement, code in replacements:
        fixed, count = re.subn(pattern, replacement, text, count=1, flags=re.I)
        if count:
            return fixed, code
    return text, None


def _unwrap_known_embedded_rtmp(text: str) -> tuple[str | None, str | None]:
    patterns = (
        r"^(rtmp)://\$OPT:rtmp-raw=((?:rtmp|rtmps)://\S+)$",
        r"^rtmp-raw=((?:rtmp|rtmps)://\S+)$",
    )
    for pattern in patterns:
        m = re.match(pattern, text, re.I)
        if m:
            return m.group(2 if m.lastindex and m.lastindex >= 2 else 1), "RTMP_RAW_WRAPPER_UNWRAPPED"
    return None, None


def inspect_url(value: Any) -> URLInspection:
    raw, early_status, early_reason = _clean_input(value)
    if early_status is not None:
        return URLInspection(str(value) if value is not None else "", "", early_status, early_reason, code=early_status.value)

    working = raw
    repair_code: str | None = None
    fixed, code = _repair_obvious_scheme(working)
    if code:
        working, repair_code = fixed, code
    else:
        unwrapped, unwrap_code = _unwrap_known_embedded_rtmp(working)
        if unwrapped:
            working, repair_code = unwrapped, unwrap_code

    try:
        parsed = urlsplit(working)
    except (ValueError, TypeError) as exc:
        return URLInspection(raw, working, URLStatus.INVALID_SYNTAX, f"urlsplit rechazó la URL: {exc}", code="URL_PARSE_ERROR", repaired=bool(repair_code))
    except Exception as exc:
        return URLInspection(raw, working, URLStatus.INVALID_SYNTAX, f"Error de parsing URL: {type(exc).__name__}: {exc}", code="URL_PARSE_ERROR", repaired=bool(repair_code))

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return URLInspection(raw, working, URLStatus.INVALID_SYNTAX, "Falta esquema/protocolo", code="MALFORMED_URL", repaired=bool(repair_code))
    if scheme not in SUPPORTED_SCHEMES:
        return URLInspection(raw, working, URLStatus.UNSUPPORTED_SCHEME, f"Protocolo no soportado: {scheme}", scheme=scheme, code="UNSUPPORTED_SCHEME", repaired=bool(repair_code))

    try:
        hostname = parsed.hostname or ""
    except (ValueError, TypeError) as exc:
        return URLInspection(raw, working, URLStatus.INVALID_HOST, f"Host inválido: {exc}", scheme=scheme, code="INVALID_HOST", repaired=bool(repair_code))

    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            return URLInspection(raw, working, URLStatus.INVALID_HOST, "IPv6 entre corchetes incompleto.", scheme=scheme, host=hostname, code="INVALID_IPV6", repaired=bool(repair_code))
        suffix = authority[closing + 1:]
        if suffix.startswith(":") and suffix[1:] == "":
            return URLInspection(raw, working, URLStatus.INVALID_PORT, "Puerto inválido: puerto vacío", scheme=scheme, host=hostname, code="EMPTY_PORT", repaired=bool(repair_code))
    elif authority.count(":") == 1 and authority.endswith(":"):
        return URLInspection(raw, working, URLStatus.INVALID_PORT, "Puerto inválido: puerto vacío", scheme=scheme, host=hostname, code="EMPTY_PORT", repaired=bool(repair_code))
    elif authority.count(":") > 1:
        return URLInspection(raw, working, URLStatus.INVALID_HOST, "IPv6 debe ir entre corchetes en la autoridad URL.", scheme=scheme, host=hostname, code="INVALID_IPV6", repaired=bool(repair_code))

    try:
        port = parsed.port
    except (ValueError, TypeError) as exc:
        return URLInspection(raw, working, URLStatus.INVALID_PORT, f"Puerto inválido: {exc}", scheme=scheme, host=hostname, code="INVALID_PORT", repaired=bool(repair_code))

    if scheme in {"http", "https", "rtmp", "rtmps", "rtsp", "p2p", "udp"} and not hostname:
        return URLInspection(raw, working, URLStatus.INVALID_HOST, "Falta host en la URL.", scheme=scheme, code="MISSING_HOST", repaired=bool(repair_code))

    # Block literal loopback, private, link-local and reserved endpoints by
    # default.  UDP multicast remains syntactically valid because it is a
    # legitimate playback form; it is still subject to the explicit runtime
    # network policy before a request is made.
    if scheme != "udp":
        try:
            address = ipaddress.ip_address(hostname)
            if not address.is_global and os.getenv("M3U_ALLOW_PRIVATE_NETWORKS", "").strip() != "1":
                return URLInspection(
                    raw, working, URLStatus.PRIVATE_NETWORK,
                    "Destino IP local, privado, reservado o loopback bloqueado por política.",
                    scheme=scheme, host=hostname, port=port, code="PRIVATE_NETWORK", repaired=bool(repair_code),
                )
        except ValueError:
            pass

    if any(ord(ch) < 32 or ch == "\x7f" for ch in working):
        return URLInspection(raw, working, URLStatus.INVALID_SYNTAX, "La URL contiene caracteres de control.", scheme=scheme, host=hostname, port=port, code="CONTROL_CHARACTERS", repaired=bool(repair_code))

    try:
        credentials = parsed.username is not None or parsed.password is not None
    except Exception:
        credentials = "@" in parsed.netloc.rsplit("@", 1)[0]

    status = URLStatus.REPAIRED if repair_code else URLStatus.VALID
    return URLInspection(
        raw=raw, normalized_url=working, status=status,
        reason=("URL reparada de forma determinista." if repair_code else "URL sintácticamente válida"),
        scheme=scheme, host=hostname, port=port, has_credentials=credentials,
        repaired=bool(repair_code), code=repair_code or "URL_VALID",
    )


def apply_url_policy(entry: dict[str, Any]) -> URLInspection:
    inspection = inspect_url(entry.get("url"))
    if inspection.repaired and inspection.normalized_url:
        entry.setdefault("url_original", inspection.raw)
        entry["url"] = inspection.normalized_url
    entry["url_validation"] = inspection.as_dict()
    return inspection


def network_target_allowed(value: Any, *, allow_private: bool | None = None, resolver=None) -> tuple[bool, str]:
    """Resolve a target immediately before network access and reject non-public IPs.

    This protects HTTP and FFprobe callers from DNS names resolving to local or
    private addresses.  Internal deployments can opt in explicitly with
    ``M3U_ALLOW_PRIVATE_NETWORKS=1``; that decision is intentionally external
    to playlist content and can be audited by the operator.
    """
    inspection = inspect_url(value)
    if not inspection.usable_for_network:
        return False, inspection.reason
    if allow_private is None:
        allow_private = os.getenv("M3U_ALLOW_PRIVATE_NETWORKS", "").strip() == "1"
    if allow_private or inspection.scheme == "udp":
        return True, "Destino permitido por política explícita o protocolo UDP."
    try:
        # Enterprise supplies an owned-process resolver so a slow OS DNS call
        # can be cancelled. Other callers retain the existing synchronous API.
        resolve = resolver or socket.getaddrinfo
        addresses = {item[4][0] for item in resolve(inspection.host, inspection.port or 0, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        return False, f"No se pudo resolver DNS: {exc}"
    if not addresses:
        return False, "DNS no devolvió direcciones IP."
    blocked = []
    for raw_address in addresses:
        try:
            if not ipaddress.ip_address(raw_address).is_global:
                blocked.append(raw_address)
        except ValueError:
            return False, "DNS devolvió una dirección inválida."
    if blocked:
        return False, "DNS resolvió a una dirección no pública bloqueada por política."
    return True, "Destino público permitido."


def _safe_host(parsed: Any) -> str:
    try:
        host = parsed.hostname or ""
    except Exception:
        return "[HOST_REDACTED]"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def redact_url(value: Any) -> str:
    inspection = inspect_url(value)
    text = inspection.normalized_url or inspection.raw
    if not text:
        return "[URL_REDACTED: EMPTY]"
    try:
        parsed = urlsplit(text)
        host = _safe_host(parsed)
        if not host or not parsed.netloc:
            return f"[URL_REDACTED: {inspection.status.value}]"
        try:
            port = parsed.port
        except Exception:
            port = None
        netloc = host + (f":{port}" if port is not None else "")
        safe_path = []
        for segment in (parsed.path or "").split("/"):
            if not segment:
                safe_path.append(segment)
            elif len(segment) > 32 or re.search(r"[A-Za-z0-9_-]{20,}", segment) or SENSITIVE_PATH_TOKENS.search(segment):
                safe_path.append("[REDACTED]")
            else:
                safe_path.append(segment)
        try:
            items = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=512)
        except Exception:
            return f"{parsed.scheme.lower()}://{netloc}{'/'.join(safe_path)}?[QUERY_REDACTED]"
        safe_query = []
        for key, qvalue in items:
            safe_value = "[REDACTED]" if key.casefold() in SENSITIVE_QUERY_KEYS or len(qvalue) > 24 or SENSITIVE_PATH_TOKENS.search(qvalue) else qvalue
            safe_query.append((key, safe_value))
        query = "&".join(f"{quote(str(k), safe='._~-')}={quote(str(v), safe='._~-')}" for k, v in safe_query)
        rendered = urlunsplit((parsed.scheme.lower(), netloc, "/".join(safe_path), query, ""))
        if inspection.status not in (URLStatus.VALID, URLStatus.REPAIRED):
            rendered += f" [{inspection.status.value}]"
        if inspection.repaired:
            rendered += " [REPAIRED]"
        return rendered
    except Exception:
        return f"[URL_REDACTED: {inspection.status.value}]"


def redact_url_for_export(value: Any) -> str:
    """Return a stable, share-safe stream reference.

    Diagnostic redaction may retain harmless path components so an operator can
    troubleshoot a local run.  Exported evidence has a different trust
    boundary: paths, query strings, user info and fragments may themselves be
    bearer credentials even when they do not match a known token pattern.
    Consequently an export retains only scheme, host, port and a short
    fingerprint; it never serializes any of those components.
    """
    inspection = inspect_url(value)
    raw = inspection.normalized_url or inspection.raw
    fingerprint = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    try:
        parsed = urlsplit(raw)
        host = _safe_host(parsed)
        if host and parsed.netloc:
            try:
                port = parsed.port
            except ValueError:
                port = None
            netloc = host + (f":{port}" if port is not None else "")
            return f"{parsed.scheme.lower()}://{netloc}/[PATH_REDACTED]#sha256={fingerprint}"
    except Exception:
        pass
    return f"[URL_REDACTED:{inspection.status.value}:sha256={fingerprint}]"


def redact_text(value: Any) -> str:
    try:
        return URL_LIKE_RE.sub(lambda m: redact_url(m.group(0)), str(value or ""))
    except Exception:
        return "[DIAGNOSTIC_REDACTED]"


def classification_reason(value: Any) -> tuple[str, str]:
    inspection = inspect_url(value)
    return inspection.status.value, inspection.reason
