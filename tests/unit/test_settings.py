"""Smoke tests for config.settings — ensure env-based loading works."""

import os

import pytest


def test_settings_loads_defaults(monkeypatch, tmp_path):
    """Settings.load() should work with zero env vars (all defaults)."""
    # Write an empty .env so dotenv doesn't complain
    env_file = tmp_path / ".env"
    env_file.write_text("")

    from config.settings import Settings

    settings = Settings.load(env_path=env_file)

    assert settings.mode == "dry_run"
    assert "binance" in settings.enabled_exchanges
    assert settings.risk.max_trade_usd == 100.0
    assert settings.risk.daily_loss_limit_usd == 50.0


def test_settings_rejects_invalid_mode(monkeypatch, tmp_path):
    """An invalid ARBCORE_MODE must raise immediately."""
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setenv("ARBCORE_MODE", "yolo")

    from config.settings import Settings

    with pytest.raises(ValueError, match="dry_run"):
        Settings.load(env_path=env_file)


def test_credentials_repr_never_leaks():
    """Repr of ExchangeCredentials must mask secrets."""
    from config.settings import ExchangeCredentials

    cred = ExchangeCredentials(api_key="super_secret", api_secret="mega_secret")
    text = repr(cred)
    assert "super_secret" not in text
    assert "mega_secret" not in text
    assert "masked" in text
