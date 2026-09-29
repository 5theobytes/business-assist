"""GCP credentials bootstrap.

На Render секреты — env-строки. Google SDK ждёт GOOGLE_APPLICATION_CREDENTIALS
указывающий на JSON-файл сервис-аккаунта. Этот модуль собирает JSON из
отдельных env-полей (GCP_PROJECT_ID, GCP_CLIENT_EMAIL, GCP_PRIVATE_KEY +
optional GCP_PRIVATE_KEY_ID), пишет во временный файл и выставляет env.

Импортируется ДО любого `from google import ...`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_MATERIALISED: Path | None = None


def materialise_gcp_credentials() -> Path | None:
    global _MATERIALISED
    if _MATERIALISED is not None:
        return _MATERIALISED
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return None
    project_id = os.environ.get("GCP_PROJECT_ID")
    client_email = os.environ.get("GCP_CLIENT_EMAIL")
    private_key_raw = os.environ.get("GCP_PRIVATE_KEY")
    if not (project_id and client_email and private_key_raw):
        return None

    private_key = private_key_raw.replace("\\n", "\n")
    payload: dict = {
        "type": "service_account",
        "project_id": project_id,
        "client_email": client_email,
        "private_key": private_key,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    if pk_id := os.environ.get("GCP_PRIVATE_KEY_ID"):
        payload["private_key_id"] = pk_id

    fd, path_str = tempfile.mkstemp(prefix="gcp-sa-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path_str
    _MATERIALISED = Path(path_str)
    return _MATERIALISED


# Call on import so order-of-import bugs can't bite us — every consumer that does
# `from . import bootstrap  # side-effect` then `from google.cloud import firestore`
# gets credentials materialised by the time the second import runs.
materialise_gcp_credentials()
