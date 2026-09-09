"""
Shared request-signing helpers for the kiosk refresh tool.

Both machines run this same file. The agent on the display laptop and the
client on the POS computer hold the same shared secret; every request carries
an HMAC-SHA256 signature over the method, path, timestamp, nonce and body.

Why sign rather than just use a password header: a signature that covers the
method and path means a captured "reload" request cannot be replayed as a
"point the display somewhere else" request, and the timestamp + nonce mean it
cannot be replayed at all outside a narrow window.

Standard library only -- no pip install on either machine.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# Header names carrying the authentication material.
HDR_TIMESTAMP = "X-Kiosk-Timestamp"
HDR_NONCE = "X-Kiosk-Nonce"
HDR_SIGNATURE = "X-Kiosk-Signature"

# How far a request's timestamp may drift from the agent's clock, in seconds.
# Bounds how long a captured request stays replayable, and how badly the two
# machines' clocks may disagree. Both are domain-joined or NTP-synced in
# practice, so a minute is generous.
MAX_CLOCK_SKEW = 60

# Refuse to read a request body larger than this. The only body we accept is a
# small JSON object holding a URL.
MAX_BODY_BYTES = 8192


def generate_secret() -> str:
    """A fresh shared secret, safe to paste into both config files."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    """A unique per-request value, so a replay can be recognised as one."""
    return secrets.token_urlsafe(12)


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    """
    Build the exact string both sides sign.

    The body is folded in as a hash so this stays a fixed-size string, and the
    fields are newline-joined so no field's content can impersonate another's
    (none of method, path, timestamp or nonce may contain a newline).
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash])


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of the canonical string under the shared secret."""
    message = canonical_string(method, path, timestamp, nonce, body).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_headers(secret: str, method: str, path: str, body: bytes) -> dict[str, str]:
    """The full set of auth headers for one outgoing request."""
    timestamp = str(int(time.time()))
    nonce = generate_nonce()
    return {
        HDR_TIMESTAMP: timestamp,
        HDR_NONCE: nonce,
        HDR_SIGNATURE: sign(secret, method, path, timestamp, nonce, body),
    }


class AuthError(Exception):
    """A request failed authentication. The message is safe to log."""


def verify(
    secret: str,
    method: str,
    path: str,
    headers: dict[str, str] | object,
    body: bytes,
    seen_nonces: dict[str, float],
) -> None:
    """
    Raise AuthError unless the request is authentic, fresh and not a replay.

    `headers` is anything with a .get(name) returning a string or None, which
    covers both a plain dict and http.server's message object. `seen_nonces`
    maps a nonce to the time it expires; it is pruned here as a side effect, so
    it does not grow without bound.
    """
    getter = headers.get
    timestamp = getter(HDR_TIMESTAMP)
    nonce = getter(HDR_NONCE)
    signature = getter(HDR_SIGNATURE)

    if not timestamp or not nonce or not signature:
        raise AuthError("missing authentication headers")

    try:
        sent_at = int(timestamp)
    except ValueError:
        raise AuthError("malformed timestamp") from None

    now = time.time()
    if abs(now - sent_at) > MAX_CLOCK_SKEW:
        raise AuthError("timestamp outside the accepted window (check the clocks on both machines)")

    # Prune anything whose replay window has already closed.
    for stale in [n for n, expires in seen_nonces.items() if expires <= now]:
        del seen_nonces[stale]

    if nonce in seen_nonces:
        raise AuthError("nonce already used (replayed request)")

    expected = sign(secret, method, path, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature):
        raise AuthError("signature mismatch (shared secret differs between the machines?)")

    # Only remember the nonce once the request is known-good, so an attacker
    # cannot fill the cache with garbage nonces.
    seen_nonces[nonce] = sent_at + MAX_CLOCK_SKEW
