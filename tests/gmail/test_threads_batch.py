"""Regression test: get_gmail_threads_content_batch formats threads.

The upstream removal of get_gmail_thread_content also deleted the shared
_format_thread_content helper, which made every batch call fail with NameError.
"""

import base64
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmail.gmail_tools as gmail_tools  # noqa: E402


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _thread(thread_id, subject, body):
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": thread_id,
        "messages": [
            {
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "Date", "value": "Mon, 12 Oct 2026 10:00:00 +0200"},
                        {"name": "Subject", "value": subject},
                    ],
                    "body": {"data": data},
                }
            }
        ],
    }


@pytest.mark.asyncio
async def test_threads_batch_formats_each_thread(monkeypatch):
    monkeypatch.setattr(gmail_tools, "GMAIL_REQUEST_DELAY", 0)
    threads = {
        "t1": _thread("t1", "Budget", "Numbers attached"),
        "t2": _thread("t2", "Offer", "Final version"),
    }
    service = Mock()
    # Force the sequential fallback path.
    service.new_batch_http_request.side_effect = RuntimeError("no batch")

    def _get(userId, id, format):
        request = Mock()
        request.execute.return_value = threads[id]
        return request

    service.users.return_value.threads.return_value.get.side_effect = _get

    result = await _unwrap(gmail_tools.get_gmail_threads_content_batch)(
        service, thread_ids=["t1", "t2"], user_google_email="me@example.com"
    )

    assert result.startswith("Retrieved 2 threads:")
    assert "Thread ID: t1" in result and "Subject: Budget" in result
    assert "Numbers attached" in result
    assert "Thread ID: t2" in result and "Final version" in result
