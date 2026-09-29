from __future__ import annotations

from tests import conftest


def _skip_reason(markers: set[str], env: dict[str, str]) -> str | None:
    gate = getattr(conftest, "live_test_skip_reason", lambda *_args: None)
    return gate(markers, env)


def test_live_tests_require_explicit_opt_in():
    assert _skip_reason({"integration"}, {}) == (
        "live tests are disabled; set BIZWARE_RUN_LIVE_TESTS=1 to enable"
    )


def test_firestore_tests_reject_default_database():
    env = {
        "BIZWARE_RUN_LIVE_TESTS": "1",
        "GCP_PROJECT_ID": "staging-project",
        "FIRESTORE_DATABASE": "(default)",
    }
    assert _skip_reason({"integration", "requires_firestore"}, env) == (
        "requires a named non-default FIRESTORE_DATABASE"
    )


def test_firestore_tests_require_project_credentials():
    env = {
        "BIZWARE_RUN_LIVE_TESTS": "1",
        "FIRESTORE_DATABASE": "bizware-staging-test",
    }
    assert _skip_reason({"integration", "requires_firestore"}, env) == (
        "requires GOOGLE_APPLICATION_CREDENTIALS or GCP_PROJECT_ID"
    )


def test_firestore_tests_allow_named_database_with_project_credentials():
    env = {
        "BIZWARE_RUN_LIVE_TESTS": "1",
        "GCP_PROJECT_ID": "staging-project",
        "FIRESTORE_DATABASE": "bizware-staging-test",
    }
    assert _skip_reason({"integration", "requires_firestore"}, env) is None


def test_anthropic_tests_require_provider_key():
    env = {"BIZWARE_RUN_LIVE_TESTS": "1"}
    assert _skip_reason({"integration", "requires_anthropic"}, env) == (
        "requires ANTHROPIC_API_KEY"
    )


def test_offline_test_configuration_uses_memory_store():
    env: dict[str, str] = {}
    configure = getattr(conftest, "configure_test_environment", lambda _env: None)
    configure(env)
    assert env["SESSION_STORE"] == "memory"


def test_live_test_configuration_preserves_selected_store():
    env = {"BIZWARE_RUN_LIVE_TESTS": "1", "SESSION_STORE": "firestore"}
    configure = getattr(conftest, "configure_test_environment", lambda _env: None)
    configure(env)
    assert env["SESSION_STORE"] == "firestore"
