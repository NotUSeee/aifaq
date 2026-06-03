"""Self-contained auth primitives for the /admin panel.

Stdlib-only crypto:
  • passwords  — hashlib.scrypt (memory-hard) with a per-user random salt
  • 2FA        — RFC-6238 TOTP (HMAC-SHA1, 6 digits, 30s) with replay guard
  • sessions   — HMAC-signed "uid.exp.sig" cookie value
  • setup link — single-use token; the provisional TOTP secret is carried in a
                 server-HMAC-signed hidden field so it survives GET → POST
                 without being persisted before the account is activated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

# scrypt cost — interactive-login appropriate, ~tens of ms.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32
TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # accept ±1 step (±30s) of clock skew
ISSUER = "YourBot Status"


# ── Passwords ───────────────────────────────────────────────────────────
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                            n=_N, r=_R, p=_P, dklen=_DKLEN)
    return digest.hex(), salt


def verify_password(password: str, salt: str | None, expected_hex: str | None) -> bool:
    if not salt or not expected_hex:
        return False
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                n=_N, r=_R, p=_P, dklen=_DKLEN).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_hex)


# ── TOTP (RFC 6238) ─────────────────────────────────────────────────────
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _b32decode(secret: str) -> bytes:
    pad = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret.upper() + pad, casefold=True)


def totp_at(secret: str, step: int) -> str:
    key = _b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, last_step: int | None = None) -> tuple[bool, int | None]:
    """Return (ok, step). `step` is the accepted time-step; persist it as the
    user's last_totp_step so a code can't be replayed within its window."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit() or not secret:
        return (False, None)
    now_step = int(time.time()) // TOTP_PERIOD
    for step in range(now_step - TOTP_WINDOW, now_step + TOTP_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue  # replay guard — never accept an already-used (or older) step
        try:
            if hmac.compare_digest(totp_at(secret, step), code):
                return (True, step)
        except (ValueError, TypeError):
            return (False, None)
    return (False, None)


def otpauth_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(ISSUER)}&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")


def qr_svg(data: str) -> str | None:
    """Inline SVG QR for `data`, or None if segno isn't installed (callers
    fall back to manual secret entry)."""
    try:
        import segno
    except ImportError:
        return None
    import io
    buf = io.BytesIO()  # segno's SVG writer emits bytes
    segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, dark="#dde1f2", light=None)
    return buf.getvalue().decode("utf-8")


# ── Signed sessions ─────────────────────────────────────────────────────
def make_session(secret: str, uid: int, ttl: int) -> str:
    payload = f"{uid}.{int(time.time()) + ttl}"
    return payload + "." + _sign(secret, payload)


def session_uid(secret: str, token: str | None) -> int | None:
    if not secret or not token or token.count(".") != 2:
        return None
    uid, exp, sig = token.split(".")
    if not uid.isdigit() or not exp.isdigit() or int(exp) < int(time.time()):
        return None
    if not hmac.compare_digest(_sign(secret, f"{uid}.{exp}"), sig):
        return None
    return int(uid)


# ── Signed setup-secret field (GET → POST, no premature persistence) ─────
def sign_secret_field(server_secret: str, totp_secret: str, token: str) -> str:
    return totp_secret + "." + _sign(server_secret, totp_secret + "|" + token)


def unsign_secret_field(server_secret: str, value: str | None, token: str) -> str | None:
    if not value or "." not in value:
        return None
    totp_secret, sig = value.rsplit(".", 1)
    if hmac.compare_digest(_sign(server_secret, totp_secret + "|" + token), sig):
        return totp_secret
    return None


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
