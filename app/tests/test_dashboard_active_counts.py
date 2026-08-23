from pathlib import Path


def _dashboard_schema_text() -> str:
    return (Path(__file__).resolve().parents[1] / "services" / "dashboard_schema.py").read_text()


def test_dashboard_summary_counts_weekly_service_pools_not_all_active_records():
    text = _dashboard_schema_text()
    summary_section = text.split("CREATE OR REPLACE VIEW dashboard_summary_v", 1)[1]

    assert "dashboard_weekly_service_pools_v" in summary_section
    assert "COUNT(DISTINCT customer_id)" in summary_section
    assert "COUNT(DISTINCT source_service_location_id)" in summary_section
    assert "COUNT(*) FROM customers WHERE is_operationally_active = TRUE" not in summary_section


def test_weekly_service_pool_scope_excludes_non_weekly_tags_and_requires_current_route():
    text = _dashboard_schema_text()
    weekly_view = text.split("CREATE OR REPLACE VIEW dashboard_weekly_service_pools_v", 1)[1].split(
        "CREATE OR REPLACE VIEW problem_pools_v",
        1,
    )[0]

    assert "service_location_technician_assignments" in weekly_view
    assert "route_assignment_is_weekly(a.frequency)" in weekly_view
    assert "customer_has_tag(c.raw_json, 'service-only')" in weekly_view
    assert "customer_has_tag(c.raw_json, 'inspection')" in weekly_view
    assert "a.is_deleted = FALSE" in weekly_view
    assert "a.end_date IS NULL OR a.end_date >= CURRENT_DATE" in weekly_view


def test_problem_pools_uses_weekly_service_pool_scope():
    text = _dashboard_schema_text()
    problem_pools_view = text.split("CREATE OR REPLACE VIEW problem_pools_v", 1)[1].split(
        "CREATE OR REPLACE VIEW dashboard_summary_v",
        1,
    )[0]

    assert "JOIN dashboard_weekly_service_pools_v wsp ON wsp.pool_id = p.id" in problem_pools_view


def test_assignment_current_location_index_exists():
    pipeline = (Path(__file__).resolve().parents[1] / "ingest" / "pipeline.py").read_text()

    assert "idx_service_location_technician_assignments_location_current" in pipeline


def _dashboard_backend_text() -> str:
    return (Path(__file__).resolve().parents[1] / "services" / "dashboard_backend.py").read_text()


def test_dashboard_summary_includes_customer_flow_metrics():
    text = _dashboard_backend_text()

    assert 'payload["customer_flow"] = _get_customer_flow_metrics(cur)' in text
    assert "VALUES (30), (60), (90)" in text
    assert "created_at" in text
    assert "inactive_since" in text


def test_customer_flow_excludes_non_operational_customer_types():
    text = _dashboard_backend_text()
    flow_section = text.split("def _get_customer_flow_metrics", 1)[1].split(
        "def refresh_alert_instances",
        1,
    )[0]

    assert "COALESCE(c.is_lead, FALSE) = FALSE" in flow_section
    assert "COALESCE(c.has_pool, FALSE) = TRUE" in flow_section
    assert "customer_has_tag(c.raw_json, 'service-only')" in flow_section
    assert "customer_has_tag(c.raw_json, 'inspection')" in flow_section