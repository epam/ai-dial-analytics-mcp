from __future__ import annotations

import pytest

from adas_mcp.config import (
    Settings,
    capabilities_from_env,
    execute_sql_enabled_from_env,
)
from adas_mcp.errors import ConfigError


def test_capabilities_defaults_to_limited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADAS_CAPABILITIES", raising=False)
    assert capabilities_from_env() == "limited"


def test_capabilities_accepts_full_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_CAPABILITIES", "FULL")
    assert capabilities_from_env() == "full"


def test_capabilities_rejects_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_CAPABILITIES", "everything")
    with pytest.raises(ConfigError):
        capabilities_from_env()


def test_execute_sql_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADAS_ENABLE_EXECUTE_SQL", raising=False)
    assert execute_sql_enabled_from_env() is False


def test_execute_sql_is_independent_of_capabilities(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_CAPABILITIES", "full")
    assert execute_sql_enabled_from_env() is False
    monkeypatch.setenv("ADAS_CAPABILITIES", "limited")
    monkeypatch.setenv("ADAS_ENABLE_EXECUTE_SQL", "true")
    assert execute_sql_enabled_from_env() is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_execute_sql_accepts_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
):
    monkeypatch.setenv("ADAS_ENABLE_EXECUTE_SQL", raw)
    assert execute_sql_enabled_from_env() is True


@pytest.mark.parametrize("raw", ["0", "false", "NO", "off", ""])
def test_execute_sql_accepts_falsy_spellings(monkeypatch: pytest.MonkeyPatch, raw: str):
    monkeypatch.setenv("ADAS_ENABLE_EXECUTE_SQL", raw)
    assert execute_sql_enabled_from_env() is False


def test_execute_sql_rejects_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_ENABLE_EXECUTE_SQL", "maybe")
    with pytest.raises(ConfigError):
        execute_sql_enabled_from_env()


def test_settings_requires_base_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADAS_BASE_URL", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_settings_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_BASE_URL", "http://adas.test/")
    settings = Settings.from_env()
    assert settings.base_url == "http://adas.test"
    assert settings.capabilities == "limited"
    assert settings.execute_sql_enabled is False
