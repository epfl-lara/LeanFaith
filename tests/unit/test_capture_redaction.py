"""Focused tests for provider-capture secret redaction."""

from leanfaith.generation.capture_redaction import redact_captured_streams


def test_provider_metadata_key_is_not_treated_as_a_secret() -> None:
    capture = b'{"api_key_source":null,"result":"safe"}\n'

    result = redact_captured_streams({"stdout.json": capture}, environment={})

    assert result.replacement_count == 0
    assert result.streams["stdout.json"] == capture


def test_provider_token_value_is_still_redacted() -> None:
    capture = b'{"value":"token-abcdef0123456789","api_key_source":null}\n'

    result = redact_captured_streams({"stdout.json": capture}, environment={})

    assert result.replacement_count == 1
    assert b"token-abcdef0123456789" not in result.streams["stdout.json"]
    assert b'"api_key_source":null' in result.streams["stdout.json"]
