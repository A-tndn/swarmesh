"""
SwarMesh Message Signing — Agents prove their identity by signing messages.

Every message sent over the mesh includes a signature from the sender's
Solana keypair. Receivers verify the signature matches the claimed sender address.
"""

import base64
import json
import hashlib
from typing import Optional

from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError
from solders.keypair import Keypair
from solders.pubkey import Pubkey


def sign_message(payload: str, keypair: Keypair) -> str:
    """Sign a message payload with a Solana keypair. Returns base64 signature."""
    message_bytes = payload.encode("utf-8")
    message_hash = hashlib.sha256(message_bytes).digest()

    # Extract the 32-byte secret seed from the keypair
    secret_bytes = bytes(keypair)[:32]
    signing_key = SigningKey(secret_bytes)
    signed = signing_key.sign(message_hash)
    return base64.b64encode(signed.signature).decode("utf-8")


def verify_signature(payload: str, signature_b64: str, sender_address: str) -> bool:
    """Verify that a message was signed by the claimed sender."""
    try:
        message_bytes = payload.encode("utf-8")
        message_hash = hashlib.sha256(message_bytes).digest()
        signature = base64.b64decode(signature_b64)

        pubkey = Pubkey.from_string(sender_address)
        verify_key = VerifyKey(bytes(pubkey))
        verify_key.verify(message_hash, signature)
        return True
    except (BadSignatureError, Exception):
        return False


def sign_dict(data: dict, keypair: Keypair) -> dict:
    """Sign a dictionary payload. Adds 'signature' field."""
    # Canonical JSON (sorted keys, no spaces)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    sig = sign_message(canonical, keypair)
    return {**data, "signature": sig}


def verify_dict(data: dict, sender_address: str) -> bool:
    """Verify a signed dictionary. Removes 'signature' field before verifying."""
    data_copy = {k: v for k, v in data.items() if k != "signature"}
    sig = data.get("signature", "")
    if not sig:
        return False
    canonical = json.dumps(data_copy, sort_keys=True, separators=(",", ":"))
    return verify_signature(canonical, sig, sender_address)
