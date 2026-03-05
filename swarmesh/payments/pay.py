"""
SwarMesh Solana Payments — Send/receive SOL between agents.

Uses Solana devnet by default. Switch to mainnet-beta for production.
Escrow is off-chain for now (node wallet holds) — on-chain escrow program comes later.
"""

import asyncio
import logging
import os
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed, Finalized
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

logger = logging.getLogger("swarmesh.payment")


class SolanaPayment:
    def __init__(self, rpc_url: Optional[str] = None):
        self.rpc_url = rpc_url or os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
        self._client: Optional[AsyncClient] = None

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(self.rpc_url)
        return self._client

    async def get_balance(self, address: str) -> int:
        """Get balance in lamports."""
        client = await self._get_client()
        pubkey = Pubkey.from_string(address)
        resp = await client.get_balance(pubkey, commitment=Confirmed)
        return resp.value

    async def get_balance_sol(self, address: str) -> float:
        """Get balance in SOL."""
        lamports = await self.get_balance(address)
        return lamports / 1_000_000_000

    async def transfer(self, sender: Keypair, recipient: str, lamports: int,
                       max_retries: int = 3) -> str:
        """Transfer SOL from sender to recipient. Returns transaction signature."""
        client = await self._get_client()
        recipient_pubkey = Pubkey.from_string(recipient)

        for attempt in range(max_retries):
            try:
                # Get fresh blockhash with finalized commitment for reliability
                blockhash_resp = await client.get_latest_blockhash(commitment=Finalized)
                recent_blockhash = blockhash_resp.value.blockhash

                # Build transfer instruction
                ix = transfer(TransferParams(
                    from_pubkey=sender.pubkey(),
                    to_pubkey=recipient_pubkey,
                    lamports=lamports,
                ))

                # Build and sign transaction
                tx = Transaction.new_signed_with_payer(
                    [ix],
                    sender.pubkey(),
                    [sender],
                    recent_blockhash,
                )

                # Send with skip_preflight to avoid simulation issues
                opts = TxOpts(skip_preflight=True, preflight_commitment=Finalized)
                resp = await client.send_transaction(tx, opts=opts)
                sig = str(resp.value)
                logger.info(f"TX sent: {sig[:16]}... (attempt {attempt + 1})")

                # Wait for confirmation
                await client.confirm_transaction(resp.value, commitment=Confirmed)
                logger.info(f"TX confirmed: {sig[:16]}...")
                return sig

            except Exception as e:
                logger.warning(f"Transfer attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
                else:
                    raise

    async def transfer_sol(self, sender: Keypair, recipient: str, amount_sol: float) -> str:
        """Transfer SOL (human-readable amount)."""
        lamports = int(amount_sol * 1_000_000_000)
        return await self.transfer(sender, recipient, lamports)

    async def airdrop(self, address: str, lamports: int = 1_000_000_000) -> str:
        """Request airdrop on devnet/testnet. Default 1 SOL."""
        client = await self._get_client()
        pubkey = Pubkey.from_string(address)
        resp = await client.request_airdrop(pubkey, lamports, commitment=Confirmed)
        sig = str(resp.value)
        await client.confirm_transaction(resp.value, commitment=Confirmed)
        return sig

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None
