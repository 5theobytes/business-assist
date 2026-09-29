# BizWare / Business Assist

An AI-assisted business discovery tool that turns a guided conversation into a clear view of a business process and practical implementation documents.

Run the tool on your own server or locally, with your own API keys and a self-hosted web interface.

## What it does

- Guides a business owner through a structured discovery conversation in Russian or English.
- Shows conversation progress in the web chat and Journey view; users can revisit earlier steps.
- Produces a business clarity summary, a business brief, and an implementation specification when the selected workflow supports them.
- Supports multiple workflow versions and configurable LLM backends.
- Can store sessions in memory for local development or in a Firestore database configured by the operator.
- Includes optional Telegram, Google Sheets, Google Drive, and Cloudflare Turnstile integrations.
- Includes deterministic offline personas and an evaluation harness so maintainers can compare workflow behavior without calling an LLM provider.

## Quick start

Requirements: Python 3.11 or newer.

```bash
git clone https://github.com/5theobytes/business-assist.git
cd business-assist
python -m venv .venv
```

Activate the environment, then install dependencies:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, use `Copy-Item .env.example .env` instead of `cp` if the alias is unavailable.

For a local run, set these values in `.env`:

```dotenv
LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=your-own-key
SESSION_STORE=memory
```

The key is read by the server from the local environment. Do not put it in source code, screenshots, issues, or commits. Provider usage is billed to the account that owns the key.

Start the app from the repository root:

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` to start the web tool intake flow (the root redirects to `/intake`). Journey Mode is available at `http://127.0.0.1:8000/journey` after a session starts. With `SESSION_STORE=memory`, sessions are lost when the process stops. For durable sessions, configure a dedicated Firestore database and service credentials as described in [configuration](#configuration).

## Configuration

`.env.example` contains variable names and safe defaults only. Copy it to `.env`, then set only the integrations you use. `.env` and `.env.test` are ignored by Git.

| Use | Settings |
| --- | --- |
| Anthropic API | `LLM_BACKEND=anthropic`, `ANTHROPIC_API_KEY`, optional `ANTHROPIC_MODEL` |
| Gemini on Vertex AI | `LLM_BACKEND=gemini`, `GCP_PROJECT_ID` or `VERTEX_PROJECT_ID`, Google application credentials, optional `GEMINI_MODEL` and `GEMINI_LOCATION` |
| Claude on Vertex AI | `LLM_BACKEND=vertex`, Google application credentials, optional `VERTEX_MODEL` and `VERTEX_REGION` |
| Session persistence | `SESSION_STORE=memory` for local runs, or Firestore configuration with a dedicated database ID in `FIRESTORE_DATABASE` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and a reachable `PUBLIC_URL` when registering a webhook |
| Google Sheets | `GOOGLE_SHEETS_ID` and a service account with access to that sheet |
| Intake protection | `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` |

The exact supported settings are documented in `.env.example` and the integration modules. Do not point tests or experiments at a production Firestore project. Live tests require an explicit opt-in and a dedicated staging database.

## Tests and evaluations

The repository includes both offline checks and live integration checks. Offline checks do not require provider credentials. Run the offline evaluation harness against a specific workflow, for example:

```bash
python -m app.eval.competition --workflows v4 --persona Анна
```

Evaluation reports and raw transcripts are written under `eval_runs/`, which is ignored by Git. Use synthetic personas only; do not place real customer conversations or personal data in fixtures or reports.

Live provider and Firestore checks can incur charges and create records in your configured staging services. They are separate from the default local workflow; see [tests/README.md](tests/README.md) before enabling them.

## Add a business niche to the evaluation set

Start with [the evaluation guide](docs/evaluation/adding-a-business-niche.md). It describes how to verify source claims, create a fictional persona with explicit ground truth, run the offline workflow simulation, and review the result before adding it to regression coverage. Optional Claude Code helpers are in `.claude/`; the app does not depend on Claude Code.

## Data handling

BizWare may collect a participant's name, email address, age range, gender, sector, and business-process details through its intake and conversation flows. Depending on configuration, session content can be sent to the LLM provider and stored in Firestore, Google Sheets, or Telegram integrations selected by the operator. Review [DATA-AND-PRIVACY.md](DATA-AND-PRIVACY.md) before using the tool with real people.

This is self-hosted software. The person operating an instance is responsible for its access controls, retention, deletion, backups, provider agreements, and any notices or consent required for their users. The repository does not include a hosted BizWare service.

## Deployment

`render.yaml` is an optional starting point for a new Render service. Review its region, plan, environment variables, database settings, and access controls before deploying. Use dedicated credentials for each instance.

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities. Do not expose a new instance to the public internet until you have configured the protections appropriate for your deployment.

## License

BizWare is distributed under the MIT License. See [LICENSE](LICENSE).
