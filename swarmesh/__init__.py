"""
SwarMesh — Agent-to-Agent Payment Protocol & Task Marketplace

The infrastructure layer where autonomous agents find each other,
exchange work, and move money. Built on Solana.
"""

__version__ = "0.1.0"

from .core import Wallet, Task, TaskStatus, Escrow, Protocol
from .sdk import SwarMeshClient, SwarMeshServer, task_handler

__all__ = [
    "Wallet", "Task", "TaskStatus", "Escrow", "Protocol",
    "SwarMeshClient", "SwarMeshServer", "task_handler",
]
