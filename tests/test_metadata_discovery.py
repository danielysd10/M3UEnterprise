from tools.discover_metadata_candidates import key


def test_metadata_discovery_key_keeps_real_channel_identity_and_removes_safe_variants():
    assert key("Dios Te Ve Kids (720p)") == "dios te ve kids"
    assert key("Canal Ñ HD") == "canal n"


def test_metadata_discovery_key_does_not_drop_internal_words_or_parameters():
    assert key("TV 5 News Plus") == "tv 5 news plus"
