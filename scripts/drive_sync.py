"""Загружает markdown-файлы в указанную папку Google Drive (idempotent upsert).

Используется в `scripts/sync_to_sheets.py` для выгрузки больших спек, которые не
помещаются в ячейки Sheets и неудобно редактировать в строке таблицы.

Auth — тот же service-account JSON, что и для Firestore (через bootstrap.py).
Service account нужен **Editor**-доступ к целевой папке Drive.
"""
from __future__ import annotations

import io
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_drive_service(creds_path: str | None = None):
    """Build a Drive v3 service client from the materialised service-account JSON."""
    creds_path = creds_path or os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_file_in_folder(drive, folder_id: str, filename: str) -> dict | None:
    """Return file metadata if a file with given name exists in folder, else None."""
    safe_name = filename.replace("'", "\\'")
    safe_folder = folder_id.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{safe_folder}' in parents and trashed = false"
    )
    resp = drive.files().list(
        q=query, fields="files(id, name, webViewLink)", pageSize=1,
    ).execute()
    files = resp.get("files") or []
    return files[0] if files else None


def upsert_markdown_in_folder(
    drive, folder_id: str, filename: str, content: str,
) -> str:
    """Создать или обновить .md-файл в указанной папке Drive. Возвращает webViewLink.

    Идемпотентно: если файл с таким именем в папке уже есть — переписываем content.
    Если нет — создаём новый.
    """
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    existing = _find_file_in_folder(drive, folder_id, filename)
    if existing:
        file_id = existing["id"]
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        body = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "text/markdown",
        }
        created = drive.files().create(body=body, media_body=media, fields="id").execute()
        file_id = created["id"]

    meta = drive.files().get(fileId=file_id, fields="webViewLink").execute()
    return meta.get("webViewLink", "")
