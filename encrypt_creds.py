#!/usr/bin/env python3
"""Encrypt Firebase credentials for safe storage in version control.

Usage:
    JARVIS_AUTH_TOKEN=<your_token> python encrypt_creds.py [path/to/creds.json]

The encrypted file is written alongside the original as <filename>.enc.
Commit the .enc file; keep the plain .json in .gitignore.
"""

import hashlib
import base64
import os
import sys
from pathlib import Path


def derive_fernet_key(token: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest())


def main():
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        print("Error: cryptography package not installed. Run: pip install cryptography")
        sys.exit(1)

    token = os.environ.get("JARVIS_AUTH_TOKEN")
    if not token:
        print("Error: JARVIS_AUTH_TOKEN environment variable is not set.")
        sys.exit(1)

    creds_path = Path(
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("FIREBASE_SERVICE_ACCOUNT",
                            "jarvis-app-43084-firebase-adminsdk-fbsvc-c836619551.json")
    )

    if not creds_path.exists():
        print(f"Error: {creds_path} not found.")
        sys.exit(1)

    key = derive_fernet_key(token)
    encrypted = Fernet(key).encrypt(creds_path.read_bytes())

    enc_path = Path(str(creds_path) + ".enc")
    enc_path.write_bytes(encrypted)

    print(f"Encrypted: {creds_path} → {enc_path}")
    print(f"  Commit  : {enc_path}")
    print(f"  Ignore  : {creds_path}  (already in .gitignore)")


if __name__ == "__main__":
    main()
