"""
SwarMesh Server SDK — For agents that ACCEPT tasks and EARN SOL.

Usage:
    from swarmesh.sdk import SwarMeshServer, task_handler

    server = SwarMeshServer(mesh_url="ws://mesh-node:7770")

    @server.handle("web-scrape")
    async def scrape(input_data):
        # Do the work
        return {"prices": [...]}

    await server.connect()
    await server.listen()  # Blocks, processes tasks
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

import aiohttp

from ..core.protocol import Message, MessageType
from ..core.task import Task, TaskStatus
from ..core.wallet import Wallet

logger = logging.getLogger("swarmesh.server")

TaskHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]


class SwarMeshServer:
    def __init__(self, mesh_url: str = "ws://localhost:7770",
                 wallet: Optional[Wallet] = None,
                 skills: Optional[List[str]] = None):
        self.mesh_url = mesh_url
        self.wallet = wallet or Wallet()
        self.skills = skills or []
        self._handlers: Dict[str, TaskHandler] = {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._active_tasks: Dict[str, Task] = {}

    def handle(self, skill: str):
        """Decorator to register a task handler for a skill."""
        def decorator(func: TaskHandler):
            self._handlers[skill] = func
            if skill not in self.skills:
                self.skills.append(skill)
            return func
        return decorator

    async def connect(self):
        """Connect to mesh and announce capabilities."""
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(f"{self.mesh_url}/ws")

        # Announce our skills
        announce = Message(
            msg_type=MessageType.AGENT_ANNOUNCE,
            sender=self.wallet.address,
            payload={
                "skills": self.skills,
                "description": f"Worker agent with skills: {', '.join(self.skills)}",
            },
        )
        await self._ws.send_str(announce.to_json())
        logger.info(f"Connected and announced skills: {self.skills}")

    async def disconnect(self):
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def listen(self):
        """Listen for tasks and process them. Blocks."""
        logger.info("Listening for tasks...")
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

        if message.msg_type == MessageType.TASK_POST:
            await self._handle_task_post(message)
        elif message.msg_type == MessageType.TASK_VERIFY:
            task_id = message.payload.get("task_id")
            approved = message.payload.get("approved", False)
            if approved:
                logger.info(f"Task {task_id} verified and approved!")
            else:
                logger.warning(f"Task {task_id} rejected by buyer")
        elif message.msg_type == MessageType.TASK_PAY:
            task_id = message.payload.get("task_id")
            tx = message.payload.get("tx_signature", "")
            logger.info(f"Payment received for task {task_id}: {tx[:16]}...")

    async def _handle_task_post(self, message: Message):
        """Evaluate and potentially claim a posted task."""
        task_data = message.payload.get("task", {})
        task = Task.from_dict(task_data)

        # Check if we can handle this skill
        if task.skill not in self._handlers:
            return

        # Auto-claim the task
        logger.info(f"Claiming task {task.task_id}: {task.skill} for {task.bounty_sol} SOL")

        claim_msg = Message(
            msg_type=MessageType.TASK_CLAIM,
            sender=self.wallet.address,
            payload={"task_id": task.task_id},
        )
        await self._ws.send_str(claim_msg.to_json())

        # Execute the handler
        handler = self._handlers[task.skill]
        try:
            result = await handler(task.input_data)

            # Submit result
            submit_msg = Message(
                msg_type=MessageType.TASK_SUBMIT,
                sender=self.wallet.address,
                payload={"task_id": task.task_id, "output": result},
                target=task.buyer,
            )
            await self._ws.send_str(submit_msg.to_json())
            logger.info(f"Task {task.task_id} completed and submitted")
        except Exception as e:
            logger.error(f"Task {task.task_id} failed: {e}")
            error_msg = Message(
                msg_type=MessageType.ERROR,
                sender=self.wallet.address,
                payload={"task_id": task.task_id, "error": str(e)},
                target=task.buyer,
            )
            await self._ws.send_str(error_msg.to_json())

    def __repr__(self) -> str:
        return f"SwarMeshServer(skills={self.skills}, wallet={self.wallet})"
