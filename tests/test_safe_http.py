from email.message import Message
from urllib.error import HTTPError

import pytest

import safe_http


def test_private_target_is_rejected_before_http_open(monkeypatch):
    monkeypatch.setattr(safe_http, "apply_profile", lambda: None)
    monkeypatch.setattr(safe_http, "network_target_allowed", lambda _url: (False, "privado"))
    monkeypatch.setattr(safe_http, "_open_no_redirect", lambda *_args: pytest.fail("No debía abrir red"))
    with pytest.raises(safe_http.PublicHttpError, match="privado"):
        safe_http.open_public_http("https://private.example.test/guide.xml")


def test_redirect_target_is_revalidated_before_following(monkeypatch):
    monkeypatch.setattr(safe_http, "apply_profile", lambda: None)
    checks = []
    def policy(url):
        checks.append(url)
        return (not url.startswith("https://127.0.0.1"), "privado")
    headers = Message()
    headers["Location"] = "https://127.0.0.1:8080/internal"
    monkeypatch.setattr(safe_http, "network_target_allowed", policy)
    monkeypatch.setattr(safe_http, "_open_no_redirect", lambda request, _timeout: (_ for _ in ()).throw(HTTPError(request.full_url, 302, "Found", headers, None)))
    with pytest.raises(safe_http.PublicHttpError, match="privado"):
        safe_http.open_public_http("https://public.example.test/start")
    assert checks == ["https://public.example.test/start", "https://127.0.0.1:8080/internal"]
