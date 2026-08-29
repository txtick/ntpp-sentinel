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


def _fake_pg_with_roles(*role_types):
    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [{"role_type": role_type} for role_type in role_types]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return Connection()


def test_kevin_and_jarrett_manager_overrides_retain_full_dashboard(monkeypatch):
    monkeypatch.setattr(
        wb,
        "DASHBOARD_MANAGER_EMAILS",
        {
            "kevin@northtexaspoolpros.com",
            "jarrett@northtexaspoolpros.com",
        },
    )
    for email in (
        "kevin@northtexaspoolpros.com",
        "jarrett@northtexaspoolpros.com",
    ):
        assert wb._dashboard_access_profile({"email": email}) == {
            "role": "manager",
            "landing_view": "home",
            "allowed_views": None,
        }


def test_skimmer_owner_retain_full_dashboard(monkeypatch):
    monkeypatch.setattr(wb, "DASHBOARD_MANAGER_EMAILS", set())
    monkeypatch.setattr(wb, "pg", lambda: _fake_pg_with_roles("Owner"))
    assert wb._dashboard_access_profile({"email": "jim@northtexaspoolpros.com"})[
        "role"
    ] == "manager"


def test_active_skimmer_tech_gets_rollover_only_access(monkeypatch):
    monkeypatch.setattr(wb, "DASHBOARD_MANAGER_EMAILS", set())
    monkeypatch.setattr(wb, "pg", lambda: _fake_pg_with_roles("Tech"))
    assert wb._dashboard_access_profile({"email": "john@northtexaspoolpros.com"}) == {
        "role": "technician",
        "landing_view": "route-rollover",
        "allowed_views": ["route-rollover"],
    }


def test_technician_cannot_use_manager_dashboard_api(monkeypatch):
    request = _request(
        {"dashboard_user": {"email": "john@northtexaspoolpros.com"}}
    )
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_ID", "client")
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_SECRET", "secret")
    monkeypatch.setattr(
        wb,
        "_dashboard_access_profile",
        lambda _user: {
            "role": "technician",
            "landing_view": "route-rollover",
            "allowed_views": ["route-rollover"],
        },
    )

    try:
        wb._dashboard_read_auth_or_401(request)
    except Exception as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Technician dashboard access should fail closed")


def test_rollover_login_guard_allows_authenticated_technician(monkeypatch):
    request = _request(
        {"dashboard_user": {"email": "john@northtexaspoolpros.com"}}
    )
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_ID", "client")
    monkeypatch.setattr(wb, "GOOGLE_DASHBOARD_CLIENT_SECRET", "secret")
    wb._dashboard_login_auth_or_401(request)
