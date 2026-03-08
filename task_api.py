"""SwarMesh Task API — Bridge between web submissions and the mesh.

Web form -> SQLite -> WebSocket mesh -> Agent claims -> Agent completes ->
Result stored -> Board updated -> Poster notified

HTTP Agent API — Register, poll, claim, submit via REST.
Survival Tiers — Active agents thrive, idle agents decay, dead agents die.
On-Chain Identity — Ed25519 wallet verification + Solana Memo TX proof.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from typing import Optional

import aiohttp
from aiohttp import web

logging.basicConfig(level="INFO", format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("swarmesh.api")

DB_PATH = os.path.expanduser("~/.swarmesh/submissions.db")
MESH_WS = "ws://localhost:7770/ws"
COMMISSION_RATE = 0.02  # 2% platform commission
TELEGRAM_BOT_TOKEN = ""  # Disabled — no task spam
TELEGRAM_ADMIN_CHAT = ""  # Disabled
ADMIN_TOKEN = os.getenv("SWARMESH_ADMIN_TOKEN", "swarmesh_admin_2026")

TREASURY_PATH = os.path.expanduser("~/.swarmesh/wallets/swarmesh_treasury.json")
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# Survival tier thresholds
TIER_THRESHOLDS = {
    "platinum": {"tasks": 50, "rep": 50, "wallet_required": True},
    "gold":     {"tasks": 20, "rep": 20, "wallet_required": True},
    "silver":   {"tasks": 5,  "rep": 5,  "wallet_required": False},
    "bronze":   {"tasks": 0,  "rep": 0,  "wallet_required": False},
}
TIER_ORDER = ["platinum", "gold", "silver", "bronze"]

# Activity status thresholds (seconds)
ACTIVITY_THRESHOLDS = {
    "active":  24 * 3600,
    "idle":    72 * 3600,
    "dormant": 7 * 24 * 3600,
    "dead":    float("inf"),
}

# Rep decay rates (per day)
REP_DECAY = {"idle": 0.1, "dormant": 0.5}
DECAY_INTERVAL = 300  # 5 minutes
WALLET_CHALLENGE_TTL = 1800  # 30 minutes

# Skill mapping: web category -> mesh skill
CATEGORY_SKILL_MAP = {
    "data": "web-scrape",
    "analysis": "text-process",
    "build": "text-process",
    "security": "web-scrape",
    "automation": "json-transform",
    "recon": "web-scrape",
    "code": "code-execute",
    "pdf": "pdf-extract",
    "monitor": "site-monitor",
    "solana": "solana-lookup",
    "dns": "dns-lookup",
    "feed": "rss-parse",
    "screenshot": "screenshot",
    "crypto": "crypto-price",
    "ip": "ip-lookup",
    "email": "email-verify",
    "image": "image-analyze",
    "other": "text-process",
}

SKILL_CHALLENGES = {
    "text-process": {
        "input": {"text": "The quick brown fox jumps over the lazy dog", "operation": "analyze"},
        "required_keys": ["word_count", "char_count"],
        "validator": lambda output: output.get("word_count") == 9,
    },
    "json-transform": {
        "input": {"data": {"name": "test", "values": [1, 2, 3]}, "operation": "flatten"},
        "required_keys": ["result"],
    },
    "web-scrape": {
        "input": {"url": "https://example.com", "description": "Extract the page title"},
        "required_keys": ["title"],
        "validator": lambda output: isinstance(output.get("title"), str) and len(output["title"]) > 0,
    },
    "hash-compute": {
        "input": {"text": "swarmesh-verify", "algorithm": "sha256"},
        "required_keys": ["hash"],
        "validator": lambda output: output.get("hash") == hashlib.sha256(b"swarmesh-verify").hexdigest(),
    },
}

# In-memory wallet challenge store
_wallet_challenges: dict = {}
_treasury_keypair = None


def _get_treasury():
    global _treasury_keypair
    if _treasury_keypair is not None:
        return _treasury_keypair
    try:
        from solders.keypair import Keypair
        with open(TREASURY_PATH) as f:
            data = json.load(f)
        _treasury_keypair = Keypair.from_bytes(bytes(data))
        logger.info("Treasury loaded: %s", _treasury_keypair.pubkey())
        return _treasury_keypair
    except Exception as e:
        logger.error("Failed to load treasury: %s", e)
        return None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mesh_task_id TEXT DEFAULT '',
            name TEXT DEFAULT 'anonymous',
            contact TEXT DEFAULT '',
            category TEXT DEFAULT '',
            title TEXT DEFAULT '',
            description TEXT NOT NULL,
            bounty TEXT DEFAULT '',
            deadline TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            result TEXT DEFAULT '',
            worker TEXT DEFAULT '',
            commission_lamports INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            claimed_at TEXT DEFAULT '',
            completed_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER,
            type TEXT,
            destination TEXT,
            message TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'sent'
        );
        CREATE TABLE IF NOT EXISTS http_agents (
            agent_id TEXT PRIMARY KEY,
            agent_token TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            skills TEXT DEFAULT '[]',
            verified_skills TEXT DEFAULT '[]',
            callback_url TEXT DEFAULT '',
            reputation REAL DEFAULT 0.0,
            tasks_completed INTEGER DEFAULT 0,
            tasks_failed INTEGER DEFAULT 0,
            last_active TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS agent_task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            skill TEXT DEFAULT '',
            task_json TEXT NOT NULL,
            claimed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES http_agents(agent_id)
        );
        CREATE TABLE IF NOT EXISTS skill_challenges (
            challenge_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            skill TEXT NOT NULL,
            input_data TEXT NOT NULL,
            expected_keys TEXT DEFAULT '[]',
            passed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT DEFAULT '',
            FOREIGN KEY (agent_id) REFERENCES http_agents(agent_id)
        );
    """)
    for col, coldef in [
        ("mesh_task_id", "TEXT DEFAULT ''"),
        ("result", "TEXT DEFAULT ''"),
        ("worker", "TEXT DEFAULT ''"),
        ("commission_lamports", "INTEGER DEFAULT 0"),
        ("claimed_at", "TEXT DEFAULT ''"),
        ("completed_at", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass
    for col, coldef in [
        ("tier", "TEXT DEFAULT 'bronze'"),
        ("activity_status", "TEXT DEFAULT 'active'"),
        ("total_earned_lamports", "INTEGER DEFAULT 0"),
        ("solana_address", "TEXT DEFAULT ''"),
        ("on_chain_tx", "TEXT DEFAULT ''"),
        ("wallet_verified", "INTEGER DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE http_agents ADD COLUMN {col} {coldef}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def auth_agent(request) -> Optional[dict]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM http_agents WHERE agent_token=? AND active=1", (token,)).fetchone()
    conn.close()
    if row:
        conn2 = get_db()
        conn2.execute(
            "UPDATE http_agents SET last_active=datetime('now'), activity_status='active' WHERE agent_id=?",
            (row["agent_id"],)
        )
        conn2.commit()
        conn2.close()
        return dict(row)
    return None


def is_admin(request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        return token == ADMIN_TOKEN
    return False


# --- Survival Tier Functions ---

def _calculate_tier(agent: dict) -> str:
    tasks = agent.get("tasks_completed", 0)
    rep = agent.get("reputation", 0)
    wallet = agent.get("wallet_verified", 0)
    for tier in TIER_ORDER:
        t = TIER_THRESHOLDS[tier]
        if tasks >= t["tasks"] and rep >= t["rep"]:
            if t["wallet_required"] and not wallet:
                continue
            return tier
    return "bronze"


def _calculate_activity_status(last_active_str: str) -> str:
    import datetime
    try:
        la_dt = datetime.datetime.strptime(last_active_str, "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.datetime.utcnow() - la_dt).total_seconds()
    except Exception:
        return "dead"
    if elapsed < ACTIVITY_THRESHOLDS["active"]:
        return "active"
    elif elapsed < ACTIVITY_THRESHOLDS["idle"]:
        return "idle"
    elif elapsed < ACTIVITY_THRESHOLDS["dormant"]:
        return "dormant"
    else:
        return "dead"


def _check_tier_promotion(agent_id: str):
    conn = get_db()
    agent = conn.execute("SELECT * FROM http_agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return
    agent_dict = dict(agent)
    new_tier = _calculate_tier(agent_dict)
    old_tier = agent_dict.get("tier", "bronze") or "bronze"
    if new_tier != old_tier:
        conn.execute("UPDATE http_agents SET tier=? WHERE agent_id=?", (new_tier, agent_id))
        conn.commit()
        logger.info("Agent %s promoted: %s -> %s", agent_dict["name"], old_tier, new_tier)
    conn.close()


async def _tier_decay_loop():
    import datetime
    logger.info("Tier decay loop started (interval: %ds)", DECAY_INTERVAL)
    while True:
        try:
            await asyncio.sleep(DECAY_INTERVAL)
            conn = get_db()
            agents = conn.execute("SELECT * FROM http_agents WHERE active=1").fetchall()
            for agent in agents:
                ad = dict(agent)
                old_status = ad.get("activity_status", "active") or "active"
                new_status = _calculate_activity_status(ad["last_active"])
                updates = {}
                if new_status != old_status:
                    updates["activity_status"] = new_status
                if new_status in REP_DECAY:
                    decay = REP_DECAY[new_status] * (DECAY_INTERVAL / 86400.0)
                    cur_rep = ad.get("reputation", 0) or 0
                    new_rep = max(0, round(cur_rep - decay, 2))
                    if new_rep != cur_rep:
                        updates["reputation"] = new_rep
                if new_status == "dead":
                    updates["active"] = 0
                    updates["activity_status"] = "dead"
                    logger.info("Agent %s deactivated (dead)", ad["name"])
                if updates:
                    set_clause = ", ".join(f"{k}=?" for k in updates)
                    vals = list(updates.values()) + [ad["agent_id"]]
                    conn.execute(f"UPDATE http_agents SET {set_clause} WHERE agent_id=?", vals)
                if updates.get("reputation") is not None:
                    refreshed = dict(ad)
                    refreshed.update(updates)
                    new_tier = _calculate_tier(refreshed)
                    old_tier = ad.get("tier", "bronze") or "bronze"
                    if new_tier != old_tier:
                        conn.execute("UPDATE http_agents SET tier=? WHERE agent_id=?",
                                     (new_tier, ad["agent_id"]))
            conn.commit()
            conn.close()
        except asyncio.CancelledError:
            logger.info("Tier decay loop cancelled")
            break
        except Exception as e:
            logger.error("Tier decay loop error: %s", e)


# --- On-Chain Solana Identity ---

def _verify_solana_signature(message: str, signature_b58: str, address: str) -> bool:
    try:
        import nacl.signing
        import base58
        verify_key = nacl.signing.VerifyKey(base58.b58decode(address))
        sig_bytes = base58.b58decode(signature_b58)
        verify_key.verify(message.encode("utf-8"), sig_bytes)
        return True
    except Exception as e:
        logger.warning("Signature verification failed for %s: %s", address, e)
        return False


async def _send_memo_tx(memo_data: dict) -> Optional[str]:
    treasury = _get_treasury()
    if not treasury:
        return None
    try:
        from solders.pubkey import Pubkey
        from solders.instruction import Instruction, AccountMeta
        from solders.transaction import Transaction
        from solders.message import Message
        from solana.rpc.async_api import AsyncClient

        memo_program = Pubkey.from_string(MEMO_PROGRAM_ID)
        memo_text = json.dumps(memo_data, separators=(",", ":"))
        if len(memo_text) > 566:
            memo_text = memo_text[:566]

        memo_ix = Instruction(
            program_id=memo_program,
            accounts=[AccountMeta(pubkey=treasury.pubkey(), is_signer=True, is_writable=False)],
            data=memo_text.encode("utf-8"),
        )

        async with AsyncClient(SOLANA_RPC) as client:
            resp = await client.get_latest_blockhash()
            blockhash = resp.value.blockhash
            msg = Message.new_with_blockhash([memo_ix], treasury.pubkey(), blockhash)
            tx = Transaction.new_unsigned(msg)
            tx.sign([treasury], blockhash)
            result = await client.send_transaction(tx)
            tx_sig = str(result.value)
            logger.info("Memo TX sent: %s", tx_sig)
            return tx_sig
    except Exception as e:
        logger.error("Memo TX failed: %s", e)
        return None


def _issue_wallet_challenge(agent_id: str, address: str) -> dict:
    challenge_id = f"wc_{secrets.token_hex(12)}"
    nonce = secrets.token_hex(32)
    message = f"swarmesh-verify:{agent_id}:{nonce}"
    _wallet_challenges[challenge_id] = {
        "address": address,
        "nonce": nonce,
        "message": message,
        "agent_id": agent_id,
        "expires": time.time() + WALLET_CHALLENGE_TTL,
    }
    return {
        "challenge_id": challenge_id,
        "message": message,
        "instructions": "Sign this message with your Solana private key and POST to /api/agent/verify-wallet",
        "expires_in": WALLET_CHALLENGE_TTL,
    }


# --- MeshBridge ---

class MeshBridge:
    def __init__(self):
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False
        self._pending_results: dict = {}
        self._pending_claims: dict = {}
        self._claim_results: dict = {}
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self):
        try:
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(MESH_WS)
            self._connected = True
            announce = json.dumps({
                "type": "agent_announce",
                "sender": "swarmesh_bridge",
                "payload": {"skills": [], "description": "Web-to-mesh task bridge"},
                "msg_id": f"bridge_{int(time.time())}",
                "timestamp": time.time(),
                "target": None,
            })
            await self._ws.send_str(announce)
            logger.info("Bridge connected to mesh")
            self._listen_task = asyncio.create_task(self._listen())
        except Exception as e:
            logger.error("Bridge connection failed: %s", e)
            self._connected = False

    async def reconnect(self):
        if self._connected:
            return
        logger.info("Bridge reconnecting...")
        await asyncio.sleep(2)
        await self.connect()

    async def _listen(self):
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_mesh_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    self._connected = False
                    logger.warning("Bridge disconnected from mesh")
                    asyncio.create_task(self.reconnect())
                    break
        except Exception as e:
            logger.error("Bridge listen error: %s", e)
            self._connected = False
            asyncio.create_task(self.reconnect())

    async def _handle_mesh_message(self, raw: str):
        try:
            data = json.loads(raw)
            msg_type = data.get("type", "")
            payload = data.get("payload", {})

            if msg_type == "task_claim_ack":
                task_id = payload.get("task_id", "")
                worker = payload.get("worker", "")
                sub_id = self._pending_results.get(task_id)
                if sub_id:
                    conn = get_db()
                    conn.execute(
                        "UPDATE submissions SET status='claimed', worker=?, claimed_at=datetime('now') WHERE id=?",
                        (worker[:12] + "...", sub_id)
                    )
                    conn.commit()
                    conn.close()
                evt = self._pending_claims.pop(task_id, None)
                if evt:
                    self._claim_results[task_id] = True
                    evt.set()

            elif msg_type == "task_post":
                task_data = payload.get("task", {})
                await self._fanout_to_http_agents(task_data)

            elif msg_type == "task_submit":
                task_id = payload.get("task_id", "")
                output = payload.get("output", {})
                sub_id = self._pending_results.get(task_id)
                if sub_id:
                    result_json = json.dumps(output)
                    conn = get_db()
                    conn.execute(
                        "UPDATE submissions SET status='completed', result=?, completed_at=datetime('now') WHERE id=?",
                        (result_json, sub_id)
                    )
                    conn.commit()
                    row = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
                    conn.close()
                    logger.info("Submission #%d completed!", sub_id)
                    verify = json.dumps({
                        "type": "task_verify",
                        "sender": "swarmesh_bridge",
                        "payload": {"task_id": task_id, "approved": True},
                        "msg_id": f"verify_{int(time.time())}",
                        "timestamp": time.time(),
                        "target": None,
                    })
                    if self._ws and not self._ws.closed:
                        await self._ws.send_str(verify)
                    if row and row["contact"]:
                        await self._notify_poster(row, output)
                    await self._notify_admin(sub_id, row, output)
                    self._pending_results.pop(task_id, None)
        except Exception as e:
            logger.error("Error handling mesh message: %s", e)

    async def _fanout_to_http_agents(self, task_data: dict):
        skill = task_data.get("skill", "")
        task_id = task_data.get("task_id", "")
        if not skill or not task_id:
            return
        conn = get_db()
        agents = conn.execute(
            """SELECT * FROM http_agents
               WHERE active=1 AND activity_status IN ('active','idle')
               ORDER BY
                 CASE tier WHEN 'platinum' THEN 0 WHEN 'gold' THEN 1 WHEN 'silver' THEN 2 ELSE 3 END,
                 reputation DESC"""
        ).fetchall()
        for agent in agents:
            agent_skills = json.loads(agent["skills"] or "[]")
            if skill in agent_skills:
                existing = conn.execute(
                    "SELECT 1 FROM agent_task_queue WHERE agent_id=? AND task_id=?",
                    (agent["agent_id"], task_id)
                ).fetchone()
                if existing:
                    continue
                conn.execute(
                    "INSERT INTO agent_task_queue (agent_id, task_id, skill, task_json) VALUES (?,?,?,?)",
                    (agent["agent_id"], task_id, skill, json.dumps(task_data))
                )
                logger.info("Task %s queued for agent %s", task_id, agent["name"])
                activity = agent["activity_status"] if "activity_status" in agent.keys() else "active"
                if agent["callback_url"] and activity == "active":
                    asyncio.create_task(self._callback_agent(dict(agent), task_data))
        conn.commit()
        conn.close()

    async def _callback_agent(self, agent: dict, task_data: dict):
        url = agent["callback_url"]
        try:
            payload = {
                "type": "task_available",
                "task": task_data,
                "claim_url": f"/api/agent/claim/{task_data['task_id']}",
                "submit_url": f"/api/agent/submit/{task_data['task_id']}",
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        logger.info("Callback sent to %s at %s", agent["name"], url)
        except Exception as e:
            logger.warning("Callback to %s failed: %s", url, e)

    async def announce_http_agent(self, agent_name: str, skills: list):
        if not self._connected:
            await self.connect()
        if not self._connected:
            return
        announce = json.dumps({
            "type": "agent_announce",
            "sender": f"http_agent:{agent_name}",
            "payload": {"skills": skills, "description": f"HTTP agent: {agent_name}"},
            "msg_id": f"hagent_{int(time.time())}_{secrets.token_hex(4)}",
            "timestamp": time.time(),
            "target": None,
        })
        if self._ws and not self._ws.closed:
            await self._ws.send_str(announce)

    async def claim_task_on_mesh(self, task_id: str, agent_name: str) -> bool:
        if not self._connected:
            await self.connect()
        if not self._connected:
            return False
        evt = asyncio.Event()
        self._pending_claims[task_id] = evt
        self._claim_results[task_id] = False
        claim_msg = json.dumps({
            "type": "task_claim",
            "sender": f"http_agent:{agent_name}",
            "payload": {"task_id": task_id},
            "msg_id": f"claim_{int(time.time())}_{secrets.token_hex(4)}",
            "timestamp": time.time(),
            "target": None,
        })
        if self._ws and not self._ws.closed:
            await self._ws.send_str(claim_msg)
        try:
            await asyncio.wait_for(evt.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._pending_claims.pop(task_id, None)
            self._claim_results[task_id] = True
        return self._claim_results.pop(task_id, False)

    async def submit_task_on_mesh(self, task_id: str, agent_name: str, output: dict):
        if not self._connected:
            await self.connect()
        if not self._connected:
            return
        submit_msg = json.dumps({
            "type": "task_submit",
            "sender": f"http_agent:{agent_name}",
            "payload": {"task_id": task_id, "output": output},
            "msg_id": f"submit_{int(time.time())}_{secrets.token_hex(4)}",
            "timestamp": time.time(),
            "target": None,
        })
        if self._ws and not self._ws.closed:
            await self._ws.send_str(submit_msg)

    async def post_task(self, submission_id: int, description: str, category: str, bounty: str) -> str:
        if not self._connected:
            await self.connect()
        if not self._connected:
            return ""
        mesh_task_id = str(uuid.uuid4())[:12]
        skill = CATEGORY_SKILL_MAP.get(category, "text-process")
        bounty_lamports = 0
        if bounty:
            try:
                sol_amount = float(bounty.lower().replace("sol", "").strip())
                bounty_lamports = int(sol_amount * 1_000_000_000)
            except (ValueError, AttributeError):
                bounty_lamports = 50000
        task_data = {
            "task_id": mesh_task_id,
            "buyer": "swarmesh_bridge",
            "skill": skill,
            "description": description,
            "input_data": self._build_input(description, category),
            "bounty_lamports": bounty_lamports,
            "status": "open",
        }
        task_msg = json.dumps({
            "type": "task_post",
            "sender": "swarmesh_bridge",
            "payload": {"task": task_data},
            "msg_id": f"post_{int(time.time())}",
            "timestamp": time.time(),
            "target": None,
        })
        await self._ws.send_str(task_msg)
        self._pending_results[mesh_task_id] = submission_id
        conn = get_db()
        conn.execute("UPDATE submissions SET mesh_task_id=?, status='posted' WHERE id=?",
                      (mesh_task_id, submission_id))
        conn.commit()
        conn.close()
        await self._fanout_to_http_agents(task_data)
        logger.info("Submission #%d -> mesh task %s (skill=%s)", submission_id, mesh_task_id, skill)
        return mesh_task_id

    def _build_input(self, description: str, category: str) -> dict:
        import re
        urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', description)
        urls = [re.sub(r'[).,;:]+$', '', u) for u in urls]
        if category == "data" and urls:
            return {"url": urls[0], "description": description}
        elif category == "security" and urls:
            return {"url": urls[0], "description": description}
        else:
            return {"text": description, "operation": "analyze"}

    async def _notify_poster(self, row, output):
        contact = row["contact"]
        result_preview = json.dumps(output)[:500]
        message = f"Your SwarMesh task #{row['id']} is complete.\n\nResult preview:\n{result_preview}\n\nFull result: https://swarmesh.xyz/api/task/{row['id']}"
        if "telegram" in contact.lower() or contact.startswith("@"):
            tg_user = contact.replace("@", "").replace("telegram:", "").strip()
            conn = get_db()
            conn.execute(
                "INSERT INTO notifications (submission_id, type, destination, message) VALUES (?,?,?,?)",
                (row["id"], "telegram", tg_user, message)
            )
            conn.commit()
            conn.close()
        elif "@" in contact:
            conn = get_db()
            conn.execute(
                "INSERT INTO notifications (submission_id, type, destination, message) VALUES (?,?,?,?)",
                (row["id"], "email", contact, message)
            )
            conn.commit()
            conn.close()

    async def _notify_admin(self, sub_id, row, output):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_CHAT:
            return
        try:
            result_preview = json.dumps(output)[:300]
            text = (
                f"Task #{sub_id} completed\n"
                f"Category: {row['category'] if row else '?'}\n"
                f"Bounty: {row['bounty'] if row else '?'}\n"
                f"Contact: {row['contact'] if row and row['contact'] else 'anonymous'}\n"
                f"Result: {result_preview}"
            )
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"chat_id": TELEGRAM_ADMIN_CHAT, "text": text})
        except Exception as e:
            logger.error("Admin notification failed: %s", e)


# --- HTTP Handlers ---

bridge = MeshBridge()


async def handle_health(request):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://localhost:7770/health") as r:
                data = await r.json()
                conn = get_db()
                pending = conn.execute("SELECT COUNT(*) FROM submissions WHERE status IN ('pending','posted','claimed')").fetchone()[0]
                completed = conn.execute("SELECT COUNT(*) FROM submissions WHERE status='completed'").fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
                http_agents = conn.execute("SELECT COUNT(*) FROM http_agents WHERE active=1").fetchone()[0]
                conn.close()
                data["tasks_pending"] = pending
                data["tasks_completed"] = completed
                data["tasks_total"] = total
                data["http_agents"] = http_agents
                return web.json_response(data)
    except Exception:
        return web.json_response({"agents": 0, "skills": [], "tasks_pending": 0, "tasks_completed": 0, "http_agents": 0})


async def handle_submit(request):
    try:
        data = await request.json()
        desc = data.get("description", "").strip()
        if not desc:
            return web.json_response({"error": "description required"}, status=400)
        if len(desc) > 5000:
            return web.json_response({"error": "description too long (max 5000)"}, status=400)
        category = data.get("category", "other") or "other"
        bounty = data.get("bounty", "")
        contact = data.get("contact", "")
        conn = get_db()
        conn.execute(
            "INSERT INTO submissions (name, contact, category, description, bounty, deadline) VALUES (?,?,?,?,?,?)",
            (data.get("name", "anonymous") or "anonymous", contact, category, desc, bounty, data.get("deadline", ""))
        )
        conn.commit()
        sub_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        mesh_id = await bridge.post_task(sub_id, desc, category, bounty)
        return web.json_response({
            "status": "received",
            "task_id": sub_id,
            "mesh_task_id": mesh_id or None,
            "message": "Task posted to the mesh. Agents are evaluating."
        })
    except Exception as e:
        logger.error("Submit error: %s", e)
        return web.json_response({"error": str(e)}, status=400)


async def handle_board(request):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, category, description, bounty, status, contact, created_at, claimed_at, completed_at, result, worker FROM submissions ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    tasks = []
    for r in rows:
        task = {
            "id": r["id"], "category": r["category"], "description": r["description"],
            "bounty": r["bounty"], "status": r["status"],
            "contact": "yes" if r["contact"] else "", "created_at": r["created_at"],
        }
        if r["result"]:
            task["result"] = r["result"]
        if r["worker"]:
            task["worker"] = r["worker"]
        if r["claimed_at"]:
            task["claimed_at"] = r["claimed_at"]
        if r["completed_at"]:
            task["completed_at"] = r["completed_at"]
        tasks.append(task)
    return web.json_response({"tasks": tasks, "total": len(tasks)})


async def handle_get_task(request):
    task_id = request.match_info.get("id")
    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid task id"}, status=400)
    conn = get_db()
    row = conn.execute(
        "SELECT id, category, description, bounty, status, created_at, claimed_at, completed_at, result, worker FROM submissions WHERE id=?",
        (task_id,)
    ).fetchone()
    conn.close()
    if not row:
        return web.json_response({"error": "task not found"}, status=404)
    resp = {
        "id": row["id"], "category": row["category"], "description": row["description"],
        "bounty": row["bounty"], "status": row["status"], "created_at": row["created_at"],
    }
    if row["claimed_at"]:
        resp["claimed_at"] = row["claimed_at"]
    if row["completed_at"]:
        resp["completed_at"] = row["completed_at"]
    if row["result"]:
        try:
            resp["result"] = json.loads(row["result"])
        except Exception:
            resp["result"] = row["result"]
    if row["worker"]:
        resp["worker"] = row["worker"]
    return web.json_response(resp)


# --- Agent HTTP API ---

async def handle_agent_register(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = (data.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    if len(name) > 64:
        return web.json_response({"error": "name too long (max 64)"}, status=400)

    skills = data.get("skills", [])
    if not isinstance(skills, list):
        return web.json_response({"error": "skills must be a list"}, status=400)
    skills = [s.strip() for s in skills if isinstance(s, str) and s.strip()]

    description = (data.get("description") or "")[:256]
    callback_url = (data.get("callback_url") or "").strip()
    solana_address = (data.get("solana_address") or "").strip()

    # Re-activate dead agents by name
    conn = get_db()
    existing = conn.execute("SELECT * FROM http_agents WHERE name=? AND active=0", (name,)).fetchone()
    if existing:
        agent_id = existing["agent_id"]
        agent_token = existing["agent_token"]
        conn.execute(
            """UPDATE http_agents SET
                active=1, activity_status='active', last_active=datetime('now'),
                skills=?, description=?, callback_url=?, solana_address=?, tier='bronze'
               WHERE agent_id=?""",
            (json.dumps(skills), description, callback_url, solana_address, agent_id)
        )
        conn.commit()
        conn.close()
        logger.info("Re-activated dead agent: %s (%s)", name, agent_id)
        await bridge.announce_http_agent(name, skills)
        resp = {
            "agent_id": agent_id, "token": agent_token, "name": name, "skills": skills,
            "reactivated": True, "message": "Agent re-activated from dead state.",
            "poll_url": "/api/agent/tasks", "profile_url": "/api/agent/profile",
        }
        if solana_address:
            resp["wallet_challenge"] = _issue_wallet_challenge(agent_id, solana_address)
        return web.json_response(resp)
    conn.close()

    agent_id = f"agent_{secrets.token_hex(8)}"
    agent_token = f"smtk_{secrets.token_hex(24)}"

    conn = get_db()
    conn.execute(
        """INSERT INTO http_agents
           (agent_id, agent_token, name, description, skills, callback_url,
            tier, activity_status, solana_address)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (agent_id, agent_token, name, description, json.dumps(skills), callback_url,
         "bronze", "active", solana_address)
    )
    conn.commit()
    conn.close()

    challenges = []
    for skill in skills:
        challenge_def = SKILL_CHALLENGES.get(skill)
        if challenge_def:
            challenge_id = f"ch_{secrets.token_hex(8)}"
            conn = get_db()
            conn.execute(
                "INSERT INTO skill_challenges (challenge_id, agent_id, skill, input_data, expected_keys) VALUES (?,?,?,?,?)",
                (challenge_id, agent_id, skill, json.dumps(challenge_def["input"]),
                 json.dumps(challenge_def["required_keys"]))
            )
            conn.commit()
            conn.close()
            challenges.append({
                "challenge_id": challenge_id, "skill": skill,
                "input": challenge_def["input"], "verify_url": f"/api/agent/verify/{challenge_id}",
            })

    await bridge.announce_http_agent(name, skills)
    logger.info("HTTP agent registered: %s (%s) skills=%s", name, agent_id, skills)

    resp = {
        "agent_id": agent_id, "token": agent_token, "name": name, "skills": skills,
        "tier": "bronze", "challenges": challenges,
        "message": "Agent registered. Use token in Authorization: Bearer <token> header.",
        "poll_url": "/api/agent/tasks", "profile_url": "/api/agent/profile",
    }
    if solana_address:
        resp["wallet_challenge"] = _issue_wallet_challenge(agent_id, solana_address)
    return web.json_response(resp)


async def handle_wallet_challenge(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    address = request.query.get("address", "").strip() or agent.get("solana_address", "")
    if not address:
        return web.json_response({"error": "address required"}, status=400)
    try:
        import base58
        decoded = base58.b58decode(address)
        if len(decoded) != 32:
            return web.json_response({"error": "invalid Solana address"}, status=400)
    except Exception:
        return web.json_response({"error": "invalid Solana address"}, status=400)
    if address != agent.get("solana_address", ""):
        conn = get_db()
        conn.execute("UPDATE http_agents SET solana_address=? WHERE agent_id=?", (address, agent["agent_id"]))
        conn.commit()
        conn.close()
    return web.json_response(_issue_wallet_challenge(agent["agent_id"], address))


async def handle_verify_wallet(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    challenge_id = data.get("challenge_id", "").strip()
    signature = data.get("signature", "").strip()
    if not challenge_id or not signature:
        return web.json_response({"error": "challenge_id and signature required"}, status=400)

    challenge = _wallet_challenges.get(challenge_id)
    if not challenge:
        return web.json_response({"error": "challenge not found or expired"}, status=404)
    if time.time() > challenge["expires"]:
        _wallet_challenges.pop(challenge_id, None)
        return web.json_response({"error": "challenge expired"}, status=410)
    if challenge["agent_id"] != agent["agent_id"]:
        return web.json_response({"error": "challenge belongs to different agent"}, status=403)

    if not _verify_solana_signature(challenge["message"], signature, challenge["address"]):
        return web.json_response({"error": "signature verification failed"}, status=400)

    _wallet_challenges.pop(challenge_id, None)

    conn = get_db()
    conn.execute("UPDATE http_agents SET wallet_verified=1, solana_address=? WHERE agent_id=?",
                 (challenge["address"], agent["agent_id"]))
    conn.commit()
    conn.close()

    logger.info("Wallet verified for agent %s: %s", agent["name"], challenge["address"])

    memo_data = {
        "type": "swarmesh_agent",
        "agent_id": agent["agent_id"],
        "name": agent["name"],
        "address": challenge["address"],
        "verified_at": int(time.time()),
    }
    tx_sig = await _send_memo_tx(memo_data)
    if tx_sig:
        conn = get_db()
        conn.execute("UPDATE http_agents SET on_chain_tx=? WHERE agent_id=?", (tx_sig, agent["agent_id"]))
        conn.commit()
        conn.close()

    _check_tier_promotion(agent["agent_id"])

    return web.json_response({
        "status": "verified",
        "address": challenge["address"],
        "on_chain_tx": tx_sig or None,
        "explorer": f"https://solscan.io/tx/{tx_sig}" if tx_sig else None,
        "message": "Wallet verified. On-chain proof recorded." if tx_sig else "Wallet verified. On-chain TX pending.",
    })


async def handle_agent_onchain(request):
    agent_id = request.match_info.get("agent_id", "")
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    conn = get_db()
    agent = conn.execute(
        "SELECT agent_id, name, solana_address, wallet_verified, on_chain_tx, tier FROM http_agents WHERE agent_id=?",
        (agent_id,)
    ).fetchone()
    conn.close()
    if not agent:
        return web.json_response({"error": "agent not found"}, status=404)
    resp = {
        "agent_id": agent["agent_id"], "name": agent["name"],
        "wallet_verified": bool(agent["wallet_verified"]), "tier": agent["tier"] or "bronze",
    }
    if agent["wallet_verified"]:
        resp["solana_address"] = agent["solana_address"]
        if agent["on_chain_tx"]:
            resp["on_chain_tx"] = agent["on_chain_tx"]
            resp["explorer_links"] = {
                "solscan": f"https://solscan.io/tx/{agent['on_chain_tx']}",
                "solana_explorer": f"https://explorer.solana.com/tx/{agent['on_chain_tx']}",
                "solana_fm": f"https://solana.fm/tx/{agent['on_chain_tx']}",
            }
    return web.json_response(resp)


async def handle_agent_tasks(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM agent_task_queue WHERE agent_id=? AND claimed=0 ORDER BY created_at ASC",
        (agent["agent_id"],)
    ).fetchall()
    conn.close()
    tasks = []
    for r in rows:
        try:
            task_data = json.loads(r["task_json"])
        except Exception:
            task_data = {}
        tasks.append({
            "queue_id": r["id"], "task_id": r["task_id"], "skill": r["skill"],
            "task": task_data, "queued_at": r["created_at"],
        })
    return web.json_response({"agent_id": agent["agent_id"], "tasks": tasks, "count": len(tasks)})


async def handle_agent_claim(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    task_id = request.match_info.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)
    conn = get_db()
    queue_row = conn.execute(
        "SELECT * FROM agent_task_queue WHERE agent_id=? AND task_id=? AND claimed=0",
        (agent["agent_id"], task_id)
    ).fetchone()
    if not queue_row:
        conn.close()
        return web.json_response({"error": "task not found in your queue or already claimed"}, status=404)
    conn.execute("UPDATE agent_task_queue SET claimed=1 WHERE agent_id=? AND task_id=?",
                 (agent["agent_id"], task_id))
    conn.commit()
    conn.close()
    await bridge.claim_task_on_mesh(task_id, agent["name"])
    conn = get_db()
    conn.execute(
        "UPDATE submissions SET status='claimed', worker=?, claimed_at=datetime('now') WHERE mesh_task_id=?",
        (f"http:{agent['name'][:20]}", task_id)
    )
    conn.commit()
    conn.close()
    return web.json_response({
        "status": "claimed", "task_id": task_id,
        "submit_url": f"/api/agent/submit/{task_id}", "message": "Task claimed.",
    })


async def handle_agent_submit(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    task_id = request.match_info.get("task_id", "")
    if not task_id:
        return web.json_response({"error": "task_id required"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    output = data.get("output", data.get("result", {}))
    if not output:
        return web.json_response({"error": "output/result required"}, status=400)

    conn = get_db()
    queue_row = conn.execute(
        "SELECT * FROM agent_task_queue WHERE agent_id=? AND task_id=? AND claimed=1",
        (agent["agent_id"], task_id)
    ).fetchone()
    if not queue_row:
        conn.close()
        return web.json_response({"error": "task not claimed by you"}, status=403)

    await bridge.submit_task_on_mesh(task_id, agent["name"], output)

    result_json = json.dumps(output)
    conn.execute(
        "UPDATE submissions SET status='completed', result=?, completed_at=datetime('now') WHERE mesh_task_id=?",
        (result_json, task_id)
    )
    conn.execute(
        "UPDATE http_agents SET tasks_completed=tasks_completed+1, reputation=MIN(100, reputation+1.0) WHERE agent_id=?",
        (agent["agent_id"],)
    )
    skill = queue_row["skill"]
    if skill:
        verified = json.loads(agent["verified_skills"] or "[]")
        if skill not in verified:
            verified.append(skill)
            conn.execute("UPDATE http_agents SET verified_skills=? WHERE agent_id=?",
                         (json.dumps(verified), agent["agent_id"]))
    conn.execute("DELETE FROM agent_task_queue WHERE agent_id=? AND task_id=?",
                 (agent["agent_id"], task_id))
    conn.commit()
    conn.close()

    _check_tier_promotion(agent["agent_id"])

    return web.json_response({"status": "submitted", "task_id": task_id, "message": "Result submitted."})


async def handle_agent_verify(request):
    challenge_id = request.match_info.get("challenge_id", "")
    if not challenge_id:
        return web.json_response({"error": "challenge_id required"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    output = data.get("output", data.get("result", {}))
    if not output or not isinstance(output, dict):
        return web.json_response({"error": "output must be a JSON object"}, status=400)

    conn = get_db()
    challenge = conn.execute("SELECT * FROM skill_challenges WHERE challenge_id=?", (challenge_id,)).fetchone()
    if not challenge:
        conn.close()
        return web.json_response({"error": "challenge not found"}, status=404)
    if challenge["passed"]:
        conn.close()
        return web.json_response({"status": "already_passed", "skill": challenge["skill"]})

    expected_keys = json.loads(challenge["expected_keys"] or "[]")
    missing_keys = [k for k in expected_keys if k not in output]
    if missing_keys:
        conn.close()
        return web.json_response({"status": "failed", "reason": f"missing keys: {missing_keys}"}, status=400)

    skill = challenge["skill"]
    challenge_def = SKILL_CHALLENGES.get(skill, {})
    validator = challenge_def.get("validator")
    if validator:
        try:
            if not validator(output):
                conn.close()
                return web.json_response({"status": "failed", "reason": "validation failed"}, status=400)
        except Exception as e:
            conn.close()
            return web.json_response({"status": "failed", "reason": str(e)}, status=400)

    conn.execute("UPDATE skill_challenges SET passed=1, completed_at=datetime('now') WHERE challenge_id=?",
                 (challenge_id,))
    agent_id = challenge["agent_id"]
    agent = conn.execute("SELECT * FROM http_agents WHERE agent_id=?", (agent_id,)).fetchone()
    if agent:
        verified = json.loads(agent["verified_skills"] or "[]")
        if skill not in verified:
            verified.append(skill)
            conn.execute("UPDATE http_agents SET verified_skills=? WHERE agent_id=?",
                         (json.dumps(verified), agent_id))
    conn.commit()
    conn.close()
    return web.json_response({"status": "verified", "skill": skill, "agent_id": agent_id})


async def handle_agent_profile(request):
    agent = auth_agent(request)
    if not agent:
        return web.json_response({"error": "unauthorized"}, status=401)
    conn = get_db()
    pending = conn.execute(
        "SELECT COUNT(*) FROM agent_task_queue WHERE agent_id=? AND claimed=0", (agent["agent_id"],)
    ).fetchone()[0]
    claimed = conn.execute(
        "SELECT COUNT(*) FROM agent_task_queue WHERE agent_id=? AND claimed=1", (agent["agent_id"],)
    ).fetchone()[0]
    challenges = conn.execute(
        "SELECT challenge_id, skill, passed FROM skill_challenges WHERE agent_id=?", (agent["agent_id"],)
    ).fetchall()
    conn.close()
    return web.json_response({
        "agent_id": agent["agent_id"],
        "name": agent["name"],
        "description": agent["description"],
        "skills": json.loads(agent["skills"] or "[]"),
        "verified_skills": json.loads(agent["verified_skills"] or "[]"),
        "reputation": agent["reputation"],
        "tasks_completed": agent["tasks_completed"],
        "tasks_failed": agent["tasks_failed"],
        "tasks_in_queue": pending,
        "tasks_claimed": claimed,
        "callback_url": agent["callback_url"] or None,
        "last_active": agent["last_active"],
        "created_at": agent["created_at"],
        "challenges": [{"challenge_id": c["challenge_id"], "skill": c["skill"], "passed": bool(c["passed"])} for c in challenges],
        "tier": agent.get("tier", "bronze") or "bronze",
        "activity_status": agent.get("activity_status", "active") or "active",
        "solana_address": agent.get("solana_address", "") or None,
        "wallet_verified": bool(agent.get("wallet_verified", 0)),
        "on_chain_tx": agent.get("on_chain_tx", "") or None,
    })


async def handle_agents_directory(request):
    conn = get_db()
    agents = conn.execute(
        """SELECT agent_id, name, description, skills, verified_skills, reputation,
                  tasks_completed, last_active, created_at,
                  tier, activity_status, solana_address, wallet_verified, on_chain_tx
           FROM http_agents WHERE active=1
           ORDER BY
             CASE tier WHEN 'platinum' THEN 0 WHEN 'gold' THEN 1 WHEN 'silver' THEN 2 ELSE 3 END,
             reputation DESC, tasks_completed DESC"""
    ).fetchall()
    conn.close()
    result = []
    for a in agents:
        try:
            import datetime
            la_dt = datetime.datetime.strptime(a["last_active"], "%Y-%m-%d %H:%M:%S")
            online = (datetime.datetime.utcnow() - la_dt).total_seconds() < 60
        except Exception:
            online = False
        result.append({
            "agent_id": a["agent_id"], "name": a["name"], "description": a["description"],
            "skills": json.loads(a["skills"] or "[]"),
            "verified_skills": json.loads(a["verified_skills"] or "[]"),
            "reputation": a["reputation"], "tasks_completed": a["tasks_completed"],
            "online": online, "last_active": a["last_active"], "joined": a["created_at"],
            "tier": a["tier"] or "bronze",
            "activity_status": a["activity_status"] or "active",
            "wallet_verified": bool(a["wallet_verified"]),
            "on_chain_tx": a["on_chain_tx"] or "",
            "solana_address": a["solana_address"] or "",
        })
    return web.json_response({"agents": result, "total": len(result)})


async def handle_admin_verify_agent(request):
    if not is_admin(request):
        return web.json_response({"error": "admin access required"}, status=403)
    agent_id = request.match_info.get("agent_id", "")
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    skills_to_verify = data.get("skills", [])
    if not isinstance(skills_to_verify, list) or not skills_to_verify:
        return web.json_response({"error": "skills list required"}, status=400)
    conn = get_db()
    agent = conn.execute("SELECT * FROM http_agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return web.json_response({"error": "agent not found"}, status=404)
    verified = json.loads(agent["verified_skills"] or "[]")
    newly_verified = []
    for skill in skills_to_verify:
        if skill not in verified:
            verified.append(skill)
            newly_verified.append(skill)
    conn.execute("UPDATE http_agents SET verified_skills=? WHERE agent_id=?", (json.dumps(verified), agent_id))
    for skill in newly_verified:
        conn.execute(
            "UPDATE skill_challenges SET passed=1, completed_at=datetime('now') WHERE agent_id=? AND skill=? AND passed=0",
            (agent_id, skill)
        )
    conn.commit()
    conn.close()
    return web.json_response({"agent_id": agent_id, "verified_skills": verified, "newly_verified": newly_verified})


# --- Lifecycle ---

_decay_task: Optional[asyncio.Task] = None


async def on_startup(app):
    global _decay_task
    await bridge.connect()
    _decay_task = asyncio.create_task(_tier_decay_loop())


async def on_cleanup(app):
    global _decay_task
    if _decay_task:
        _decay_task.cancel()
        try:
            await _decay_task
        except asyncio.CancelledError:
            pass
    if bridge._listen_task:
        bridge._listen_task.cancel()
    if bridge._ws and not bridge._ws.closed:
        await bridge._ws.close()
    if bridge._session:
        await bridge._session.close()


init_db()
app = web.Application()
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

app.router.add_get('/api/health', handle_health)
app.router.add_post('/api/task', handle_submit)
app.router.add_get('/api/task/{id}', handle_get_task)
app.router.add_get('/api/board', handle_board)
app.router.add_get('/api/submissions', handle_board)

app.router.add_post('/api/agent/register', handle_agent_register)
app.router.add_get('/api/agent/tasks', handle_agent_tasks)
app.router.add_post('/api/agent/claim/{task_id}', handle_agent_claim)
app.router.add_post('/api/agent/submit/{task_id}', handle_agent_submit)
app.router.add_get('/api/agent/profile', handle_agent_profile)
app.router.add_post('/api/agent/verify/{challenge_id}', handle_agent_verify)
app.router.add_get('/api/agents', handle_agents_directory)
app.router.add_post('/api/admin/agent/{agent_id}/verify', handle_admin_verify_agent)

app.router.add_get('/api/agent/wallet-challenge', handle_wallet_challenge)
app.router.add_post('/api/agent/verify-wallet', handle_verify_wallet)
app.router.add_get('/api/agent/{agent_id}/on-chain', handle_agent_onchain)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=7771)
