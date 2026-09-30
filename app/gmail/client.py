from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def create_gmail_service(credentials):
    """
    Create a Gmail API service instance.

    Args:
        credentials: The OAuth 2.0 credentials.

    Returns:
        A Gmail API service instance.
    """
    try:
        service = build('gmail', 'v1', credentials=credentials)
        return service
    except HttpError as error:
        print(f'An error occurred while creating the Gmail service: {error}')
        return None

def list_messages_page(credentials, max_results=20, page_token=None):
    """Return one Gmail message-id page and its continuation token."""
    service = create_gmail_service(credentials)
    if service is None:
        raise RuntimeError("Unable to create Gmail service")

    try:
        request = service.users().messages().list(
            userId="me",
            maxResults=max_results,
            pageToken=page_token,
        )
        results = request.execute()
        return {
            "messages": results.get("messages", []),
            "next_page_token": results.get("nextPageToken"),
            "result_size_estimate": results.get("resultSizeEstimate"),
        }
    except HttpError as error:
        raise RuntimeError("Gmail pagination failed") from error


def list_messages(credentials, max_results=10):
    """Backward-compatible helper for callers that only need the first page."""
    return list_messages_page(credentials, max_results=max_results)["messages"]
    
def get_message(credentials, message_id, format="full"):
    service = create_gmail_service(credentials)

    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format=format,
        )
        .execute()
    )


def get_message_metadata(credentials, message_id):
    service = create_gmail_service(credentials)
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date"],
        )
        .execute()
    )


def get_messages_metadata(credentials, message_ids):
    """Fetch a page of metadata with Gmail's HTTP batch facility."""
    if not message_ids:
        return []

    service = create_gmail_service(credentials)
    results = {}
    errors = {}

    def callback(request_id, response, exception):
        if exception:
            errors[request_id] = exception
        else:
            results[request_id] = response

    batch = service.new_batch_http_request(callback=callback)
    for message_id in message_ids:
        request = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Bcc", "Subject", "Date"],
        )
        batch.add(request, request_id=message_id)

    batch.execute()
    if errors and not results:
        raise RuntimeError("Unable to fetch Gmail message metadata")
    return [results[message_id] for message_id in message_ids if message_id in results]


def get_attachment(credentials, message_id, attachment_id):
    service = create_gmail_service(credentials)
    return (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
