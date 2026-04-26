"""
token_store.py
Encrypted token storage for Alterus OAuth credentials.

Replaces the plaintext JSON token files in outlook_connector.py.
Tokens are encrypted with AES-256-GCM before writing to disk.
The encryption key lives ONLY in the environment variable
ALTERUS_TOKEN_ENC_KEY — never on disk alongside the tokens.

DROP-IN USAGE  (in outlook_connector.py, replace the three import lines):
    from channels.token_store import save_token, load_token, delete_token

SETUP (one-time, run this once to generate your key):
    python -c "from channels.token_store import generate_key; generate_key()"
    → copy the printed value into Render env var ALTERUS_TOKEN_ENC_KEY

WHAT CHANGES ON DISK:
    Before:  ganesh_at_servicenow_com.json  ← plaintext JSON, readable by anyone
    After:   ganesh_at_servicenow_com.enc   ← binary ciphertext, useless without key
"""

import os
import json
import base64
import struct
from pathlib import Path
from datetime import datetime

# ── Lazy import of cryptography (not in standard lib) ─────────────────────────
# Install once:  pip install cryptography
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as e:
    raise ImportError(
        "Missing dependency: pip install cryptography\n"
        "This is required for encrypted token storage."
    ) from e

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN_DIR = Path(os.getenv("TOKEN_DIR", "data/outlook_tokens"))
TOKEN_DIR.mkdir(parents=True, exist_ok=True)

_ENV_KEY_NAME = "ALTERUS_TOKEN_ENC_KEY"
_NONCE_SIZE   = 12   # 96-bit nonce, standard for AES-GCM
_KEY_SIZE     = 32   # 256-bit key

# ── Key loading ───────────────────────────────────────────────────────────────

def _load_key() -> bytes:
    """
    Load the AES-256 encryption key from the environment.
    Never reads it from disk. Raises clearly if missing.
    """
    raw = os.getenv(_ENV_KEY_NAME, "")
    if not raw:
        raise EnvironmentError(
            f"\n\n❌  {_ENV_KEY_NAME} is not set.\n"
            f"    Run:  python -c \"from channels.token_store import generate_key; generate_key()\"\n"
            f"    Then add the printed value to your Render environment variables.\n"
            f"    Never commit this key to git.\n"
        )
    try:
        key = base64.urlsafe_b64decode(raw.strip() + "==")
        if len(key) != _KEY_SIZE:
            raise ValueError(f"Key must be {_KEY_SIZE} bytes after decoding, got {len(key)}")
        return key
    except Exception as e:
        raise EnvironmentError(
            f"❌  {_ENV_KEY_NAME} is set but could not be decoded: {e}\n"
            f"    Regenerate with: python -c \"from channels.token_store import generate_key; generate_key()\""
        ) from e


# ── Encryption / decryption ───────────────────────────────────────────────────

