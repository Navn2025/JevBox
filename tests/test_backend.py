import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.oauth2.credentials import Credentials

from app.classifier import jev
from app.gmail.client import list_messages_page
from app.gmail.parser import parse_message_content, parse_message_metadata
from app.main import (
    ClassificationBatchRequest,
    classify_email_batch,
    email_detail,
    emails_page,
    reset_history,
    usage,
)
from app.store import store


def encoded(value: str) -> str:
    import base64
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def fake_credentials() -> Credentials:
    return Credentials(
        token="access-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


class FakeListRequest:
    def execute(self):
        return {
            "messages": [{"id": "m1"}, {"id": "m2"}],
            "nextPageToken": "page-2",
            "resultSizeEstimate": 42,
        }


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return FakeListRequest()


class FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self):
        self.messages = FakeMessages()

    def users(self):
        return FakeUsers(self.messages)


class GmailPaginationTests(unittest.TestCase):
    def test_page_token_is_forwarded_and_returned(self):
        service = FakeService()
        with patch("app.gmail.client.create_gmail_service", return_value=service):
            result = list_messages_page(object(), max_results=20, page_token="page-1")
        self.assertEqual(service.messages.kwargs["pageToken"], "page-1")
        self.assertEqual(service.messages.kwargs["maxResults"], 20)
        self.assertEqual(result["next_page_token"], "page-2")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.message = {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "metadata snippet",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.com>"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Subject", "value": "Private body test"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "filename": "", "body": {"data": encoded("secret body")}},
                    {"mimeType": "text/html", "filename": "", "body": {"data": encoded("<p>secret body</p>")}},
                    {"mimeType": "application/pdf", "filename": "report.pdf", "body": {"attachmentId": "a1", "size": 99}},
                ],
            },
        }

    def test_metadata_does_not_include_body(self):
        metadata = parse_message_metadata(self.message)
        self.assertNotIn("body", metadata)
        self.assertNotIn("body_text", metadata)
        self.assertEqual(metadata["attachments"][0]["filename"], "report.pdf")

    def test_content_is_only_parsed_by_detail_parser(self):
        content = parse_message_content(self.message)
        self.assertEqual(content["body_text"], "secret body")
        self.assertEqual(content["body_html"], "<p>secret body</p>")


class ClassifierBatchTests(unittest.TestCase):
    def test_api_accepts_a_100_email_batch(self):
        payload = ClassificationBatchRequest(email_ids=[f"m{index}" for index in range(100)])
        self.assertEqual(len(payload.email_ids), 100)

    def test_one_request_contains_metadata_but_never_body(self):
        calls = []

        def answer_set():
            answers = {}
            for index in range(2):
                answers[f"email_{index}_category"] = SimpleNamespace(choice="Work", confidence=.9, probabilities={"Work": .9})
                answers[f"email_{index}_priority"] = SimpleNamespace(choice="Useful", confidence=.8, probabilities={"Useful": .8})
                answers[f"email_{index}_urgency"] = SimpleNamespace(score=2.0, confidence=.7, probabilities={2: .7})
                answers[f"email_{index}_requires_action"] = SimpleNamespace(noul=.2)
                answers[f"email_{index}_has_deadline"] = SimpleNamespace(noul=.1)
            return answers

        class FakeClient:
            def system_one(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(
                    model="jev-test",
                    answers=answer_set(),
                    usage=SimpleNamespace(input_tokens=123, output_tokens=45),
                )

        emails = [
            {"id": "m1", "sender": "a", "subject": "one", "body": "never send this", "labels": []},
            {"id": "m2", "sender": "b", "subject": "two", "body_html": "never send this either", "labels": []},
        ]
        with patch("app.classifier.jev.get_client", return_value=FakeClient()):
            result = jev.classify_emails(emails)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["state"]["emails"]), 2)
        self.assertNotIn("body", str(calls[0]["state"]).lower())
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(result["classifications"]["m1"]["category_details"]["confidence"], .9)


class PageCacheTests(unittest.TestCase):
    def test_repeated_page_uses_cache_without_duplicate_gmail_request(self):
        connection_id = store.create_connection(fake_credentials())
        request = SimpleNamespace(session={"gmail_connection_id": connection_id})
        raw = {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "Subject", "value": "Hello"}]},
        }
        with (
            patch("app.main.list_messages_page", return_value={"messages": [{"id": "m1"}], "next_page_token": None}) as page_mock,
            patch("app.main.get_messages_metadata", return_value=[raw]) as metadata_mock,
        ):
            first = emails_page(request, max_results=20, page_token="page-1")
            second = emails_page(request, max_results=20, page_token="page-1")

        self.assertEqual(first["emails"][0]["classification_status"], "pending")
        self.assertEqual(second["emails"][0]["id"], "m1")
        self.assertEqual(page_mock.call_count, 1)
        self.assertEqual(metadata_mock.call_count, 1)
        store.remove_connection(connection_id)


