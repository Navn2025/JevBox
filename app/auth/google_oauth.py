from pathlib import Path

from google_auth_oauthlib.flow import Flow


BASE_DIR = Path(__file__).resolve().parents[2]

CLIENT_SECRET_FILE = BASE_DIR / "google_client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]

REDIRECT_URI = "http://127.0.0.1:8000/auth/google/callback"


def create_google_flow(
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:

    kwargs = {
        "autogenerate_code_verifier": code_verifier is None,
    }

    if state:
        kwargs["state"] = state

    if code_verifier:
        kwargs["code_verifier"] = code_verifier

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE),
        scopes=SCOPES,
        **kwargs,
    )

    flow.redirect_uri = REDIRECT_URI

    return flow