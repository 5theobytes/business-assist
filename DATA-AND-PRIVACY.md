# Data and privacy

BizWare is self-hosted. The operator chooses the model provider, storage backend, and optional integrations. No public BizWare service is included in this repository.

## Data the application can handle

The intake flow can request a participant's name, email address, age range, gender, business sector, and the business task that takes the most time. The conversation can contain additional business details. The application stores the session profile, transcript, workflow state, and generated documents in its configured session store.

## Where data can go

- **LLM provider:** prompts include conversation context needed to continue the workflow and generate its outputs. The provider is selected through `LLM_BACKEND` and uses the operator's credentials.
- **Firestore:** with the Firestore store enabled, profiles, transcripts, state, classifications, and generated documents are persisted to the configured project, database, and collection.
- **Google Sheets / Drive:** when configured, session summaries or generated documents can be synchronized to the selected Google resources.
- **Telegram:** when configured, the bot processes Telegram updates and links sessions to Telegram chat identifiers.
- **Cloudflare Turnstile:** when configured, the intake challenge token and optional client IP are sent to Cloudflare for verification.

The repository does not configure a telemetry vendor. Hosting providers, model providers, Google, Telegram, and Cloudflare may process operational data according to the operator's deployment and account settings.

## Operator responsibilities

Before using BizWare with real participants, the operator should decide what data is necessary, give participants an appropriate notice, restrict access to the instance and storage, define retention and deletion procedures, secure backups, and confirm that selected providers are suitable for the data. The current application does not provide a user-facing account system or a general-purpose deletion workflow.

For local development, `SESSION_STORE=memory` keeps data in process memory and loses it when the app stops. For durable use, configure a dedicated Firestore database and access credentials. Never use real participant data in evaluation fixtures, screenshots, examples, or issue reports.
