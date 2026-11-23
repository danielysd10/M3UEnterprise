from url_safety import URLStatus, inspect_url, network_target_allowed


def test_private_ip_literal_is_blocked_without_network_access():
    assert inspect_url("http://192.168.1.10/live").status == URLStatus.PRIVATE_NETWORK
    ok, reason = network_target_allowed("http://127.0.0.1:8080/live")
    assert not ok and "bloqueado" in reason.lower()


def test_public_fixture_domain_is_allowed_when_dns_is_mocked(monkeypatch):
    monkeypatch.setattr("url_safety.socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("8.8.8.8", 80))])
    ok, _reason = network_target_allowed("https://fixture.example/live.m3u8")
    assert ok


def test_dns_resolution_to_private_ip_is_rejected(monkeypatch):
    monkeypatch.setattr("url_safety.socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("10.1.2.3", 80))])
    ok, reason = network_target_allowed("https://fixture.example/live.m3u8")
    assert not ok and "no pública" in reason
