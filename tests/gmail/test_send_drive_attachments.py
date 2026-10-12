"""Tests for attaching Google Drive files to send_gmail_message."""

import base64
import email
import os
import sys
from email import policy
from unittest.mock import AsyncMock, Mock

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gdrive.drive_helpers as drive_helpers  # noqa: E402
import gmail.gmail_tools as gmail_tools  # noqa: E402
from core.utils import UserInputError  # noqa: E402
from gdrive.drive_helpers import (  # noqa: E402
    GMAIL_MAX_ATTACHMENT_BYTES,
    fetch_drive_files_as_attachments,
)

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _FakeDownloader:
    """Stands in for MediaIoBaseDownload: writes request.payload in chunks."""

    def __init__(self, fd, request, chunksize=1024):
        self._fd = fd
        self._payload = request.payload
        self._chunk = chunksize
        self._pos = 0

    def next_chunk(self):
        piece = self._payload[self._pos : self._pos + self._chunk]
        self._fd.write(piece)
        self._pos += len(piece)
        return None, self._pos >= len(self._payload)


@pytest.fixture(autouse=True)
def fake_downloader(monkeypatch):
    monkeypatch.setattr(drive_helpers, "MediaIoBaseDownload", _FakeDownloader)
    # Small chunks so the streaming size guard is exercised.
    monkeypatch.setattr(drive_helpers, "_DOWNLOAD_CHUNK_SIZE", 1024)


def _drive_service(files):
    """Drive mock. ``files`` maps file_id -> (metadata dict, payload bytes)."""
    service = Mock()
    files_api = service.files.return_value

    def _get(fileId, fields, supportsAllDrives):
        if fileId not in files:
            resp = Mock(status=404, reason="Not Found")
            request = Mock()
            request.execute.side_effect = HttpError(resp, b'{"error": "notFound"}')
            return request
        metadata = dict(files[fileId][0])
        metadata.setdefault("id", fileId)
        request = Mock()
        request.execute.return_value = metadata
        return request

    def _media_request(fileId, **_kwargs):
        request = Mock()
        request.payload = files[fileId][1]
        return request

    files_api.get.side_effect = _get
    files_api.export_media.side_effect = _media_request
    files_api.get_media.side_effect = _media_request
    return service


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# fetch_drive_files_as_attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_doc_is_exported_to_pdf():
    service = _drive_service(
        {"doc1": ({"name": "Quarterly report", "mimeType": DOC_MIME}, b"%PDF-1.7 doc")}
    )

    [att] = await fetch_drive_files_as_attachments(service, ["doc1"])

    service.files.return_value.export_media.assert_called_once_with(
        fileId="doc1", mimeType="application/pdf"
    )
    service.files.return_value.get_media.assert_not_called()
    assert att["filename"] == "Quarterly report.pdf"
    assert att["mime_type"] == "application/pdf"
    assert att["data"] == b"%PDF-1.7 doc"


@pytest.mark.asyncio
async def test_google_sheet_is_exported_to_xlsx():
    service = _drive_service(
        {"sheet1": ({"name": "Budget", "mimeType": SHEET_MIME}, b"PK\x03\x04xlsx")}
    )

    [att] = await fetch_drive_files_as_attachments(service, ["sheet1"])

    service.files.return_value.export_media.assert_called_once_with(
        fileId="sheet1", mimeType=XLSX_MIME
    )
    assert att["filename"] == "Budget.xlsx"
    assert att["mime_type"] == XLSX_MIME


@pytest.mark.asyncio
async def test_binary_file_is_downloaded_as_is():
    payload = bytes(range(256)) * 10
    service = _drive_service(
        {
            "png1": (
                {
                    "name": "logo.png",
                    "mimeType": "image/png",
                    "size": str(len(payload)),
                },
                payload,
            )
        }
    )

    [att] = await fetch_drive_files_as_attachments(service, ["png1"])

    service.files.return_value.get_media.assert_called_once_with(
        fileId="png1", supportsAllDrives=True
    )
    service.files.return_value.export_media.assert_not_called()
    assert att == {
        "file_id": "png1",
        "filename": "logo.png",
        "mime_type": "image/png",
        "data": payload,
    }


@pytest.mark.asyncio
async def test_declared_size_over_limit_fails_before_download():
    too_big = GMAIL_MAX_ATTACHMENT_BYTES + 1
    service = _drive_service(
        {
            "big": (
                {"name": "video.mp4", "mimeType": "video/mp4", "size": str(too_big)},
                b"",
            )
        }
    )

    with pytest.raises(UserInputError, match="Gmail size limit of 25.0 MB"):
        await fetch_drive_files_as_attachments(service, ["big"])
    service.files.return_value.get_media.assert_not_called()


@pytest.mark.asyncio
async def test_total_size_counts_other_attachments():
    service = _drive_service(
        {
            "f": (
                {
                    "name": "a.bin",
                    "mimeType": "application/octet-stream",
                    "size": "600",
                },
                b"x" * 600,
            )
        }
    )
    with pytest.raises(UserInputError, match="size limit"):
        await fetch_drive_files_as_attachments(
            service, ["f"], max_total_bytes=1000, already_used_bytes=500
        )


@pytest.mark.asyncio
async def test_export_without_declared_size_is_capped_while_streaming():
    service = _drive_service(
        {"doc": ({"name": "Huge", "mimeType": DOC_MIME}, b"y" * 5000)}
    )
    with pytest.raises(UserInputError, match="more than"):
        await fetch_drive_files_as_attachments(service, ["doc"], max_total_bytes=2048)


