import base64
from typing import Callable


def decode_body(data: str) -> str:
    if not data:
        return ""

    decoded = base64.urlsafe_b64decode(
        data + "=" * (-len(data) % 4)
    )

    return decoded.decode("utf-8", errors="replace")


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]

    return ""


def parse_message_metadata(message):
    payload = message.get("payload", {})

    headers = payload.get("headers", [])

    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),

        "sender": get_header(
            headers,
            "From",
        ),

        "recipient": get_header(
            headers,
            "To",
        ),

        "cc": get_header(headers, "Cc"),
        "bcc": get_header(headers, "Bcc"),

        "subject": get_header(
            headers,
            "Subject",
        ),

        "timestamp": get_header(
            headers,
            "Date",
        ),

        "labels": message.get(
            "labelIds",
            [],
        ),

        "snippet": message.get("snippet", ""),

        "attachments": extract_attachment_metadata(payload),
    }


def parse_message(message):
    """Backward-compatible metadata parser."""
    return parse_message_metadata(message)


def extract_attachment_metadata(payload):
    attachments = []

    def visit(part):
        filename = part.get("filename", "")
        body = part.get("body", {})
        if filename:
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId"),
            })
        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)
    return attachments


def _header_map(headers):
    return {header.get("name", "").lower(): header.get("value", "") for header in headers}


def parse_message_content(
    message,
    attachment_loader: Callable[[str], str] | None = None,
):
    """Parse full content after a user opens a message; never used by JEV."""
    payload = message.get("payload", {})
    plain_parts = []
    html_parts = []
    inline_content = []

    def body_data(part):
        body = part.get("body", {})
        data = body.get("data")
        attachment_id = body.get("attachmentId")
        if not data and attachment_id and attachment_loader:
            data = attachment_loader(attachment_id)
        return data

    def visit(part):
        mime_type = part.get("mimeType", "").lower()
        filename = part.get("filename", "")
        headers = _header_map(part.get("headers", []))
        data = body_data(part)

        if data and not filename and mime_type == "text/plain":
            plain_parts.append(decode_body(data))
        elif data and not filename and mime_type == "text/html":
            html_parts.append(decode_body(data))
        elif data and mime_type.startswith("image/") and headers.get("content-id"):
            content_id = headers["content-id"].strip("<>")
            normalized_data = base64.b64encode(
                base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
            ).decode("ascii")
            inline_content.append({
                "content_id": content_id,
                "mime_type": mime_type,
                "data_url": f"data:{mime_type};base64,{normalized_data}",
            })

        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)
    return {
        **parse_message_metadata(message),
        "body_text": "\n\n".join(part for part in plain_parts if part).strip(),
        "body_html": "\n".join(part for part in html_parts if part).strip(),
        "inline_content": inline_content,
    }
