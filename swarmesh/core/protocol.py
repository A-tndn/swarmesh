"""
SwarMesh Protocol — The message protocol for agent-to-agent communication.

Messages are JSON over WebSocket. Every message has a type, sender, and payload.
The protocol handles: task broadcast, claim, submit, verify, pay, and discovery.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class MessageType(Enum):
    # Task lifecycle
    TASK_POST = "task_post"           # Buyer broadcasts a new task
    TASK_CLAIM = "task_claim"         # Worker claims a task
    TASK_CLAIM_ACK = "task_claim_ack" # Buyer confirms worker's claim
    TASK_SUBMIT = "task_submit"       # Worker submits result
    TASK_VERIFY = "task_verify"       # Buyer approves result
    TASK_DISPUTE = "task_dispute"     # Buyer rejects result
    TASK_CANCEL = "task_cancel"       # Buyer cancels task
    TASK_PAY = "task_pay"            # Payment confirmation

    # Discovery
    AGENT_ANNOUNCE = "agent_announce"  # Agent declares its capabilities
    AGENT_QUERY = "agent_query"        # Search for agents with a skill
    AGENT_RESPONSE = "agent_response"  # Response to query

    # Network
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


@dataclass
class Message:
    msg_type: MessageType
    sender: str           # Sender's pubkey/address
    payload: Dict[str, Any] = field(default_factory=dict)
    msg_id: str = ""
    timestamp: float = field(default_factory=time.time)
    target: Optional[str] = None  # Specific recipient (None = broadcast)

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = f"{self.msg_type.value}_{int(self.timestamp * 1000)}"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.msg_type.value,
            "sender": self.sender,
            "payload": self.payload,
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "target": self.target,
        })

    @classmethod
    def from_json(cls, raw: str) -> "Message":
        data = json.loads(raw)
        return cls(
            msg_type=MessageType(data["type"]),
            sender=data["sender"],
            payload=data.get("payload", {}),
            msg_id=data.get("msg_id", ""),
            timestamp=data.get("timestamp", time.time()),
            target=data.get("target"),
        )


class Protocol:
    """Handles message routing and handler registration."""

    def __init__(self):
        self._handlers: Dict[MessageType, List[Callable]] = {}

    def on(self, msg_type: MessageType, handler: Callable):
        """Register a handler for a message type."""
        if msg_type not in self._handlers:
            self._handlers[msg_type] = []
        self._handlers[msg_type].append(handler)

    async def handle(self, message: Message):
        """Route a message to registered handlers."""
        handlers = self._handlers.get(message.msg_type, [])
        for handler in handlers:
            await handler(message)

    def task_post_message(self, sender: str, task_dict: Dict) -> Message:
        return Message(
            msg_type=MessageType.TASK_POST,
            sender=sender,
            payload={"task": task_dict},
        )

    def task_claim_message(self, sender: str, task_id: str) -> Message:
        return Message(
            msg_type=MessageType.TASK_CLAIM,
            sender=sender,
            payload={"task_id": task_id},
        )

    def task_submit_message(self, sender: str, task_id: str, output: Dict) -> Message:
        return Message(
            msg_type=MessageType.TASK_SUBMIT,
            sender=sender,
            payload={"task_id": task_id, "output": output},
        )

    def task_verify_message(self, sender: str, task_id: str, approved: bool) -> Message:
        return Message(
            msg_type=MessageType.TASK_VERIFY,
            sender=sender,
            payload={"task_id": task_id, "approved": approved},
        )

    def agent_announce_message(self, sender: str, skills: List[str],
                                description: str = "") -> Message:
        return Message(
            msg_type=MessageType.AGENT_ANNOUNCE,
            sender=sender,
            payload={"skills": skills, "description": description},
        )

    def agent_query_message(self, sender: str, skill: str) -> Message:
        return Message(
            msg_type=MessageType.AGENT_QUERY,
            sender=sender,
            payload={"skill": skill},
        )
