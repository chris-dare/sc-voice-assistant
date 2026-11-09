import os

# Keep test runs out of Logfire, whatever keys the shell has.
# Tests use in-memory state and fakes, never the real Redis or vendor URLs from the shell.
for _var in ("LOGFIRE_TOKEN", "LOGFIRE_API_KEY", "REDIS_URL", "AGENT_SERVICE_URL", "SUNCO_DELIVERY"):
    os.environ.pop(_var, None)

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def mock_settings(**kw) -> Settings:
    base = dict(
        mock_providers=True,
        auth_secret="test-secret",
        hold_prompt="",
        sunco_poll_interval_s=0.05,
        turn_quiet_window_s=0.1,
        sunco_webhook_secret="hook-secret",
        _env_file=None,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def client():
    with TestClient(create_app(mock_settings())) as c:
        yield c


@pytest.fixture
def token(client):
    return client.post("/v1/session", json={}).json()["token"]
