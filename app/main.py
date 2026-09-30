from __future__ import annotations

import os
import time
from pathlib import Path
from secrets import token_urlsafe

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.auth.google_oauth import create_google_flow
from app.classifier.jev import classify_emails
from app.gmail.client import (
    get_attachment,
    get_message,
    get_messages_metadata,
    list_messages_page,
)
from app.gmail.parser import parse_message_content, parse_message_metadata
from app.settings import (
    GMAIL_PAGE_SIZE,
    JEV_BATCH_DEBOUNCE_MS,
    JEV_BATCH_SIZE,
    JEV_INPUT_COST_PER_1M_TOKENS,
    JEV_OUTPUT_COST_PER_1M_TOKENS,
    SESSION_COOKIE_SECURE,
    SESSION_MAX_AGE_DAYS,
)
from app.store import EmailRecord, GmailConnection, UsageRecord, store, utc_now_iso


app = FastAPI(title="JEV Email Intelligence")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    session_cookie="jev_session",
    max_age=60 * 60 * 24 * SESSION_MAX_AGE_DAYS,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
)


class ClassificationBatchRequest(BaseModel):
    email_ids: list[str] = Field(min_length=1, max_length=JEV_BATCH_SIZE)
    retry_failed: bool = False


def _connection(request: Request) -> GmailConnection:
    connection = store.get_connection(request.session.get("gmail_connection_id"))
    if connection is None:
        raise HTTPException(status_code=401, detail="Connect Gmail to continue")
    return connection


def _cost(tokens: int | None, price_per_million: float | None) -> float | None:
    if tokens is None or price_per_million is None:
        return None
    return tokens / 1_000_000 * price_per_million


def _usage_summary(connection: GmailConnection) -> dict:
    usage = store.get_usage(connection)
    classified_count = store.count_classified(connection)

    input_tokens = sum(item.input_tokens or 0 for item in usage)
    output_tokens = sum(item.output_tokens or 0 for item in usage)
    total_tokens = sum(item.total_tokens or 0 for item in usage)
    successful_usage = [item for item in usage if item.success]
    total_classification_time_ms = sum(item.latency_ms for item in successful_usage)
    priced = [item for item in usage if item.total_cost is not None]
    total_cost = sum(item.total_cost or 0 for item in priced) if priced else None
    email_count = sum(item.email_count for item in usage if item.success)
    return {
        "emails_classified": classified_count,
        "jev_requests": len(usage),
        "successful_requests": sum(item.success for item in usage),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "average_tokens_per_email": total_tokens / email_count if email_count else 0,
        "average_cost_per_email": total_cost / email_count if email_count and total_cost is not None else None,
        "average_batch_size": email_count / len(usage) if usage else 0,
        "total_classification_time_ms": total_classification_time_ms,
        "average_request_latency_ms": (
            total_classification_time_ms / len(successful_usage)
            if successful_usage
            else 0
        ),
        "current_batch": usage[-1].to_client() if usage else None,
        "pricing_configured": (
            JEV_INPUT_COST_PER_1M_TOKENS is not None
            and JEV_OUTPUT_COST_PER_1M_TOKENS is not None
        ),
        "batch_size": JEV_BATCH_SIZE,
        "batch_debounce_ms": JEV_BATCH_DEBOUNCE_MS,
    }


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/auth/status")
def auth_status(request: Request):
    return {"connected": store.get_connection(request.session.get("gmail_connection_id")) is not None}


@app.get("/auth/google")
def google_login(request: Request):
    flow = create_google_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    request.session["oauth_state"] = state
    request.session["code_verifier"] = flow.code_verifier
    return RedirectResponse(authorization_url)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    state = request.session.get("oauth_state")
    code_verifier = request.session.get("code_verifier")
    if not state or not code_verifier:
        raise HTTPException(status_code=400, detail="OAuth session expired. Start the login flow again.")

    flow = create_google_flow(state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=str(request.url))

    old_connection_id = request.session.get("gmail_connection_id")
    if old_connection_id:
        store.remove_connection(old_connection_id)
    connection_id = store.create_connection(flow.credentials)

    request.session.clear()
    request.session["gmail_connection_id"] = connection_id
    return RedirectResponse("/", status_code=303)


@app.post("/auth/logout")
def logout(request: Request):
    store.remove_connection(request.session.get("gmail_connection_id"))
    request.session.clear()
    return {"connected": False}


