"""
SwarMesh Wallet — Solana keypair management for agents.
Each agent gets a wallet. Wallets can send/receive SOL and SPL tokens.
"""

import json
import os
from pathlib import Path
from typing import Optional

import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey


class Wallet:
    def __init__(self, keypair: Optional[Keypair] = None, wallet_dir: Optional[str] = None):
        self._keypair = keypair or Keypair()
        self._wallet_dir = Path(wallet_dir or os.getenv("SWARMESH_WALLET_DIR", "~/.swarmesh/wallets")).expanduser()
        self._wallet_dir.mkdir(parents=True, exist_ok=True)

    @property
    def pubkey(self) -> Pubkey:
        return self._keypair.pubkey()

    @property
    def address(self) -> str:
        return str(self.pubkey)

    @property
    def keypair(self) -> Keypair:
        return self._keypair

    def save(self, name: str) -> Path:
        """Save wallet to disk. Returns path to saved file."""
        path = self._wallet_dir / f"{name}.json"
        secret_bytes = bytes(self._keypair)
        path.write_text(json.dumps(list(secret_bytes)))
        path.chmod(0o600)
        return path

    @classmethod
    def load(cls, name: str, wallet_dir: Optional[str] = None) -> "Wallet":
        """Load wallet from disk by name."""
        d = Path(wallet_dir or os.getenv("SWARMESH_WALLET_DIR", "~/.swarmesh/wallets")).expanduser()
        path = d / f"{name}.json"
        secret_bytes = bytes(json.loads(path.read_text()))
        kp = Keypair.from_bytes(secret_bytes)
        return cls(keypair=kp, wallet_dir=wallet_dir)

    @classmethod
    def from_secret_key(cls, secret_key: str, wallet_dir: Optional[str] = None) -> "Wallet":
        """Create wallet from base58-encoded secret key."""
        decoded = base58.b58decode(secret_key)
        kp = Keypair.from_bytes(decoded)
        return cls(keypair=kp, wallet_dir=wallet_dir)

    def export_secret_key(self) -> str:
        """Export secret key as base58 string."""
        return base58.b58encode(bytes(self._keypair)).decode()

    def __repr__(self) -> str:
        return f"Wallet({self.address[:8]}...{self.address[-4:]})"
