from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from secrets import token_urlsafe
from threading import RLock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials
from sqlalchemy import delete, func, select

from app.database import (
    EmailRow,
    GmailConnectionRow,
    PageCacheRow,
    SessionLocal,
    UsageRow,
    init_db,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fernet() -> Fernet:
    secret = os.getenv("CREDENTIAL_ENCRYPTION_KEY") or os.environ["SESSION_SECRET"]
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(derived)


@dataclass
class EmailRecord:
    metadata: dict[str, Any]
    classification_status: str = "pending"
    classification: dict[str, Any] | None = None
    classification_error: str | None = None
    classified_at: str | None = None

    def to_client(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "classification_status": self.classification_status,
            "classification": self.classification,
            "classification_error": self.classification_error,
            "classified_at": self.classified_at,
        }


@dataclass
class UsageRecord:
    request_id: str
    timestamp: str
    model: str | None
    email_count: int
    email_ids: list[str]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    input_cost: float | None
    output_cost: float | None
    total_cost: float | None
    latency_ms: float
    success: bool
    error: str | None = None

    def to_client(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class GmailConnection:
    connection_id: str
    credentials: Credentials
    lock: RLock = field(default_factory=RLock)


class DatabaseStore:
    """SQLAlchemy repository; only active credentials and locks are cached in memory."""

    def __init__(self) -> None:
        init_db()
        self._connections: dict[str, GmailConnection] = {}
        self._lock = RLock()
        self._cipher = _fernet()

    def _encrypt_credentials(self, credentials: Credentials) -> str:
        return self._cipher.encrypt(credentials.to_json().encode("utf-8")).decode("ascii")

    def _decrypt_credentials(self, payload: str) -> Credentials:
        decoded = self._cipher.decrypt(payload.encode("ascii")).decode("utf-8")
        return Credentials.from_authorized_user_info(json.loads(decoded))

    def create_connection(self, credentials: Credentials) -> str:
        connection_id = token_urlsafe(32)
        now = utc_now_iso()
        with SessionLocal() as db:
            db.add(GmailConnectionRow(
                id=connection_id,
                encrypted_credentials=self._encrypt_credentials(credentials),
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        connection = GmailConnection(connection_id=connection_id, credentials=credentials)
        with self._lock:
            self._connections[connection_id] = connection
        return connection_id

    def get_connection(self, connection_id: str | None) -> GmailConnection | None:
        if not connection_id:
            return None
        with self._lock:
            cached = self._connections.get(connection_id)
        if cached:
            return cached
        with SessionLocal() as db:
            row = db.get(GmailConnectionRow, connection_id)
            if row is None:
                return None
            try:
                credentials = self._decrypt_credentials(row.encrypted_credentials)
            except (InvalidToken, ValueError, json.JSONDecodeError):
                return None
        connection = GmailConnection(connection_id=connection_id, credentials=credentials)
        with self._lock:
            self._connections[connection_id] = connection
        return connection

    def save_credentials(self, connection: GmailConnection) -> None:
        with SessionLocal() as db:
            row = db.get(GmailConnectionRow, connection.connection_id)
            if row:
                row.encrypted_credentials = self._encrypt_credentials(connection.credentials)
                row.updated_at = utc_now_iso()
                db.commit()

    def remove_connection(self, connection_id: str | None) -> None:
        if not connection_id:
            return
        with self._lock:
            self._connections.pop(connection_id, None)
        with SessionLocal() as db:
            db.execute(delete(EmailRow).where(EmailRow.connection_id == connection_id))
            db.execute(delete(PageCacheRow).where(PageCacheRow.connection_id == connection_id))
            db.execute(delete(UsageRow).where(UsageRow.connection_id == connection_id))
            db.execute(delete(GmailConnectionRow).where(GmailConnectionRow.id == connection_id))
            db.commit()

    @staticmethod
    def _email_from_row(row: EmailRow) -> EmailRecord:
        return EmailRecord(
            metadata=json.loads(row.metadata_json),
            classification_status=row.classification_status,
            classification=json.loads(row.classification_json) if row.classification_json else None,
            classification_error=row.classification_error,
            classified_at=row.classified_at,
        )

    def get_email(self, connection: GmailConnection, message_id: str) -> EmailRecord | None:
        with SessionLocal() as db:
            row = db.scalar(select(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.gmail_message_id == message_id,
            ))
            return self._email_from_row(row) if row else None

    def get_emails(self, connection: GmailConnection, message_ids: list[str]) -> list[EmailRecord]:
        if not message_ids:
            return []
        with SessionLocal() as db:
            rows = db.scalars(select(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.gmail_message_id.in_(message_ids),
            )).all()
        by_id = {row.gmail_message_id: self._email_from_row(row) for row in rows}
        return [by_id[message_id] for message_id in message_ids if message_id in by_id]

    def upsert_email(self, connection: GmailConnection, metadata: dict[str, Any]) -> EmailRecord:
        message_id = metadata["id"]
        with SessionLocal() as db:
            row = db.scalar(select(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.gmail_message_id == message_id,
            ))
            if row is None:
                row = EmailRow(
                    connection_id=connection.connection_id,
                    gmail_message_id=message_id,
                    metadata_json=json.dumps(metadata),
                    classification_status="pending",
                )
                db.add(row)
            else:
                row.metadata_json = json.dumps(metadata)
            db.commit()
            db.refresh(row)
            return self._email_from_row(row)

    def set_email_status(
        self,
        connection: GmailConnection,
        message_id: str,
        status: str,
        error: str | None = None,
    ) -> EmailRecord | None:
        with SessionLocal() as db:
            row = db.scalar(select(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.gmail_message_id == message_id,
            ))
            if row is None:
                return None
            row.classification_status = status
            row.classification_error = error
            db.commit()
            db.refresh(row)
            return self._email_from_row(row)

    def save_classification(
        self,
        connection: GmailConnection,
        message_id: str,
        classification: dict[str, Any],
    ) -> EmailRecord:
        with SessionLocal() as db:
            row = db.scalar(select(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.gmail_message_id == message_id,
            ))
            if row is None:
                raise KeyError(message_id)
            row.classification_json = json.dumps(classification)
            row.classification_status = "classified"
            row.classification_error = None
            row.classified_at = utc_now_iso()
            db.commit()
            db.refresh(row)
            return self._email_from_row(row)

    def get_page(self, connection: GmailConnection, page_key: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.scalar(select(PageCacheRow).where(
                PageCacheRow.connection_id == connection.connection_id,
                PageCacheRow.page_key == page_key,
            ))
            if row is None:
                return None
            return {
                "email_ids": json.loads(row.email_ids_json),
                "next_page_token": row.next_page_token,
            }

    def save_page(
        self,
        connection: GmailConnection,
        page_key: str,
        email_ids: list[str],
        next_page_token: str | None,
    ) -> None:
        with SessionLocal() as db:
            row = db.scalar(select(PageCacheRow).where(
                PageCacheRow.connection_id == connection.connection_id,
                PageCacheRow.page_key == page_key,
            ))
            if row is None:
                row = PageCacheRow(connection_id=connection.connection_id, page_key=page_key)
                db.add(row)
            row.email_ids_json = json.dumps(email_ids)
            row.next_page_token = next_page_token
            db.commit()

    def append_usage(self, connection: GmailConnection, usage: UsageRecord) -> None:
        with SessionLocal() as db:
            db.add(UsageRow(
                request_id=usage.request_id,
                connection_id=connection.connection_id,
                timestamp=usage.timestamp,
                model=usage.model,
                email_count=usage.email_count,
                email_ids_json=json.dumps(usage.email_ids),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                input_cost=usage.input_cost,
                output_cost=usage.output_cost,
                total_cost=usage.total_cost,
                latency_ms=usage.latency_ms,
                success=usage.success,
                error=usage.error,
            ))
            db.commit()

    @staticmethod
    def _usage_from_row(row: UsageRow) -> UsageRecord:
        return UsageRecord(
            request_id=row.request_id,
            timestamp=row.timestamp,
            model=row.model,
            email_count=row.email_count,
            email_ids=json.loads(row.email_ids_json),
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            input_cost=row.input_cost,
            output_cost=row.output_cost,
            total_cost=row.total_cost,
            latency_ms=row.latency_ms,
            success=row.success,
            error=row.error,
        )

    def get_usage(self, connection: GmailConnection) -> list[UsageRecord]:
        with SessionLocal() as db:
            rows = db.scalars(select(UsageRow).where(
                UsageRow.connection_id == connection.connection_id,
            ).order_by(UsageRow.timestamp)).all()
            return [self._usage_from_row(row) for row in rows]

    def count_classified(self, connection: GmailConnection) -> int:
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.classification_status == "classified",
            )) or 0

    def count_by_status(self, connection: GmailConnection, status: str) -> int:
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(EmailRow).where(
                EmailRow.connection_id == connection.connection_id,
                EmailRow.classification_status == status,
            )) or 0

    def reset_history(self, connection: GmailConnection) -> None:
        """Erase derived inbox history while preserving the encrypted Gmail connection."""
        with SessionLocal() as db:
            db.execute(delete(EmailRow).where(EmailRow.connection_id == connection.connection_id))
            db.execute(delete(PageCacheRow).where(PageCacheRow.connection_id == connection.connection_id))
            db.execute(delete(UsageRow).where(UsageRow.connection_id == connection.connection_id))
            db.commit()


store = DatabaseStore()
