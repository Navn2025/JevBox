from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = f"sqlite:///{(BASE_DIR / 'jev.db').as_posix()}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class GmailConnectionRow(Base):
    __tablename__ = "gmail_connections"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)


class EmailRow(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("connection_id", "gmail_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    gmail_message_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    classification_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    classification_json: Mapped[str | None] = mapped_column(Text)
    classification_error: Mapped[str | None] = mapped_column(Text)
    classified_at: Mapped[str | None] = mapped_column(String(64))


class PageCacheRow(Base):
    __tablename__ = "gmail_page_cache"
    __table_args__ = (UniqueConstraint("connection_id", "page_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    page_key: Mapped[str] = mapped_column(Text, nullable=False)
    email_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    next_page_token: Mapped[str | None] = mapped_column(Text)


class UsageRow(Base):
    __tablename__ = "jev_usage"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    timestamp: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(255))
    email_count: Mapped[int] = mapped_column(Integer, nullable=False)
    email_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    input_cost: Mapped[float | None] = mapped_column(Float)
    output_cost: Mapped[float | None] = mapped_column(Float)
    total_cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


def init_db() -> None:
    Base.metadata.create_all(engine)
