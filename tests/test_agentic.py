import json

import pytest
from django.test import override_settings

from orbit.models import OrbitEntry

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_entry(db):
    return OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_REQUEST,
        family_hash="fam-agentic",
        duration_ms=320.0,
        payload={
            "method": "POST",
            "path": "/checkout/",
            "full_path": "/checkout/?debug=1",
            "status_code": 500,
            "headers": {"Authorization": "Bearer secret", "Accept": "application/json"},
            "body": {"email": "user@example.com", "password": "secret"},
            "query_count": 3,
            "duplicate_query_count": 2,
        },
    )


@pytest.fixture
def related_entries(db, request_entry):
    slow_query = OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_QUERY,
        family_hash="fam-agentic",
        duration_ms=900.0,
        payload={
            "sql": "SELECT * FROM orders WHERE user_id = %s",
            "params": [123],
            "duration_ms": 900.0,
            "is_slow": True,
            "is_duplicate": True,
            "duplicate_count": 2,
            "caller": {
                "filename": "/app/orders/views.py",
                "lineno": 42,
                "function": "checkout",
            },
        },
    )
    log = OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_LOG,
        family_hash="fam-agentic",
        payload={"level": "ERROR", "message": "payment token rejected"},
    )
    exception = OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_EXCEPTION,
        family_hash="fam-agentic",
        fingerprint="fp-checkout",
        payload={
            "exception_type": "ValueError",
            "message": "payment token rejected",
            "traceback": [
                {
                    "filename": "/app/orders/views.py",
                    "lineno": 45,
                    "name": "checkout",
                    "line": "raise ValueError()",
                }
            ],
            "traceback_string": "very long traceback",
        },
    )
    return slow_query, log, exception


def test_agent_safe_serialize_masks_and_truncates_payload(request_entry):
    from orbit.agentic import agent_safe_serialize_entry

    data = agent_safe_serialize_entry(request_entry, max_payload_chars=180)

    assert data["id"] == str(request_entry.id)
    assert data["summary"] == request_entry.summary
    assert data["payload_truncated"] is True
    encoded = json.dumps(data)
    assert "secret" not in encoded
    assert "***HIDDEN***" in encoded


def test_agent_safe_serialize_can_omit_payload(request_entry):
    from orbit.agentic import agent_safe_serialize_entry

    data = agent_safe_serialize_entry(request_entry, include_payload=False)

    assert "payload" not in data
    assert data["payload_omitted"] is True


def test_audit_mcp_exposure_reports_safety_config(settings):
    from orbit.agentic import audit_mcp_exposure

    settings.ORBIT_CONFIG = {
        "MCP_ENABLED": True,
        "MCP_INCLUDE_PAYLOADS": False,
        "MCP_MAX_LIMIT": 25,
    }

    data = audit_mcp_exposure()

    assert data["mcp_enabled"] is True
    assert data["include_payloads"] is False
    assert data["max_limit"] == 25
    assert data["safety"]["serializer"] == "agent_safe_serialize_entry"


def test_investigate_request_builds_diagnosis(request_entry, related_entries):
    from orbit.agentic import investigate_request

    data = investigate_request("fam-agentic")

    assert data["family_hash"] == "fam-agentic"
    assert data["request"]["id"] == str(request_entry.id)
    assert data["diagnosis"]["severity"] == "error"
    assert "exception" in data["diagnosis"]["signals"]
    assert "slow_query" in data["diagnosis"]["signals"]
    assert data["query_analysis"]["slow_count"] == 1
    assert data["query_analysis"]["duplicate_count"] == 1
    assert data["event_counts"] == {"request": 1, "query": 1, "log": 1, "exception": 1}


def test_investigate_exception_group_summarizes_blast_radius(
    request_entry, related_entries
):
    from orbit.agentic import investigate_exception_group

    data = investigate_exception_group("fp-checkout")

    assert data["fingerprint"] == "fp-checkout"
    assert data["count"] == 1
    assert data["representative"]["type"] == "exception"
    assert data["affected_paths"] == [{"path": "/checkout/", "count": 1}]
    assert data["diagnosis"]["severity"] == "error"


def test_create_incident_bundle_from_request(request_entry, related_entries):
    from orbit.agentic import create_incident_bundle

    data = create_incident_bundle("family_hash", "fam-agentic")

    assert data["source"] == {"type": "family_hash", "value": "fam-agentic"}
    assert data["primary"]["family_hash"] == "fam-agentic"
    assert data["agent_handoff"]["recommended_next_actions"]
    assert data["safety_report"]["payloads_masked"] is True


def test_create_incident_bundle_markdown_from_request(request_entry, related_entries):
    from orbit.agentic import create_incident_bundle

    markdown = create_incident_bundle("family_hash", "fam-agentic", format="markdown")

    assert isinstance(markdown, str)
    assert "# Django Orbit Incident Bundle" in markdown
    assert "fam-agentic" in markdown
    assert "payment token rejected" in markdown
    assert "secret" not in markdown


def test_propose_fix_hypotheses_from_exception_group(request_entry, related_entries):
    from orbit.agentic import propose_fix_hypotheses

    data = propose_fix_hypotheses("fingerprint", "fp-checkout")

    assert data["source"] == {"type": "fingerprint", "value": "fp-checkout"}
    assert data["hypotheses"]
    assert data["hypotheses"][0]["confidence"] in {"high", "medium", "low"}
    assert any("orders/views.py" in item for item in data["likely_code_surfaces"])
    assert data["safety"]["does_not_modify_code"] is True


