"""Tests for scripts/drive_sync.upsert_markdown_in_folder — mock Drive API."""
from __future__ import annotations

from unittest.mock import MagicMock

from scripts.drive_sync import upsert_markdown_in_folder


def _build_mock_drive(*, list_response: dict, view_link: str = "https://drive/view/abc"):
    """Build a MagicMock Drive service that returns `list_response` on files().list().execute()."""
    drive = MagicMock()
    list_call = drive.files().list.return_value
    list_call.execute.return_value = list_response
    drive.files().get.return_value.execute.return_value = {"webViewLink": view_link}
    drive.files().create.return_value.execute.return_value = {"id": "new-file-id"}
    drive.files().update.return_value.execute.return_value = {"id": "existing-file-id"}
    drive.files.reset_mock()  # reset call counts after setup
    return drive


def test_upsert_creates_when_no_existing_file():
    drive = _build_mock_drive(list_response={"files": []})

    link = upsert_markdown_in_folder(drive, "folder-123", "sid_business.md", "# Hello")

    # files().create() should have been called (not update)
    drive.files().create.assert_called_once()
    create_kwargs = drive.files().create.call_args.kwargs
    assert create_kwargs["body"]["name"] == "sid_business.md"
    assert "folder-123" in create_kwargs["body"]["parents"]
    assert create_kwargs["body"]["mimeType"] == "text/markdown"
    drive.files().update.assert_not_called()
    assert link == "https://drive/view/abc"


def test_upsert_updates_when_file_exists():
    drive = _build_mock_drive(list_response={
        "files": [{"id": "existing-file-id", "name": "sid_business.md",
                   "webViewLink": "https://drive/view/old"}],
    })

    link = upsert_markdown_in_folder(drive, "folder-123", "sid_business.md", "# Updated")

    # files().update() should have been called (not create)
    drive.files().update.assert_called_once()
    update_kwargs = drive.files().update.call_args.kwargs
    assert update_kwargs["fileId"] == "existing-file-id"
    drive.files().create.assert_not_called()
    assert link == "https://drive/view/abc"


def test_upsert_query_escapes_apostrophes():
    """Filename / folder ID with apostrophes should not break the Drive query."""
    drive = _build_mock_drive(list_response={"files": []})
    upsert_markdown_in_folder(drive, "folder'with'quote", "file'with'quote.md", "x")
    list_kwargs = drive.files().list.call_args.kwargs
    # Apostrophes inside identifiers must be escaped (\') in the query
    assert "\\'" in list_kwargs["q"]
