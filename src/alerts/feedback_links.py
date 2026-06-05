"""
Generiert signierte Feedback-Links für die Alert-Email.

Jeder Link enthält eine HMAC-SHA256-Signatur damit niemand
fremde Issues labeln kann. Der Cloudflare Worker verifiziert
die Signatur bevor er die GitHub API aufruft.
"""

import hashlib
import hmac
import os
from typing import Optional


def _sign(payload: str, secret: str) -> str:
    """HMAC-SHA256 Signatur als Hex-String."""
    return hmac.new(
        secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def make_feedback_links(issue_number: int) -> Optional[dict]:
    """
    Erstellt signierte URLs für alle vier Feedback-Aktionen.

    Returns None wenn WEBHOOK_SECRET oder WORKER_URL nicht gesetzt.
    """
    secret = os.environ.get("WEBHOOK_SECRET")
    worker_url = os.environ.get("WORKER_URL", "").rstrip("/")

    if not secret or not worker_url:
        return None

    actions = ["confirmed", "rejected", "watchlist", "too-early"]
    links = {}
    for action in actions:
        payload = f"{issue_number}:{action}"
        sig = _sign(payload, secret)
        links[action] = f"{worker_url}/label?issue={issue_number}&action={action}&sig={sig}"

    return links
