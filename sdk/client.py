"""
SwarMesh Client SDK — For agents that POST tasks and PAY for work.

Usage:
    from swarmesh.sdk import SwarMeshClient

    client = SwarMeshClient(mesh_url="ws://mesh-node:7770")
    await client.connect()

    # Post a task with 0.01 SOL bounty
    task = await client.post_task(
        skill="web-scrape",
        description="Scrape product prices from example.com",
        input_data={"url": "https://example.com/products"},
        bounty_sol=0.01,
    )

    # Wait for result
    result = await client.wait_for_result(task.task_id, timeout=60)
    print(result)
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import aiohttp

from ..core.protocol import Message, MessageType
from ..core.task import Task, TaskStatus
from ..core.wallet import Wallet
from ..payments.pay import SolanaPayment

logger = logging.getLogger("swarmesh.client")


class SwarMeshClient:
    def __init__(self, mesh_url: str = "ws://localhost:7770",
                 wallet: Optional[Wallet] = None,
                 rpc_url: Optional[str] = None):
        self.mesh_url = mesh_url
        self.wallet = wallet or Wallet()
        self.payment = SolanaPayment(rpc_url=rpc_url)
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._tasks: Dict[str, Task] = {}
        self._result_events: Dict[str, asyncio.Event] = {}
        self._listener_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect to a SwarMesh node."""
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(f"{self.mesh_url}/ws")
        self._listener_task = asyncio.create_task(self._listen())
        logger.info(f"Connected to mesh: {self.mesh_url}")

    async def disconnect(self):
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        await self.payment.close()

    async def post_task(self, skill: str, description: str,
                        input_data: Dict[str, Any],
                        bounty_sol: float = 0.001,
                        timeout_seconds: int = 300) -> Task:
        """Post a task to the mesh with a SOL bounty."""
        import time

        task = Task(
            buyer=self.wallet.address,
            skill=skill,
            description=description,
            input_data=input_data,
            expires_at=time.time() + timeout_seconds,
        )
        task.bounty_sol = bounty_sol

        self._tasks[task.task_id] = task
        self._result_events[task.task_id] = asyncio.Event()

        # Broadcast task to mesh
        msg = Message(
            msg_type=MessageType.TASK_POST,
            sender=self.wallet.address,
            payload={"task": task.to_dict()},
        )
        await self._ws.send_str(msg.to_json())
        logger.info(f"Posted task {task.task_id}: {skill} for {bounty_sol} SOL")
        return task

    async def wait_for_result(self, task_id: str, timeout: float = 120) -> Optional[Dict]:
        """Wait for a task to be completed and return the result."""
        event = self._result_events.get(task_id)
        if not event:
            return None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"Task {task_id} timed out")
            return None
        task = self._tasks.get(task_id)
        if task and task.output_data:
            return task.output_data
        return None

    async def approve_and_pay(self, task_id: str) -> Optional[str]:
        """Approve task result and release payment to worker."""
        task = self._tasks.get(task_id)
        if not task or not task.worker:
            return None

        task.verify()

        # Transfer payment
        try:
            tx_sig = await self.payment.transfer(
                sender=self.wallet.keypair,
                recipient=task.worker,
                lamports=task.bounty_lamports,
            )
            task.status = TaskStatus.PAID
            logger.info(f"Paid {task.bounty_sol} SOL to {task.worker[:8]}... tx={tx_sig[:16]}...")

            # Notify mesh
            msg = Message(
                msg_type=MessageType.TASK_PAY,
                sender=self.wallet.address,
                payload={"task_id": task_id, "tx_signature": tx_sig},
            )
            await self._ws.send_str(msg.to_json())
            return tx_sig
        except Exception as e:
            logger.error(f"Payment failed: {e}")
            return None

    async def auto_approve_and_pay(self, task_id: str, validator: Optional[Callable] = None) -> Optional[str]:
        """Auto-approve: optionally run a validator function, then pay."""
        task = self._tasks.get(task_id)
        if not task or not task.output_data:
            return None

        if validator:
            is_valid = validator(task.input_data, task.output_data)
            if not is_valid:
                task.dispute()
                msg = Message(
                    msg_type=MessageType.TASK_DISPUTE,
                    sender=self.wallet.address,
                    payload={"task_id": task_id, "reason": "Validation failed"},
                )
                await self._ws.send_str(msg.to_json())
                return None

        return await self.approve_and_pay(task_id)

    async def _listen(self):
        """Listen for messages from the mesh."""
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str):
        message = Message.from_json(raw)

        if message.msg_type == MessageType.TASK_CLAIM_ACK:
            task_id = message.payload.get("task_id")
            worker = message.payload.get("worker")
            task = self._tasks.get(task_id)
            if task:
                task.claim(worker)
                logger.info(f"Task {task_id} claimed by {worker[:8]}...")

        elif message.msg_type == MessageType.TASK_SUBMIT:
            task_id = message.payload.get("task_id")
            output = message.payload.get("output", {})
            task = self._tasks.get(task_id)
            if task:
                task.submit(output)
                logger.info(f"Task {task_id} result received")
                event = self._result_events.get(task_id)
                if event:
                    event.set()

    def __repr__(self) -> str:
        return f"SwarMeshClient(mesh={self.mesh_url}, wallet={self.wallet})"