@app.get("/emails")
def emails_page(
    request: Request,
    max_results: int = Query(default=GMAIL_PAGE_SIZE, ge=1, le=50),
    page_token: str | None = Query(default=None),
):
    connection = _connection(request)
    cache_key = page_token or "__first_page__"

    with connection.lock:
        # Re-sync page one so new mail appears after a browser refresh. Later
        # page-token results are stable and can be served directly from cache.
        cached_page = store.get_page(connection, cache_key) if page_token else None
        if cached_page:
            records = [record.to_client() for record in store.get_emails(
                connection,
                cached_page["email_ids"],
            )]
            return {
                "emails": records,
                "next_page_token": cached_page["next_page_token"],
                "has_more": bool(cached_page["next_page_token"]),
            }

    page = list_messages_page(
        connection.credentials,
        max_results=max_results,
        page_token=page_token,
    )
    store.save_credentials(connection)
    message_ids = [item["id"] for item in page["messages"]]

    with connection.lock:
        cached_ids = {
            record.metadata["id"]
            for record in store.get_emails(connection, message_ids)
        }
        uncached_ids = [message_id for message_id in message_ids if message_id not in cached_ids]

    if uncached_ids:
        messages = get_messages_metadata(connection.credentials, uncached_ids)
        parsed_messages = [parse_message_metadata(message) for message in messages]
        with connection.lock:
            for metadata in parsed_messages:
                store.upsert_email(connection, metadata)
        store.save_credentials(connection)

    next_page_token = page["next_page_token"]
    with connection.lock:
        store.save_page(connection, cache_key, message_ids, next_page_token)
        records = [record.to_client() for record in store.get_emails(connection, message_ids)]

    return {
        "emails": records,
        "next_page_token": next_page_token,
        "has_more": bool(next_page_token),
    }


@app.post("/emails/classify")
def classify_email_batch(payload: ClassificationBatchRequest, request: Request):
    connection = _connection(request)
    unique_ids = list(dict.fromkeys(payload.email_ids))[:JEV_BATCH_SIZE]

    with connection.lock:
        records: list[EmailRecord] = []
        for record in store.get_emails(connection, unique_ids):
            email_id = record.metadata["id"]
            if record.classification_status == "classified":
                continue
            if record.classification_status == "classifying":
                continue
            if record.classification_status == "failed" and not payload.retry_failed:
                continue
            updated = store.set_email_status(connection, email_id, "classifying")
            if updated:
                records.append(updated)

    if not records:
        with connection.lock:
            return {
                "emails": [record.to_client() for record in store.get_emails(connection, unique_ids)],
                "usage": None,
            }

    batch_ids = [record.metadata["id"] for record in records]
    request_id = token_urlsafe(12)
    started = time.perf_counter()

    try:
        result = classify_emails([record.metadata for record in records])
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        input_tokens = result["usage"].get("input_tokens")
        output_tokens = result["usage"].get("output_tokens")
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        input_cost = _cost(input_tokens, JEV_INPUT_COST_PER_1M_TOKENS)
        output_cost = _cost(output_tokens, JEV_OUTPUT_COST_PER_1M_TOKENS)
        total_cost = (
            input_cost + output_cost
            if input_cost is not None and output_cost is not None
            else None
        )
        usage_record = UsageRecord(
            request_id=request_id,
            timestamp=utc_now_iso(),
            model=result.get("model"),
            email_count=len(records),
            email_ids=batch_ids,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=total_cost,
            latency_ms=latency_ms,
            success=True,
        )
        with connection.lock:
            updated = [
                store.save_classification(
                    connection,
                    record.metadata["id"],
                    {
                        **result["classifications"][record.metadata["id"]],
                        "classification_latency_ms": latency_ms,
                    },
                ).to_client()
                for record in records
            ]
            store.append_usage(connection, usage_record)
        return {"emails": updated, "usage": usage_record.to_client()}
    except Exception as error:
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        safe_error = str(error)[:300] or "JEV classification failed"
        usage_record = UsageRecord(
            request_id=request_id,
            timestamp=utc_now_iso(),
            model=None,
            email_count=len(records),
            email_ids=batch_ids,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            input_cost=None,
            output_cost=None,
            total_cost=None,
            latency_ms=latency_ms,
            success=False,
            error=safe_error,
        )
        with connection.lock:
            for record in records:
                store.set_email_status(connection, record.metadata["id"], "failed", safe_error)
            store.append_usage(connection, usage_record)
        raise HTTPException(status_code=502, detail={
            "message": "Classification failed",
            "email_ids": batch_ids,
            "request_id": request_id,
        }) from error


@app.get("/emails/{message_id}")
def email_detail(message_id: str, request: Request):
    connection = _connection(request)
    with connection.lock:
        if store.get_email(connection, message_id) is None:
            raise HTTPException(status_code=404, detail="Email is not loaded in this session")

    message = get_message(connection.credentials, message_id, format="full")

    def load_attachment(attachment_id: str) -> str:
        return get_attachment(connection.credentials, message_id, attachment_id).get("data", "")

    content = parse_message_content(message, attachment_loader=load_attachment)
    store.save_credentials(connection)
    return content


@app.get("/usage")
def usage(request: Request):
    return _usage_summary(_connection(request))


@app.post("/history/reset")
def reset_history(request: Request):
    connection = _connection(request)
    with connection.lock:
        if store.count_by_status(connection, "classifying"):
            raise HTTPException(
                status_code=409,
                detail="Wait for the active JEV batch to finish before resetting history",
            )
        store.reset_history(connection)
    return {"reset": True, "connected": True}
