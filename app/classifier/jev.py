from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

load_dotenv()


_client: TypeSafeClient | None = None


def get_client() -> TypeSafeClient:
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        _client = TypeSafeClient(
            api_key=api_key,
            base_url="https://openrouter.ai/api",
        )
    return _client


def build_jev_input(email: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit metadata allow-list that may be sent to JEV."""
    return {
        "id": email["id"],
        "sender": email.get("sender", ""),
        "recipient": email.get("recipient", ""),
        "cc": email.get("cc", ""),
        "bcc": email.get("bcc", ""),
        "subject": email.get("subject", ""),
        "timestamp": email.get("timestamp", ""),
        "labels": list(email.get("labels", [])),
        "attachments": [
            {
                "filename": item.get("filename", ""),
                "mime_type": item.get("mime_type", ""),
                "size": item.get("size", 0),
            }
            for item in email.get("attachments", [])
        ],
    }


def _question_key(index: int, field: str) -> str:
    return f"email_{index}_{field}"


def _answer_details(answer: Any) -> dict[str, Any]:
    details: dict[str, Any] = {}
    confidence = getattr(answer, "confidence", None)
    probabilities = getattr(answer, "probabilities", None)
    if confidence is not None:
        details["confidence"] = confidence
    if probabilities is not None:
        details["probabilities"] = dict(probabilities)
    return details


def classify_emails(emails: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify a metadata-only batch with exactly one System 1 request."""
    if not emails:
        raise ValueError("At least one email is required")

    metadata = [build_jev_input(email) for email in emails]
    questions: dict[str, Any] = {}

    for index, email in enumerate(metadata):
        reference = {"email_id": email["id"]}
        questions[_question_key(index, "category")] = Choice(
            instructions={**reference, "question": "What category does this email metadata indicate?"},
            criteria={
                "Work": "Work-related communication",
                "College": "College, university, academic, or educational communication",
                "Finance": "Banking, payments, transactions, invoices, or financial matters",
                "Personal": "Personal communication",
                "Promotion": "Marketing, offers, discounts, or promotional communication",
                "Newsletter": "Newsletter or subscription updates",
                "Notification": "Automated notifications, alerts, or confirmations",
                "Spam": "Unwanted or suspicious unsolicited email",
                "Other": "Does not fit the other categories",
            },
        )
        questions[_question_key(index, "priority")] = Choice(
            instructions={**reference, "question": "How important is this email?"},
            criteria={
                "Critical": "Requires immediate attention",
                "Important": "Requires attention but is not immediately critical",
                "Useful": "Useful information but does not require prompt attention",
                "Low": "Low importance",
                "Other": "Cannot determine priority",
            },
        )
        questions[_question_key(index, "urgency")] = Score(
            instructions={**reference, "question": "How urgent is this email on a scale from 1 to 10?"},
            criteria=[
                "No urgency",
                "Very low urgency",
                "Low urgency",
                "Slightly urgent",
                "Moderately urgent",
                "Somewhat urgent",
                "Urgent",
                "Very urgent",
                "Extremely urgent",
                "Immediate action required",
            ],
        )
        questions[_question_key(index, "requires_action")] = Noul(
            instructions={**reference, "question": "Does this email require the recipient to take action?"}
        )
        questions[_question_key(index, "has_deadline")] = Noul(
            instructions={**reference, "question": "Does this email indicate a deadline or time-sensitive requirement?"}
        )

    response = get_client().system_one(
        state={
            "task": "Classify each email independently using metadata only.",
            "emails": metadata,
        },
        questions=questions,
    )

    results: dict[str, dict[str, Any]] = {}
    for index, email in enumerate(metadata):
        category = response.answers[_question_key(index, "category")]
        priority = response.answers[_question_key(index, "priority")]
        urgency = response.answers[_question_key(index, "urgency")]
        action = response.answers[_question_key(index, "requires_action")]
        deadline = response.answers[_question_key(index, "has_deadline")]

        results[email["id"]] = {
            "category": category.choice,
            "category_details": _answer_details(category),
            "priority": priority.choice,
            "priority_details": _answer_details(priority),
            "urgency": urgency.score,
            "urgency_details": _answer_details(urgency),
            "requires_action": action.noul,
            "has_deadline": deadline.noul,
        }

    return {
        "classifications": results,
        "model": response.model,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