@pytest.mark.asyncio
async def test_multiple_files_share_one_budget():
    service = _drive_service(
        {
            "a": (
                {"name": "a.txt", "mimeType": "text/plain", "size": "700"},
                b"a" * 700,
            ),
            "b": (
                {"name": "b.txt", "mimeType": "text/plain", "size": "700"},
                b"b" * 700,
            ),
        }
    )
    with pytest.raises(UserInputError, match="size limit"):
        await fetch_drive_files_as_attachments(
            service, ["a", "b"], max_total_bytes=1000
        )


@pytest.mark.asyncio
async def test_folder_and_unsupported_native_types_are_rejected():
    service = _drive_service(
        {
            "folder": (
                {"name": "Docs", "mimeType": "application/vnd.google-apps.folder"},
                b"",
            ),
            "form": (
                {"name": "Survey", "mimeType": "application/vnd.google-apps.form"},
                b"",
            ),
        }
    )
    with pytest.raises(UserInputError, match="is a folder"):
        await fetch_drive_files_as_attachments(service, ["folder"])
    with pytest.raises(UserInputError, match="cannot be attached"):
        await fetch_drive_files_as_attachments(service, ["form"])


@pytest.mark.asyncio
async def test_missing_file_gives_clear_error():
    service = _drive_service({})
    with pytest.raises(UserInputError, match="not found or you do not have access"):
        await fetch_drive_files_as_attachments(service, ["nope"])


# ---------------------------------------------------------------------------
# send_gmail_message integration
# ---------------------------------------------------------------------------


def _gmail_service():
    service = Mock()
    service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "sent-1"
    }
    return service


def _sent_mime(gmail_service):
    kwargs = (
        gmail_service.users.return_value.messages.return_value.send.call_args.kwargs
    )
    raw = kwargs["body"]["raw"]
    return email.message_from_bytes(
        base64.urlsafe_b64decode(raw), policy=policy.default
    )


@pytest.mark.asyncio
async def test_send_gmail_message_attaches_drive_files(monkeypatch):
    drive = _drive_service(
        {
            "doc1": ({"name": "Offer", "mimeType": DOC_MIME}, b"%PDF offer"),
            "png1": (
                {"name": "chart.png", "mimeType": "image/png", "size": "4"},
                b"\x89PNG",
            ),
        }
    )
    get_service = AsyncMock(return_value=drive)
    monkeypatch.setattr(gmail_tools, "get_additional_google_service", get_service)
    gmail = _gmail_service()

    result = await _unwrap(gmail_tools.send_gmail_message)(
        gmail,
        user_google_email="me@example.com",
        to="you@example.com",
        subject="Offer",
        body="See attached.",
        drive_file_ids=["doc1", "png1"],
    )

    assert result == "Email sent with 2 attachment(s)! Message ID: sent-1"
    get_service.assert_awaited_once_with(
        "drive", "drive_read", "send_gmail_message", "me@example.com"
    )
    drive.close.assert_called_once()

    parts = list(_sent_mime(gmail).iter_attachments())
    assert [(p.get_filename(), p.get_content_type()) for p in parts] == [
        ("Offer.pdf", "application/pdf"),
        ("chart.png", "image/png"),
    ]
    assert parts[0].get_content() == b"%PDF offer"


@pytest.mark.asyncio
async def test_send_gmail_message_without_drive_ids_does_not_touch_drive(monkeypatch):
    get_service = AsyncMock()
    monkeypatch.setattr(gmail_tools, "get_additional_google_service", get_service)
    gmail = _gmail_service()

    result = await _unwrap(gmail_tools.send_gmail_message)(
        gmail,
        user_google_email="me@example.com",
        to="you@example.com",
        subject="Hi",
        body="Plain message",
    )

    assert result == "Email sent! Message ID: sent-1"
    get_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_gmail_message_size_limit_error_closes_drive(monkeypatch):
    drive = _drive_service(
        {
            "big": (
                {
                    "name": "big.zip",
                    "mimeType": "application/zip",
                    "size": str(GMAIL_MAX_ATTACHMENT_BYTES + 10),
                },
                b"",
            )
        }
    )
    monkeypatch.setattr(
        gmail_tools, "get_additional_google_service", AsyncMock(return_value=drive)
    )
    gmail = _gmail_service()

    with pytest.raises(UserInputError, match="size limit"):
        await _unwrap(gmail_tools.send_gmail_message)(
            gmail,
            user_google_email="me@example.com",
            to="you@example.com",
            subject="Too big",
            body="x",
            drive_file_ids=["big"],
        )
    drive.close.assert_called_once()
    gmail.users.return_value.messages.return_value.send.assert_not_called()


def test_large_messages_use_media_upload(monkeypatch):
    monkeypatch.setattr(gmail_tools, "GMAIL_SIMPLE_SEND_MAX_BYTES", 10)
    gmail = _gmail_service()
    raw = base64.urlsafe_b64encode(b"Subject: x\r\n\r\n" + b"z" * 100).decode()

    gmail_tools._build_send_request(gmail, raw, "thread-9")

    kwargs = gmail.users.return_value.messages.return_value.send.call_args.kwargs
    assert kwargs["body"] == {"threadId": "thread-9"}
    media = kwargs["media_body"]
    assert media.mimetype() == "message/rfc822"
    assert media.resumable()


def test_small_messages_use_raw_body():
    gmail = _gmail_service()
    raw = base64.urlsafe_b64encode(b"Subject: x\r\n\r\nhi").decode()

    gmail_tools._build_send_request(gmail, raw, None)

    kwargs = gmail.users.return_value.messages.return_value.send.call_args.kwargs
    assert kwargs == {"userId": "me", "body": {"raw": raw}}
