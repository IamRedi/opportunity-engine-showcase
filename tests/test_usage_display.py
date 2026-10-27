"""OE-003D user display, localization and redacted admin diagnostics."""
import json

from fastapi.testclient import TestClient
import pytest

import app
import auth_service
import config
import context
import i18n
import repository as repo
import usage_ledger
import usage_presenter
import usage_runtime


ONE = context.ActorContext(1, 1, "owner")


@pytest.fixture(autouse=True)
def isolated_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "usage-display.sqlite3")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "fixture-usage-secret-at-least-32-characters")
    repo.init()


def _activate():
    with repo.db() as con:
        con.execute("UPDATE settings SET value='active' WHERE key='usage_enforcement_state'")


def _card():
    usage_ledger.register_price_card(
        "display-search-v1", "search", "fixture", "EUR", 100, "fetch",
        {"fixed_amount": 25, "max_redirects": 0, "billing_mode": "per_fetch"},
        "2026-01-01T00:00:00+00:00", ctx=ONE,
    )


def _spend(actor, amount=25):
    held = usage_ledger.reserve(
        "display-search", "fixture", "search", "display-search-v1", amount,
        {"query_hash": f"fixture-{actor.user_id}"}, ctx=actor,
    )
    claim = usage_ledger.claim(held["handle"], "display-worker", ctx=actor)
    usage_ledger.settle(claim, amount, {"count_method": "fixture"}, ctx=actor)


def test_user_view_is_scoped_to_current_actor():
    second_id = auth_service.create_user(
        "usage-two@example.org", "fixture-password-long-enough", tenant_id=1)
    two = context.ActorContext(second_id, 1, "member")
    _card()
    usage_ledger.set_period_limit("search", "EUR", 100, 100, "display-v1", ctx=ONE)
    usage_ledger.set_period_limit("search", "EUR", 100, 300, "display-v1", ctx=two)
    _spend(ONE)
    _activate()

    one = next(v for v in usage_presenter.user_view(ONE)["meters"] if v["meter"] == "search")
    other = next(v for v in usage_presenter.user_view(two)["meters"] if v["meter"] == "search")
    assert (one["committed"], one["limit"], one["available"]) == (25, 100, 75)
    assert (other["committed"], other["limit"], other["available"]) == (0, 300, 300)
    assert "usage-two@example.org" not in json.dumps(one)


def test_admin_diagnostics_are_aggregate_and_redacted():
    second_id = auth_service.create_user(
        "private-usage@example.org", "fixture-password-long-enough", tenant_id=1)
    two = context.ActorContext(second_id, 1, "member")
    _card()
    usage_ledger.set_period_limit("search", "EUR", 100, 100, "display-v1", ctx=ONE)
    usage_ledger.set_period_limit("search", "EUR", 100, 100, "display-v1", ctx=two)
    _spend(two)
    _activate()

    diagnostic = usage_presenter.admin_view(ONE)
    assert diagnostic["budget_accounts"] == 2
    assert diagnostic["requests"] == diagnostic["settled"] == 1
    assert set(diagnostic) == {
        "enforcement", "schema_ready", "budget_accounts", "requests",
        "settled", "in_progress", "unresolved",
    }
    serialized = json.dumps(diagnostic)
    assert "private-usage@example.org" not in serialized
    assert "user_id" not in serialized and "tenant_id" not in serialized
    assert usage_presenter.can_view_admin(ONE)
    assert not usage_presenter.can_view_admin(two)
    with pytest.raises(PermissionError):
        usage_presenter.admin_view(two)


@pytest.mark.parametrize("locale, heading, unavailable", [
    ("sq", "Përdorimi dhe limitet", "përkohësisht joaktive"),
    ("en", "Usage and limits", "temporarily inactive"),
    ("de", "Nutzung und Limits", "vorübergehend inaktiv"),
])
def test_profile_usage_section_is_fully_localized(locale, heading, unavailable):
    with TestClient(app.app) as client:
        response = client.get("/profile", params={"lang": locale})
    assert response.status_code == 200
    assert heading in response.text
    assert unavailable in response.text
    assert "budget-exhausted" not in response.text


def test_usage_api_localizes_copy_and_never_returns_ledger_rows():
    with TestClient(app.app) as client:
        response = client.get("/api/usage", headers={"X-UI-Language": "de"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["meters"][0]["label"] == "AI für Profil und Dokumente"
    assert "Anbieterrechnung" in payload["estimate_notice"]
    serialized = json.dumps(payload)
    for forbidden in ("request_id", "user_id", "tenant_id", "input_fingerprint", "usage_meta"):
        assert forbidden not in serialized


@pytest.mark.parametrize("locale, expected", [
    ("sq", "Limiti i këtij muaji"),
    ("en", "This month's limit"),
    ("de", "Das Limit für diesen Monat"),
])
def test_exhausted_budget_message_is_localized_without_internal_code(locale, expected):
    token = i18n._locale.set(locale)
    try:
        message = i18n.translate(usage_runtime.message_key(usage_ledger.UsageDenied("budget-exhausted")))
    finally:
        i18n._locale.reset(token)
    assert expected in message
    assert "budget-exhausted" not in message
