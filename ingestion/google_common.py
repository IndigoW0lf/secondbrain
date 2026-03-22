"""
Shared Google OAuth for Gmail, Calendar, and Drive.
Uses one token file so you only authenticate once with all scopes.

If you previously authenticated with Gmail-only scopes, delete
config/google_token.json and run any Google ingest to re-consent.
"""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from dotenv import load_dotenv

load_dotenv("config/secrets.env")

CREDS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "config/google_credentials.json")
TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "config/google_token.json")

# All Google ingest pipelines use this combined scope list.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_google_credentials() -> Credentials:
    """Load or refresh OAuth credentials; run browser flow if needed."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds
