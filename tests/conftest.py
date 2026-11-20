from __future__ import annotations

import pytest

from adas_mcp import client as client_module
from adas_mcp.tools import schema as schema_module


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAS_BASE_URL", "http://adas.test")
    monkeypatch.setenv("ADAS_API_KEY", "test-admin-key")
    monkeypatch.delenv("ADAS_USER_JWT", raising=False)
    monkeypatch.delenv("ADAS_CAPABILITIES", raising=False)
    monkeypatch.delenv("ADAS_ENABLE_EXECUTE_SQL", raising=False)
    client_module.reset_client_for_tests()
    schema_module.reset_cache_for_tests()
    yield
    client_module.reset_client_for_tests()
    schema_module.reset_cache_for_tests()
