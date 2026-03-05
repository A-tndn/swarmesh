"""
SwarMesh Node — The mesh node that agents connect to.

Run this on your VPS. Agents connect via WebSocket.
Acts as: discovery server + task board + message router + payment processor.

Usage:
    python -m swarmesh.node
    # or
    swarmesh-node  (after pip install)
"""

import asyncio
import logging
import os
import signal
import sys

from .core.protocol import Protocol, Message, MessageType
from .core.task import Task, TaskStatus
from .core.escrow import Escrow
from .core.storage import Storage
from .core.signing import verify_dict
from .network.discovery import Discovery
from .network.registry import AgentRegistry
from .network.reputation import ReputationSystem, ReputationEvent

logging.basicConfig(
    level=os.getenv("SWARMESH_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("swarmesh.node")


class MeshNode:
    def __init__(self):
        self.registry = AgentRegistry()
        self.protocol = Protocol()
        self.escrow = Escrow()
        self.reputation = ReputationSystem()
        self.storage = Storage()
        self.tasks: dict[str, Task] = {}

        host = os.getenv("SWARMESH_HOST", "0.0.0.0")
        port = int(os.getenv("SWARMESH_PORT", "7770"))
        self.discovery = Discovery(self.registry, self.protocol, host=host, port=port)

        self._setup_task_routing()
        self._restore_state()

    def _restore_state(self):
        """Load persisted tasks and agents on startup."""
        # Restore open/claimed tasks
        for status in ["open", "claimed", "submitted"]:
            for task in self.storage.get_tasks_by_status(status):
                self.tasks[task.task_id] = task
        # Restore agents
        for agent in self.storage.get_all_agents():
            self.registry.register(agent)
        stats = self.storage.stats()
        logger.info(f"Restored state: {len(self.tasks)} active tasks, {stats['agents']} agents, {stats['transactions']} tx history")

    def _setup_task_routing(self):
        """Route task-related messages through the node."""
        self.protocol.on(MessageType.TASK_POST, self._route_task_post)
        self.protocol.on(MessageType.TASK_CLAIM, self._route_task_claim)
        self.protocol.on(MessageType.TASK_SUBMIT, self._route_task_submit)
        self.protocol.on(MessageType.TASK_VERIFY, self._route_task_verify)
        self.protocol.on(MessageType.TASK_PAY, self._route_task_pay)

    async def _route_task_post(self, message: Message):
        """Store task and broadcast to potential workers."""
        task_data = message.payload.get("task", {})
        task = Task.from_dict(task_data)
        self.tasks[task.task_id] = task
        self.storage.save_task(task)
        logger.info(f"Task posted: {task.task_id} | {task.skill} | {task.bounty_sol} SOL")

        # Broadcast to all connected agents (they filter by skill)
        await self.discovery.broadcast(message)

    async def _route_task_claim(self, message: Message):
        """Worker claims a task — notify buyer."""
        task_id = message.payload.get("task_id")
        task = self.tasks.get(task_id)
        if not task:
            return
        if task.status != TaskStatus.OPEN:
            error = Message(
                msg_type=MessageType.ERROR,
                sender="mesh",
                payload={"task_id": task_id, "error": "Task already claimed"},
                target=message.sender,
            )
            ws = self.discovery._peers.get(message.sender)
            if ws and not ws.closed:
                await ws.send_str(error.to_json())
            return

        task.claim(message.sender)
        self.storage.save_task(task)
        escrow_rec = self.escrow.get_by_task(task_id)
        if escrow_rec:
            self.escrow.assign_worker(escrow_rec.escrow_id, message.sender)
        logger.info(f"Task {task_id} claimed by {message.sender[:8]}...")

        # Notify buyer
        ack = Message(
            msg_type=MessageType.TASK_CLAIM_ACK,
            sender="mesh",
            payload={"task_id": task_id, "worker": message.sender},
            target=task.buyer,
        )
        buyer_ws = self.discovery._peers.get(task.buyer)
        if buyer_ws and not buyer_ws.closed:
            await buyer_ws.send_str(ack.to_json())

    async def _route_task_submit(self, message: Message):
        """Worker submits result — forward to buyer."""
        task_id = message.payload.get("task_id")
        task = self.tasks.get(task_id)
        if not task:
            return
        output = message.payload.get("output", {})
        task.submit(output)
        self.storage.save_task(task)
        logger.info(f"Task {task_id} result submitted by {message.sender[:8]}...")

        # Forward to buyer
        buyer_ws = self.discovery._peers.get(task.buyer)
        if buyer_ws and not buyer_ws.closed:
            await buyer_ws.send_str(message.to_json())

    async def _route_task_verify(self, message: Message):
        task_id = message.payload.get("task_id")
        task = self.tasks.get(task_id)
        if not task:
            return
        approved = message.payload.get("approved", False)
        if approved:
            task.verify()
        else:
            task.dispute()
        self.storage.save_task(task)

        # Forward to worker
        worker_ws = self.discovery._peers.get(task.worker)
        if worker_ws and not worker_ws.closed:
            await worker_ws.send_str(message.to_json())

    async def _route_task_pay(self, message: Message):
        task_id = message.payload.get("task_id")
        task = self.tasks.get(task_id)
        if not task:
            return
        task.status = TaskStatus.PAID
        tx_sig = message.payload.get("tx_signature", "")
        self.storage.save_task(task)

        # Log the transaction
        if tx_sig and task.worker:
            self.storage.log_transaction(
                tx_signature=tx_sig,
                task_id=task_id,
                from_address=task.buyer,
                to_address=task.worker,
                amount_lamports=task.bounty_lamports,
                tx_type="task_payment",
            )

        escrow_rec = self.escrow.get_by_task(task_id)
        if escrow_rec:
            self.escrow.release(escrow_rec.escrow_id, tx_signature=tx_sig)

        # Update reputation
        if task.worker:
            self.registry.record_completion(task.worker, task.bounty_lamports)
            self.reputation.record(task.worker, ReputationEvent(
                timestamp=task.completed_at or 0,
                event_type="success",
                task_id=task_id,
                counterparty=task.buyer,
                amount_lamports=task.bounty_lamports,
            ))
            # Persist agent stats
            agent = self.registry.get(task.worker)
            if agent:
                self.storage.save_agent(agent)

        logger.info(f"Task {task_id} paid: {tx_sig[:16] if tx_sig else 'no-sig'}...")

        # Notify worker
        worker_ws = self.discovery._peers.get(task.worker)
        if worker_ws and not worker_ws.closed:
            await worker_ws.send_str(message.to_json())

    async def start(self):
        stats = self.storage.stats()
        logger.info("=" * 50)
        logger.info("  SwarMesh Node v0.1.0")
        logger.info(f"  Tasks: {stats['tasks']} | Agents: {stats['agents']} | Tx: {stats['transactions']}")
        logger.info(f"  Volume: {stats['total_volume_sol']:.6f} SOL")
        logger.info("=" * 50)
        await self.discovery.start()

    async def stop(self):
        # Persist all current agents before shutdown
        for agent in self.registry.all_agents:
            self.storage.save_agent(agent)
        self.storage.close()
        await self.discovery.stop()
        logger.info("SwarMesh Node stopped (state persisted)")


async def main():
    node = MeshNode()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await node.start()

    # Bootstrap connections
    bootstrap = os.getenv("SWARMESH_BOOTSTRAP_NODES", "")
    if bootstrap:
        for peer_url in bootstrap.split(","):
            peer_url = peer_url.strip()
            if peer_url:
                asyncio.create_task(
                    node.discovery.connect_to_peer(peer_url, "mesh_node", [])
                )

    await stop_event.wait()
    await node.stop()


def main_sync():
    """Sync entry point for console_scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