class ClassificationStateTests(unittest.TestCase):
    def test_reset_erases_history_but_keeps_gmail_connection(self):
        connection_id = store.create_connection(fake_credentials())
        request = SimpleNamespace(session={"gmail_connection_id": connection_id})
        connection = store.get_connection(connection_id)
        store.upsert_email(connection, {
            "id": "reset-me",
            "sender": "sender@example.com",
            "subject": "Reset",
            "labels": [],
        })
        store.save_classification(connection, "reset-me", {
            "category": "Work",
            "priority": "Low",
        })

        result = reset_history(request)

        self.assertTrue(result["reset"])
        self.assertIsNotNone(store.get_connection(connection_id))
        self.assertIsNone(store.get_email(connection, "reset-me"))
        self.assertEqual(usage(request)["emails_classified"], 0)
        store.remove_connection(connection_id)

    def test_credentials_and_classification_survive_runtime_cache_reset(self):
        connection_id = store.create_connection(fake_credentials())
        connection = store.get_connection(connection_id)
        store.upsert_email(connection, {
            "id": "persistent-message",
            "sender": "sender@example.com",
            "subject": "Already classified",
            "labels": ["INBOX"],
        })
        store.save_classification(connection, "persistent-message", {
            "category": "Work",
            "priority": "Useful",
            "urgency": 1.0,
            "requires_action": .1,
            "has_deadline": .1,
        })

        # Simulate a process restart by removing only the runtime object.
        with store._lock:
            store._connections.pop(connection_id, None)

        restored = store.get_connection(connection_id)
        restored_email = store.get_email(restored, "persistent-message")
        self.assertIsNotNone(restored)
        self.assertEqual(restored.credentials.refresh_token, "refresh-token")
        self.assertEqual(restored_email.classification_status, "classified")
        self.assertEqual(restored_email.classification["category"], "Work")
        store.remove_connection(connection_id)

    def test_classified_email_is_never_sent_twice_and_usage_is_aggregated(self):
        connection_id = store.create_connection(fake_credentials())
        request = SimpleNamespace(session={"gmail_connection_id": connection_id})
        connection = store.get_connection(connection_id)
        for message_id in ("m1", "m2"):
            store.upsert_email(connection, {
                "id": message_id,
                "sender": "sender@example.com",
                "recipient": "me@example.com",
                "subject": message_id,
                "timestamp": "now",
                "labels": ["INBOX"],
                "attachments": [],
            })

        fake_result = {
            "model": "jev-test",
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "classifications": {
                message_id: {
                    "category": "Work",
                    "category_details": {"confidence": .9},
                    "priority": "Useful",
                    "priority_details": {"confidence": .8},
                    "urgency": 2.0,
                    "urgency_details": {"confidence": .7},
                    "requires_action": .2,
                    "has_deadline": .1,
                }
                for message_id in ("m1", "m2")
            },
        }

        with patch("app.main.classify_emails", return_value=fake_result) as classify_mock:
            first = classify_email_batch(ClassificationBatchRequest(email_ids=["m1", "m2"]), request)
            second = classify_email_batch(ClassificationBatchRequest(email_ids=["m1", "m2"]), request)

        self.assertEqual(classify_mock.call_count, 1)
        self.assertEqual(first["usage"]["total_tokens"], 120)
        self.assertIsInstance(first["emails"][0]["classification"]["classification_latency_ms"], float)
        self.assertIsNone(second["usage"])
        summary = usage(request)
        self.assertEqual(summary["emails_classified"], 2)
        self.assertEqual(summary["jev_requests"], 1)
        self.assertEqual(summary["total_tokens"], 120)
        self.assertEqual(summary["total_classification_time_ms"], first["usage"]["latency_ms"])
        store.remove_connection(connection_id)

    def test_full_body_is_loaded_only_by_detail_endpoint(self):
        connection_id = store.create_connection(fake_credentials())
        request = SimpleNamespace(session={"gmail_connection_id": connection_id})
        connection = store.get_connection(connection_id)
        store.upsert_email(connection, {"id": "m1"})
        full_message = {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Opened"}],
                "body": {"data": encoded("body fetched on click")},
            },
        }
        with patch("app.main.get_message", return_value=full_message) as get_mock:
            result = email_detail("m1", request)
        get_mock.assert_called_once_with(connection.credentials, "m1", format="full")
        self.assertEqual(result["body_text"], "body fetched on click")
        store.remove_connection(connection_id)


if __name__ == "__main__":
    unittest.main()
