"""Explicit operating profiles for local, campaign and production runs."""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    max_workers: int
    max_per_host: int
    require_proxy: bool
    require_egress_guard: bool


PROFILES = {
    "development": RuntimeProfile("development", 4, 1, False, False),
    "campaign": RuntimeProfile("campaign", 8, 2, False, False),
    "production": RuntimeProfile("production", 8, 2, True, True),
}


def resolve_profile(name: str | None = None) -> RuntimeProfile:
    selected = (name or os.getenv("M3U_PROFILE") or "development").strip().casefold()
    try:
        return PROFILES[selected]
    except KeyError as exc:
        raise RuntimeError(f"Perfil no reconocido: {selected}. Use development, campaign o production.") from exc


def apply_profile(name: str | None = None) -> RuntimeProfile:
    """Apply the proxy policy before any network-capable component is loaded.

    Production intentionally fails closed: a proxy and an independently
    declared egress guard are both required. The guard is an operator assertion
    because the application cannot prove OS/firewall policy by itself.
    """
    profile = resolve_profile(name)
    proxy = os.getenv("M3U_OUTBOUND_PROXY", "").strip()
    if profile.require_proxy:
        if not proxy:
            raise RuntimeError("production requiere M3U_OUTBOUND_PROXY; no se permite salida directa.")
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("M3U_OUTBOUND_PROXY debe ser una URL http(s) válida.")
        if os.getenv("M3U_EGRESS_GUARD", "").strip() != "1":
            raise RuntimeError("production requiere M3U_EGRESS_GUARD=1 después de aplicar proxy o firewall dedicado.")
        if os.getenv("M3U_ALLOW_PRIVATE_NETWORKS", "").strip() == "1":
            raise RuntimeError("production no permite M3U_ALLOW_PRIVATE_NETWORKS=1.")
    if proxy:
        # urllib and FFprobe inherit these values. Empty NO_PROXY prevents a
        # caller from silently bypassing the approved proxy for selected hosts.
        os.environ.update({"http_proxy": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "NO_PROXY": "", "no_proxy": ""})
    os.environ["M3U_PROFILE"] = profile.name
    return profile