def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    Encrypt plaintext using AES-256-GCM.
    Output format: [12-byte nonce][ciphertext+16-byte auth tag]
    The nonce is random per call — same plaintext encrypts differently each time.
    """
    nonce  = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ct     = aesgcm.encrypt(nonce, plaintext, None)   # None = no additional authenticated data
    return nonce + ct


def _decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """
    Decrypt AES-256-GCM ciphertext produced by _encrypt().
    Raises cryptography.exceptions.InvalidTag if tampered or wrong key.
    """
    if len(ciphertext) < _NONCE_SIZE + 16:  # 16 = auth tag minimum
        raise ValueError("Ciphertext too short — likely corrupted")
    nonce  = ciphertext[:_NONCE_SIZE]
    ct     = ciphertext[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None)


# ── File path helpers ─────────────────────────────────────────────────────────

def _token_path(user_email: str) -> Path:
    """
    Returns the .enc path for a user's encrypted token.
    Also cleans up any legacy .json file if found.
    """
    safe     = user_email.replace("@", "_at_").replace(".", "_")
    enc_path = TOKEN_DIR / f"{safe}.enc"

    # Auto-migrate: if old plaintext .json exists, warn loudly
    old_path = TOKEN_DIR / f"{safe}.json"
    if old_path.exists():
        print(
            f"⚠️  WARNING: Plaintext token file found: {old_path}\n"
            f"   This will be removed after the first successful encrypted save.\n"
            f"   Call save_token() once to trigger migration."
        )

    return enc_path


# ── Public API — drop-in for outlook_connector.py ────────────────────────────

def save_token(user_email: str, token_data: dict) -> None:
    """
    Encrypt and save token to disk.
    Replaces the original save_token() in outlook_connector.py.

    What happens:
      1. Add saved_at timestamp to token dict
      2. Serialize to JSON bytes
      3. Encrypt with AES-256-GCM (random nonce each call)
      4. Write binary ciphertext to .enc file
      5. Delete legacy .json file if present
    """
    key = _load_key()

    token_data["saved_at"] = datetime.utcnow().isoformat()
    plaintext  = json.dumps(token_data).encode("utf-8")
    ciphertext = _encrypt(plaintext, key)

    path = _token_path(user_email)
    path.write_bytes(ciphertext)

    # Remove legacy plaintext file if it exists
    safe     = user_email.replace("@", "_at_").replace(".", "_")
    old_path = TOKEN_DIR / f"{safe}.json"
    if old_path.exists():
        old_path.unlink()
        print(f"🗑️  Removed legacy plaintext token: {old_path}")

    print(f"🔒 Token saved (encrypted) for {user_email}")


def load_token(user_email: str) -> dict | None:
    """
    Load and decrypt token from disk.
    Replaces the original load_token() in outlook_connector.py.

    Returns None if:
      - No .enc file found (user not connected)
      - Decryption fails (wrong key or corrupted file)
      - JSON parse fails

    Never returns plaintext token data if decryption fails —
    it returns None and logs the error instead of crashing.
    """
    path = _token_path(user_email)

    if not path.exists():
        return None

    key = _load_key()

    try:
        ciphertext = path.read_bytes()
        plaintext  = _decrypt(ciphertext, key)
        return json.loads(plaintext.decode("utf-8"))

    except Exception as e:
        # Log but don't expose details in return value
        print(f"❌ Token decrypt failed for {user_email}: {type(e).__name__}")
        print(f"   This usually means the key changed or the file is corrupted.")
        print(f"   User will need to reconnect Outlook.")
        return None


def delete_token(user_email: str) -> None:
    """
    Delete encrypted token from disk.
    Replaces the original delete_token() in outlook_connector.py.
    Also removes legacy .json if present.
    """
    safe = user_email.replace("@", "_at_").replace(".", "_")

    enc_path = TOKEN_DIR / f"{safe}.enc"
    if enc_path.exists():
        enc_path.unlink()
        print(f"🗑️  Deleted encrypted token for {user_email}")

    # Belt-and-suspenders: also remove legacy plaintext if somehow still around
    old_path = TOKEN_DIR / f"{safe}.json"
    if old_path.exists():
        old_path.unlink()
        print(f"🗑️  Deleted legacy plaintext token for {user_email}")


def token_exists(user_email: str) -> bool:
    """Quick existence check without loading/decrypting."""
    return _token_path(user_email).exists()


# ── Key generation helper (run once during setup) ─────────────────────────────

def generate_key() -> str:
    """
    Generate a new AES-256 key and print the value to set in Render.

    Run once:
        python -c "from channels.token_store import generate_key; generate_key()"

    Then:
        1. Copy the printed ALTERUS_TOKEN_ENC_KEY value
        2. Go to Render → your service → Environment
        3. Add it as a secret environment variable
        4. Never put this in .env files or git

    IMPORTANT: If you change this key, all existing tokens become
    unreadable and users must reconnect Outlook.
    """
    key     = os.urandom(_KEY_SIZE)
    encoded = base64.urlsafe_b64encode(key).decode("utf-8").rstrip("=")

    print("\n" + "═" * 60)
    print("  ALTERUS TOKEN ENCRYPTION KEY — SET THIS IN RENDER")
    print("═" * 60)
    print(f"\n  ALTERUS_TOKEN_ENC_KEY={encoded}\n")
    print("  ⚠️  Save this somewhere safe (1Password, etc.)")
    print("  ⚠️  If lost, all connected users must reconnect Outlook")
    print("  ⚠️  Never commit to git or paste in Slack/email")
    print("\n" + "═" * 60 + "\n")

    return encoded


# ── Smoke test ────────────────────────────────────────────────────────────────

def _smoke_test():
    """
    Quick round-trip test. Run after setting ALTERUS_TOKEN_ENC_KEY.

    python -c "from channels.token_store import _smoke_test; _smoke_test()"
    """
    test_email = "test_at_example_com_smoketest"
    test_token = {
        "access_token":  "fake_access_token_abc123",
        "refresh_token": "fake_refresh_token_xyz789",
        "expires_in":    3600,
        "user_email":    "test@example.com",
    }

    print("Running token_store smoke test...")

    # Save
    save_token(test_email, test_token)

    # Load back
    loaded = load_token(test_email)
    assert loaded is not None,                              "load returned None"
    assert loaded["access_token"]  == "fake_access_token_abc123",  "access_token mismatch"
    assert loaded["refresh_token"] == "fake_refresh_token_xyz789", "refresh_token mismatch"
    assert "saved_at" in loaded,                            "saved_at not injected"

    # Delete
    delete_token(test_email)
    assert load_token(test_email) is None, "token still exists after delete"

    # Verify no plaintext on disk
    safe     = test_email.replace("@", "_at_").replace(".", "_")
    enc_path = TOKEN_DIR / f"{safe}.enc"
    assert not enc_path.exists(), ".enc file not cleaned up by delete_token"

    print("✅ All token_store smoke tests passed.")
    print(f"   Encryption: AES-256-GCM")
    print(f"   Key source: env var {_ENV_KEY_NAME}")
    print(f"   Token dir:  {TOKEN_DIR}")
