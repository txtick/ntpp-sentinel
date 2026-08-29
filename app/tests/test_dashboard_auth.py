import asyncio
import os
import sys

from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import web_backend_main as wb


def _request(session=None):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/google/callback",
            "headers": [],
            "session": session or {},
        }
    )


def test_missing_mobile_oauth_session_restarts_login_once(monkeypatch):
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_ID", "client")
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_SECRET", "secret")

    response = asyncio.run(wb.auth_google_callback(_request(), state="first-attempt"))

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/google/start?retry=1"


def test_second_mobile_oauth_session_failure_returns_to_login_page(monkeypatch):
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_ID", "client")
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_SECRET", "secret")

    response = asyncio.run(
        wb.auth_google_callback(
            _request(),
            state=f"{wb.OAUTH_RECOVERY_STATE_PREFIX}second-attempt",
        )
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?auth_error=session"