def test_propose_test_plan_from_request(request_entry, related_entries):
    from orbit.agentic import propose_test_plan

    data = propose_test_plan("family_hash", "fam-agentic")

    assert data["source"] == {"type": "family_hash", "value": "fam-agentic"}
    assert data["recommended_tests"]
    assert any(test["type"] == "integration" for test in data["recommended_tests"])
    assert any(
        "checkout" in test["target"].lower() for test in data["recommended_tests"]
    )
    assert data["safety"]["does_not_modify_code"] is True


def test_investigate_endpoint_summarizes_recent_endpoint_health(
    request_entry, related_entries
):
    from orbit.agentic import investigate_endpoint

    OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_REQUEST,
        family_hash="fam-checkout-ok",
        duration_ms=120.0,
        payload={"method": "POST", "path": "/checkout/", "status_code": 200},
    )

    data = investigate_endpoint("/checkout/", method="POST", hours=72)

    assert data["endpoint"] == {"path": "/checkout/", "method": "POST"}
    assert data["request_count"] == 2
    assert data["error_count"] == 1
    assert data["error_rate_pct"] == 50.0
    assert data["avg_duration_ms"] == 220.0
    assert data["slowest_requests"][0]["family_hash"] == "fam-agentic"
    assert data["query_analysis"]["total"] == 1
    assert data["top_exception_groups"][0]["fingerprint"] == "fp-checkout"
    assert "investigate_request" in {tool["tool"] for tool in data["suggested_tools"]}


def test_daily_health_brief_prioritizes_actionable_runtime_signals(
    request_entry, related_entries
):
    from orbit.agentic import daily_health_brief

    OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_JOB,
        family_hash="job-family",
        duration_ms=2000.0,
        payload={"name": "sync_orders", "status": "failed", "error": "timeout"},
    )
    OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_LOG,
        payload={"level": "WARNING", "message": "cache warming skipped"},
    )

    data = daily_health_brief(hours=72, limit=5)

    assert data["hours"] == 72
    assert data["summary"]["requests"] == 1
    assert data["summary"]["exceptions"] == 1
    assert data["summary"]["failed_jobs"] == 1
    assert data["top_issues"][0]["severity"] == "error"
    assert any(issue["type"] == "job_failure" for issue in data["top_issues"])
    assert any(
        "generate_release_risk_brief" == tool["tool"]
        for tool in data["suggested_tools"]
    )


def test_generate_release_risk_brief_flags_blockers(request_entry, related_entries):
    from orbit.agentic import generate_release_risk_brief

    data = generate_release_risk_brief(hours=72)

    assert data["hours"] == 72
    assert data["risk_level"] == "blocker"
    assert "exception_groups" in data["blockers"]
    assert data["checks"]["error_requests"]["count"] == 1
    assert data["checks"]["slow_queries"]["count"] == 1
    assert data["recommendation"].startswith("Do not release")


def test_generate_release_risk_brief_all_clear_without_signals(db):
    from orbit.agentic import generate_release_risk_brief

    OrbitEntry.objects.create(
        type=OrbitEntry.TYPE_REQUEST,
        family_hash="fam-ok",
        duration_ms=80.0,
        payload={"method": "GET", "path": "/health/", "status_code": 200},
    )

    data = generate_release_risk_brief(hours=72)

    assert data["risk_level"] == "low"
    assert data["blockers"] == []
    assert data["recommendation"].startswith("No blocker")


def test_mcp_incident_bundle_markdown_returns_raw_text(request_entry, related_entries):
    from orbit.mcp_server import create_mcp_server
    from tests.test_mcp import _get_tool

    fn = _get_tool(create_mcp_server(), "create_incident_bundle")
    markdown = fn(
        source_type="family_hash", source_value="fam-agentic", format="markdown"
    )

    assert markdown.startswith("# Django Orbit Incident Bundle")
    assert not markdown.startswith('"')


def test_build_debug_brief_matches_ticket_text(request_entry, related_entries):
    from orbit.agentic import build_debug_brief

    data = build_debug_brief("checkout payment token rejected", hours=72)

    assert data["query"] == "checkout payment token rejected"
    assert data["matches"]["exceptions"][0]["id"] == str(related_entries[2].id)
    assert data["matches"]["requests"][0]["id"] == str(request_entry.id)
    assert data["suggested_tools"][0]["tool"] == "create_incident_bundle"


@pytest.mark.django_db
@override_settings(ORBIT_CONFIG={"MCP_ENABLED": True})
def test_high_level_mcp_tools_are_registered():
    from orbit.mcp_server import create_mcp_server
    from tests.test_mcp import _call_tool

    audit = _call_tool(create_mcp_server(), "audit_mcp_exposure")

    assert audit["mcp_enabled"] is True
    assert "investigate_request" in audit["high_level_tools"]
    assert "propose_fix_hypotheses" in audit["high_level_tools"]
    assert "propose_test_plan" in audit["high_level_tools"]
    assert "investigate_endpoint" in audit["high_level_tools"]
    assert "daily_health_brief" in audit["high_level_tools"]
    assert "generate_release_risk_brief" in audit["high_level_tools"]
