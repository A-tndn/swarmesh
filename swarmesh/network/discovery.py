"""
SwarMesh Discovery — How agents find each other in the mesh.

Uses a simple WebSocket-based gossip protocol:
1. Agent connects to a bootstrap node
2. Announces its skills
3. Receives announcements from other agents
4. Queries for agents with specific skills

No central server required — any node can be a bootstrap node.
"""

import asyncio
import json
import logging
from typing import Callable, Dict, List, Optional, Set

import aiohttp
from aiohttp import web

from ..core.protocol import Message, MessageType, Protocol
from .registry import AgentInfo, AgentRegistry

logger = logging.getLogger("swarmesh.discovery")


class Discovery:
    def __init__(self, registry: AgentRegistry, protocol: Protocol,
                 host: str = "0.0.0.0", port: int = 7770):
        self.registry = registry
        self.protocol = protocol
        self.host = host
        self.port = port
        self._peers: Dict[str, web.WebSocketResponse] = {}  # address -> ws
        self._outbound: Dict[str, aiohttp.ClientWebSocketResponse] = {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._setup_handlers()

    def _setup_handlers(self):
        """Register protocol handlers for discovery messages."""
        self.protocol.on(MessageType.AGENT_ANNOUNCE, self._handle_announce)
        self.protocol.on(MessageType.AGENT_QUERY, self._handle_query)
        self.protocol.on(MessageType.PING, self._handle_ping)

    async def _handle_announce(self, message: Message):
        """Handle agent announcement — register them."""
        payload = message.payload
        agent = AgentInfo(
            address=message.sender,
            skills=payload.get("skills", []),
            description=payload.get("description", ""),
            endpoint=payload.get("endpoint", ""),
        )
        self.registry.register(agent)
        logger.info(f"Agent registered: {agent.address[:8]}... skills={agent.skills}")

    async def _handle_query(self, message: Message):
        """Handle skill query — respond with matching agents."""
        skill = message.payload.get("skill", "")
        agents = self.registry.find_by_skill(skill)
        response = Message(
            msg_type=MessageType.AGENT_RESPONSE,
            sender="mesh",
            payload={
                "skill": skill,
                "agents": [a.to_dict() for a in agents],
            },
            target=message.sender,
        )
        # Send response back to querier
        ws = self._peers.get(message.sender)
        if ws and not ws.closed:
            await ws.send_str(response.to_json())

    async def _handle_ping(self, message: Message):
        self.registry.heartbeat(message.sender)
        ws = self._peers.get(message.sender)
        if ws and not ws.closed:
            pong = Message(msg_type=MessageType.PONG, sender="mesh")
            await ws.send_str(pong.to_json())

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle incoming WebSocket connections."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        peer_address = None
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    message = Message.from_json(msg.data)
                    if peer_address is None:
                        peer_address = message.sender
                        self._peers[peer_address] = ws
                        logger.info(f"Peer connected: {peer_address[:8]}...")
                    await self.protocol.handle(message)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WS error: {ws.exception()}")
        finally:
            if peer_address:
                self._peers.pop(peer_address, None)
                logger.info(f"Peer disconnected: {peer_address[:8]}...")

        return ws

    async def start(self):
        """Start the discovery node (WebSocket server)."""
        self._app = web.Application()
        self._app.router.add_get("/ws", self._ws_handler)
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_get("/agents", self._agents_handler)
        self._app.router.add_get("/tasks", self._tasks_handler)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"SwarMesh discovery node running on ws://{self.host}:{self.port}/ws")

    async def stop(self):
        """Stop the discovery node."""
        for ws in self._peers.values():
            await ws.close()
        for ws in self._outbound.values():
            await ws.close()
        if self._runner:
            await self._runner.cleanup()
        logger.info("Discovery node stopped")

    async def connect_to_peer(self, url: str, our_address: str, our_skills: List[str]):
        """Connect to another mesh node as a client."""
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(f"{url}/ws")
            # Announce ourselves
            announce = self.protocol.agent_announce_message(
                sender=our_address,
                skills=our_skills,
            )
            await ws.send_str(announce.to_json())
            self._outbound[url] = ws
            logger.info(f"Connected to peer: {url}")

            # Listen for messages
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    message = Message.from_json(msg.data)
                    await self.protocol.handle(message)
        except Exception as e:
            logger.error(f"Failed to connect to {url}: {e}")
        finally:
            await session.close()
            self._outbound.pop(url, None)

    async def broadcast(self, message: Message):
        """Send a message to all connected peers."""
        raw = message.to_json()
        dead_peers = []
        for addr, ws in self._peers.items():
            try:
                if not ws.closed:
                    await ws.send_str(raw)
                else:
                    dead_peers.append(addr)
            except Exception:
                dead_peers.append(addr)
        for addr in dead_peers:
            self._peers.pop(addr, None)

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "service": "swarmesh",
            "peers": len(self._peers),
            "agents": len(self.registry.all_agents),
            "skills": self.registry.all_skills,
        })

    async def _agents_handler(self, request: web.Request) -> web.Response:
        agents = [a.to_dict() for a in self.registry.all_agents]
        return web.json_response({"agents": agents, "count": len(agents)})

    async def _tasks_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"message": "Task board — coming via SDK"})
