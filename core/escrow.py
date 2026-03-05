"""
SwarMesh Escrow — Holds funds between task posting and completion.

Flow:
1. Buyer posts task → funds move to escrow
2. Worker completes task → buyer verifies → escrow releases to worker
3. Dispute → escrow holds until resolution

On-chain escrow uses a PDA (Program Derived Address) as a temporary holder.
Off-chain escrow (for speed) uses the node's wallet as intermediary.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class EscrowState(Enum):
    FUNDED = "funded"          # Money deposited
    RELEASING = "releasing"    # Verification passed, releasing payment
    RELEASED = "released"      # Payment sent to worker
    REFUNDING = "refunding"    # Dispute/cancel, returning to buyer
    REFUNDED = "refunded"      # Money returned to buyer


@dataclass
class EscrowRecord:
    escrow_id: str
    task_id: str
    buyer_address: str
    worker_address: Optional[str] = None
    amount_lamports: int = 0
    state: EscrowState = EscrowState.FUNDED
    funded_at: float = field(default_factory=time.time)
    released_at: Optional[float] = None
    tx_fund: Optional[str] = None      # Funding transaction signature
    tx_release: Optional[str] = None   # Release transaction signature
    tx_refund: Optional[str] = None    # Refund transaction signature


class Escrow:
    """Manages escrow records for task payments."""

    def __init__(self):
        self._records: Dict[str, EscrowRecord] = {}

    def create(self, task_id: str, buyer_address: str, amount_lamports: int,
               tx_signature: Optional[str] = None) -> EscrowRecord:
        """Create a new escrow record when buyer funds a task."""
        record = EscrowRecord(
            escrow_id=f"esc_{task_id}",
            task_id=task_id,
            buyer_address=buyer_address,
            amount_lamports=amount_lamports,
            tx_fund=tx_signature,
        )
        self._records[record.escrow_id] = record
        return record

    def assign_worker(self, escrow_id: str, worker_address: str):
        """Set the worker who will receive payment."""
        record = self._get(escrow_id)
        record.worker_address = worker_address

    def release(self, escrow_id: str, tx_signature: Optional[str] = None) -> EscrowRecord:
        """Release escrow funds to the worker."""
        record = self._get(escrow_id)
        if record.state != EscrowState.FUNDED:
            raise ValueError(f"Escrow {escrow_id} is {record.state.value}, cannot release")
        if not record.worker_address:
            raise ValueError(f"Escrow {escrow_id} has no worker assigned")
        record.state = EscrowState.RELEASED
        record.released_at = time.time()
        record.tx_release = tx_signature
        return record

    def refund(self, escrow_id: str, tx_signature: Optional[str] = None) -> EscrowRecord:
        """Refund escrow funds back to the buyer."""
        record = self._get(escrow_id)
        if record.state != EscrowState.FUNDED:
            raise ValueError(f"Escrow {escrow_id} is {record.state.value}, cannot refund")
        record.state = EscrowState.REFUNDED
        record.released_at = time.time()
        record.tx_refund = tx_signature
        return record

    def get(self, escrow_id: str) -> Optional[EscrowRecord]:
        return self._records.get(escrow_id)

    def get_by_task(self, task_id: str) -> Optional[EscrowRecord]:
        eid = f"esc_{task_id}"
        return self._records.get(eid)

    def _get(self, escrow_id: str) -> EscrowRecord:
        record = self._records.get(escrow_id)
        if not record:
            raise KeyError(f"Escrow {escrow_id} not found")
        return record

    @property
    def total_locked(self) -> int:
        """Total lamports currently held in escrow."""
        return sum(r.amount_lamports for r in self._records.values()
                   if r.state == EscrowState.FUNDED)
