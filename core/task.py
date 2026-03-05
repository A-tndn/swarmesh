"""
SwarMesh Task — The unit of work in the agent economy.
An agent posts a task with a bounty. Another agent claims it, does the work, delivers result.
Payment releases from escrow on verification.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TaskStatus(Enum):
    OPEN = "open"              # Posted, waiting for worker
    CLAIMED = "claimed"        # Worker accepted, in progress
    SUBMITTED = "submitted"    # Worker delivered result
    VERIFIED = "verified"      # Buyer confirmed result is good
    PAID = "paid"              # Payment released from escrow
    DISPUTED = "disputed"      # Buyer rejected result
    EXPIRED = "expired"        # No one claimed in time
    CANCELLED = "cancelled"    # Buyer cancelled before claim


@dataclass
class Task:
    # Identity
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])

    # Who
    buyer: str = ""            # Buyer agent's pubkey/address
    worker: Optional[str] = None  # Worker agent's pubkey/address

    # What
    skill: str = ""            # Required skill tag (e.g., "web-scrape", "summarize", "translate")
    description: str = ""      # Human/agent-readable task description
    input_data: Dict[str, Any] = field(default_factory=dict)   # Structured input
    output_data: Optional[Dict[str, Any]] = None               # Worker's result

    # Money
    bounty_lamports: int = 0   # Payment in lamports (1 SOL = 1B lamports)
    escrow_address: Optional[str] = None  # On-chain escrow holding the bounty

    # Status
    status: TaskStatus = TaskStatus.OPEN

    # Time
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None    # Auto-expire if unclaimed
    claimed_at: Optional[float] = None
    submitted_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Verification
    verification_method: str = "buyer_approve"  # or "auto_check", "oracle"
    max_attempts: int = 1

    @property
    def bounty_sol(self) -> float:
        return self.bounty_lamports / 1_000_000_000

    @bounty_sol.setter
    def bounty_sol(self, value: float):
        self.bounty_lamports = int(value * 1_000_000_000)

    @property
    def is_expired(self) -> bool:
        if self.expires_at and self.status == TaskStatus.OPEN:
            return time.time() > self.expires_at
        return False

    def claim(self, worker_address: str):
        if self.status != TaskStatus.OPEN:
            raise ValueError(f"Task {self.task_id} is {self.status.value}, cannot claim")
        if self.is_expired:
            self.status = TaskStatus.EXPIRED
            raise ValueError(f"Task {self.task_id} has expired")
        self.worker = worker_address
        self.status = TaskStatus.CLAIMED
        self.claimed_at = time.time()

    def submit(self, output: Dict[str, Any]):
        if self.status != TaskStatus.CLAIMED:
            raise ValueError(f"Task {self.task_id} is {self.status.value}, cannot submit")
        self.output_data = output
        self.status = TaskStatus.SUBMITTED
        self.submitted_at = time.time()

    def verify(self):
        if self.status != TaskStatus.SUBMITTED:
            raise ValueError(f"Task {self.task_id} is {self.status.value}, cannot verify")
        self.status = TaskStatus.VERIFIED
        self.completed_at = time.time()

    def dispute(self):
        if self.status != TaskStatus.SUBMITTED:
            raise ValueError(f"Task {self.task_id} is {self.status.value}, cannot dispute")
        self.status = TaskStatus.DISPUTED

    def cancel(self):
        if self.status != TaskStatus.OPEN:
            raise ValueError(f"Task {self.task_id} is {self.status.value}, cannot cancel")
        self.status = TaskStatus.CANCELLED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "buyer": self.buyer,
            "worker": self.worker,
            "skill": self.skill,
            "description": self.description,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "bounty_lamports": self.bounty_lamports,
            "bounty_sol": self.bounty_sol,
            "escrow_address": self.escrow_address,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "claimed_at": self.claimed_at,
            "submitted_at": self.submitted_at,
            "completed_at": self.completed_at,
            "verification_method": self.verification_method,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        data = data.copy()
        data["status"] = TaskStatus(data.get("status", "open"))
        data.pop("bounty_sol", None)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def __repr__(self) -> str:
        return f"Task({self.task_id} | {self.skill} | {self.bounty_sol:.4f} SOL | {self.status.value})"
