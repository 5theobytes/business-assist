# Test and evaluation modes

The default test run should stay offline and must not need API keys or reach a live BizWare server. Live checks are separate because they can incur provider charges and create data in cloud services.

## Offline checks

```bash
python -m pytest tests/unit
python -m app.eval.competition --workflows v4
```

The deterministic evaluation uses synthetic personas and a mock model. Outputs go to `eval_runs/`, which is ignored by Git.

## Live integration checks

Live checks require both explicit opt-in and credentials for services you own:

```bash
# Set in the shell, then run pytest from the repository root.
$env:BIZWARE_RUN_LIVE_TESTS = "1"
python -m pytest -m integration
```

Create `.env.test` from `.env.example` only for your own staging environment. For any Firestore-backed test, configure a dedicated non-default `FIRESTORE_DATABASE` and a staging GCP project. Do not set these tests up with production credentials. Live LLM tests use your own provider key. Speech-to-text checks also need your own GCP credentials and a test audio fixture.

For Firestore-backed live tests, also set `SESSION_STORE=firestore` in `.env.test`; the example file defaults to memory for local use.

`tests/integration/test_full_turn_e2e.py` and Firestore tests create and then remove synthetic test records in the configured test collection. A failed run may leave a record behind; review and clean the dedicated staging database as needed. The live server driver also persists sessions and cannot delete them through the application API.

If `BIZWARE_RUN_LIVE_TESTS` is absent or not `1`, live tests are skipped even when `.env.test` exists. The tests must also skip when a required credential or named Firestore database is missing.
