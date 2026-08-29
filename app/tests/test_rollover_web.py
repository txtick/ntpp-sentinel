from pathlib import Path

from services import rollover_web


def test_placeholder_completion_is_not_treated_as_serviced():
    assert rollover_web._parse_complete_time("2010-01-01T00:00:00+00:00") is None
    assert rollover_web._parse_complete_time(None) is None
    assert rollover_web._parse_complete_time("2026-08-29T15:30:00+00:00") is not None


def test_message_personalization_replaces_supported_tokens():
    rendered = rollover_web._render_message(
        "Hi {{customer_first_name}}, this is {tech_first_name}.",
        {"customerFirstName": "Susie"},
        "John",
    )
    assert rendered == "Hi Susie, this is John."


def test_public_route_stop_marks_real_completion_only():
    pending = rollover_web._public_stop(
        {
            "id": "stop-1",
            "customerFirstName": "Susie",
            "customerLastName": "Collins",
            "completeTime": "2010-01-01T00:00:00+00:00",
        }
    )
    completed = rollover_web._public_stop(
        {"id": "stop-2", "customerFirstName": "Kevin", "completeTime": "2026-08-29T15:00:00Z"}
    )
    assert pending["completed"] is False
    assert completed["completed"] is True


def test_openai_text_extraction_uses_output_text_content():
    payload = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Draft message"}]}
        ]
    }
    assert rollover_web._extract_openai_text(payload) == "Draft message"


def test_rollover_send_schema_and_idempotency_are_present():
    text = Path(rollover_web.__file__).read_text()
    assert "idempotency_key TEXT NOT NULL UNIQUE" in text
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in text
    assert "A selected customer is already complete" in text
    assert "Multiple GHL conversations matched this phone" in text


def test_web_routes_require_signed_in_email():
    main = (Path(__file__).resolve().parents[1] / "web_backend_main.py").read_text()
    assert '@app.get("/api/rollover/route")' in main
    assert '@app.post("/api/rollover/message/ai")' in main
    assert '@app.post("/api/rollover/send")' in main
    assert "_rollover_user_email(request)" in main


def test_sms_rollover_is_disabled_by_default_after_web_migration():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert 'ROLLOVER_SMS_ENABLED = os.getenv("ROLLOVER_SMS_ENABLED", "0")' in main
    assert "rollover_enabled=ROLLOVER_ENABLED and ROLLOVER_SMS_ENABLED" in main


def test_frontend_has_reviewed_mobile_rollover_flow():
    repo = Path(__file__).resolve().parents[2]
    index = (repo / "web-frontend" / "index.html").read_text()
    app = (repo / "web-frontend" / "app.js").read_text()
    assert 'data-view="route-rollover"' in index
    assert 'api("/api/rollover/route")' in app
    assert 'id="rollover-reviewed"' in app
    assert 'idempotency_key: rolloverUi.submissionId' in app
    assert "Personalized preview" in app


def test_frontend_hides_dashboard_behind_explicit_login_gate():
    repo = Path(__file__).resolve().parents[2]
    index = (repo / "web-frontend" / "index.html").read_text()
    app = (repo / "web-frontend" / "app.js").read_text()
    styles = (repo / "web-frontend" / "styles.css").read_text()
    assert 'id="login-gate"' in index
    assert 'id="login-page-button"' in index
    assert 'id="app-shell" class="shell" hidden' in index
    assert "function renderAuthGate" in app
    assert "checking || (state.auth.enabled && !state.auth.authenticated)" in app
    assert ".shell[hidden]" in styles
