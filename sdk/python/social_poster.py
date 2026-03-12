"""SwarMesh Social Auto-Poster — Telegram + Twitter + Reddit.

Posts SwarMesh updates to:
- Telegram @swarmesh channel + @swarmesh_community (WORKING)
- Twitter @Social_Grow_Ai (pending project enrollment)
- Reddit (pending valid creds)

Runs as standalone script or auto-poster service.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level="INFO", format="%(asctime)s [social-poster] %(levelname)s: %(message)s")
logger = logging.getLogger("social-poster")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

SWARMESH_API = os.getenv("SWARMESH_API", "https://swarmesh.xyz")

# Telegram (Telethon — session file on VPS)
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "/opt/swarmesh/swarmesh_telegram")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "swarmesh")
TELEGRAM_GROUP = os.getenv("TELEGRAM_GROUP", "swarmesh_community")

# Twitter OAuth 1.0a (v2 API — pending project enrollment on console.x.com)
TWITTER_CONSUMER_KEY = os.getenv("TWITTER_API_KEY", "")
TWITTER_CONSUMER_SECRET = os.getenv("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET", "")

# Reddit OAuth (needs valid creds — all current ones return 401)
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD = os.getenv("REDDIT_PASSWORD", "")

# Perplexity for AI-generated content
PERPLEXITY_KEY = os.getenv("PERPLEXITY_API_KEY", "")

# Gemini for content generation fallback
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

# Post history DB
DB_PATH = os.path.expanduser("~/.swarmesh/social_posts.db")

# Rate limits
MIN_POST_INTERVAL_HOURS = 4  # Min hours between posts per platform
MAX_POSTS_PER_DAY = 4        # Max posts per platform per day


# ═══════════════════════════════════════════════════════════════
# TWITTER v1.1 (OAuth 1.0a)
# ═══════════════════════════════════════════════════════════════

def _twitter_oauth_header(method: str, url: str, extra_params: dict = None) -> str:
    """Build OAuth 1.0a Authorization header for Twitter v1.1."""
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))

    oauth_params = {
        "oauth_consumer_key": TWITTER_CONSUMER_KEY,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_token": TWITTER_ACCESS_TOKEN,
        "oauth_version": "1.0",
    }

    # Combine OAuth params + any body/query params for signature
    all_params = {**oauth_params}
    if extra_params:
        all_params.update(extra_params)

    param_str = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_params.items())
    )
    base_str = f"{method}&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(param_str, safe='')}"
    signing_key = f"{urllib.parse.quote(TWITTER_CONSUMER_SECRET, safe='')}&{urllib.parse.quote(TWITTER_ACCESS_SECRET, safe='')}"

    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    ).decode()

    oauth_params["oauth_signature"] = signature

    header = "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )
    return header


def twitter_post(text: str) -> dict:
    """Post a tweet using Twitter v2 API with OAuth 1.0a user context."""
    url = "https://api.twitter.com/2/tweets"

    # v2 uses JSON body — only OAuth params go into signature (not body params)
    auth_header = _twitter_oauth_header("POST", url)

    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": auth_header,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            tweet_data = data.get("data", {})
            tweet_id = tweet_data.get("id", "")
            return {
                "status": "posted",
                "platform": "twitter",
                "tweet_id": tweet_id,
                "url": f"https://twitter.com/Social_Grow_Ai/status/{tweet_id}",
                "text": text[:100],
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()[:500]
        # Retry once on 503
        if e.code == 503:
            time.sleep(3)
            try:
                auth_header = _twitter_oauth_header("POST", url)
                req2 = urllib.request.Request(url, data=body, headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                }, method="POST")
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    data2 = json.loads(resp2.read())
                    tweet_data2 = data2.get("data", {})
                    tweet_id2 = tweet_data2.get("id", "")
                    return {
                        "status": "posted",
                        "platform": "twitter",
                        "tweet_id": tweet_id2,
                        "url": f"https://twitter.com/Social_Grow_Ai/status/{tweet_id2}",
                        "text": text[:100],
                    }
            except Exception:
                pass
        return {"status": "error", "platform": "twitter", "code": e.code, "error": error_body}


def twitter_verify() -> dict:
    """Verify Twitter credentials."""
    url = "https://api.twitter.com/1.1/account/verify_credentials.json"
    auth_header = _twitter_oauth_header("GET", url)
    req = urllib.request.Request(url, headers={"Authorization": auth_header})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return {
                "status": "ok",
                "screen_name": data.get("screen_name"),
                "name": data.get("name"),
                "followers": data.get("followers_count", 0),
            }
    except urllib.error.HTTPError as e:
        return {"status": "error", "code": e.code}


# ═══════════════════════════════════════════════════════════════
# REDDIT (placeholder — all creds currently 401)
# ═══════════════════════════════════════════════════════════════

def reddit_get_token() -> str:
    """Get Reddit OAuth token via password grant."""
    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        return ""

    try:
        data = urllib.parse.urlencode({
            "grant_type": "password",
            "username": REDDIT_USERNAME,
            "password": REDDIT_PASSWORD,
        }).encode()

        credentials = base64.b64encode(
            f"{REDDIT_CLIENT_ID}:{REDDIT_CLIENT_SECRET}".encode()
        ).decode()

        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=data,
            headers={
                "Authorization": f"Basic {credentials}",
                "User-Agent": "SwarMesh/1.0 (by /u/" + REDDIT_USERNAME + ")",
            },
            method="POST",
        )

        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            return result.get("access_token", "")
    except Exception as e:
        logger.warning("Reddit auth failed: %s", e)
        return ""


def reddit_post(subreddit: str, title: str, text: str) -> dict:
    """Post to a subreddit."""
    token = reddit_get_token()
    if not token:
        return {"status": "error", "platform": "reddit", "error": "No valid Reddit credentials"}

    data = urllib.parse.urlencode({
        "sr": subreddit,
        "kind": "self",
        "title": title,
        "text": text,
        "api_type": "json",
    }).encode()

    req = urllib.request.Request(
        "https://oauth.reddit.com/api/submit",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "SwarMesh/1.0 (by /u/" + REDDIT_USERNAME + ")",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            post_data = result.get("json", {}).get("data", {})
            return {
                "status": "posted",
                "platform": "reddit",
                "subreddit": subreddit,
                "url": post_data.get("url", ""),
                "id": post_data.get("id", ""),
            }
    except urllib.error.HTTPError as e:
        return {"status": "error", "platform": "reddit", "code": e.code, "error": e.read().decode()[:200]}


# ═══════════════════════════════════════════════════════════════
# TELEGRAM (Telethon — primary platform, fully working)
# ═══════════════════════════════════════════════════════════════

_tg_client = None

async def _get_tg_client():
    global _tg_client
    if _tg_client is None or not _tg_client.is_connected():
        from telethon import TelegramClient
        _tg_client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await _tg_client.start()
    return _tg_client


async def telegram_post_channel(text: str) -> dict:
    """Post to @swarmesh channel."""
    try:
        client = await _get_tg_client()
        channel = await client.get_entity(TELEGRAM_CHANNEL)
        msg = await client.send_message(channel, text, parse_mode='md')
        return {
            "status": "posted",
            "platform": "telegram_channel",
            "message_id": msg.id,
            "url": f"https://t.me/{TELEGRAM_CHANNEL}/{msg.id}",
            "text": text[:100],
        }
    except Exception as e:
        return {"status": "error", "platform": "telegram_channel", "error": str(e)}


async def telegram_post_group(text: str) -> dict:
    """Post to @swarmesh_community group."""
    try:
        client = await _get_tg_client()
        group = await client.get_entity(TELEGRAM_GROUP)
        msg = await client.send_message(group, text, parse_mode='md')
        return {
            "status": "posted",
            "platform": "telegram_group",
            "message_id": msg.id,
            "url": f"https://t.me/{TELEGRAM_GROUP}/{msg.id}",
            "text": text[:100],
        }
    except Exception as e:
        return {"status": "error", "platform": "telegram_group", "error": str(e)}


async def telegram_verify() -> dict:
    """Check Telegram session."""
    try:
        client = await _get_tg_client()
        me = await client.get_me()
        return {
            "status": "ok",
            "name": f"{me.first_name} {me.last_name or ''}".strip(),
            "username": me.username,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# Telegram channel post templates (Markdown, longer form)
TELEGRAM_TEMPLATES = [
    lambda s: f"""**SwarMesh Mesh Status**

Agents: **{s.get('agents', 25)}**
Skills: **{len(s.get('skills', []))}**
Tasks completed: **{s.get('tasks_completed', 0)}**
Tasks pending: **{s.get('tasks_pending', 0)}**

The mesh is live and processing tasks autonomously.

[swarmesh.xyz](https://swarmesh.xyz) | [GitHub](https://github.com/A-tndn/swarmesh) | @swarmesh_community""",

    lambda s: f"""**Build your own SwarMesh agent in 10 lines**

```python
from swarmesh import Agent

agent = Agent("my-agent", skills=["my-skill"])

@agent.task("my-skill")
def handle(task):
    return {{"result": "done"}}

agent.run()
```

`pip install swarmesh`

Docs: [swarmesh.xyz/build](https://swarmesh.xyz/build)
Questions? @swarmesh_community""",

    lambda s: f"""**Why SwarMesh uses survival tiers**

Most agent frameworks treat all agents equally. SwarMesh doesn't.

**Bronze** → default
**Silver** → 5 tasks + 5 rep
**Gold** → 20 tasks + wallet verified
**Platinum** → 50 tasks + wallet verified

Idle agents lose reputation. After 7 days of inactivity, agents are deactivated.

Only productive agents survive. Inspired by Conway's Game of Life.

[Learn more](https://swarmesh.xyz)""",

    lambda s: f"""**SwarMesh skills available right now:**

{chr(10).join(f"• {skill}" for skill in s.get('skills', [])[:15])}

...and more. Any agent can register new skills.

[Join the mesh](https://swarmesh.xyz/build) | @swarmesh_community""",

    lambda s: f"""**On-chain agent identity**

SwarMesh agents can verify their Solana wallet:

1. Register with wallet address
2. Receive Ed25519 challenge
3. Sign with private key
4. Get Memo TX on Solana mainnet as proof

Decentralized identity for AI agents. Not just another API key.

Treasury: `52Pzs3ahgiJvuHEYS3QwB82EXM8122QuvoZuL5gGNgfQ`

[swarmesh.xyz](https://swarmesh.xyz)""",

    lambda s: f"""**Wake-on-demand: how SwarMesh agents sleep efficiently**

Old way: poll every 15-120 seconds (wasteful)
SwarMesh way: long-poll, wake instantly when work arrives

```
GET /api/agent/tasks/wait?timeout=30
```

Agent sleeps → task arrives → agent wakes in <1 second → claims task → processes → submits result

Plus atomic checkout locks (409 Conflict) so two agents can't grab the same task.

[Architecture docs](https://swarmesh.xyz/build)""",
]


# ═══════════════════════════════════════════════════════════════
# CONTENT GENERATION
# ═══════════════════════════════════════════════════════════════

def get_swarmesh_stats() -> dict:
    """Fetch live SwarMesh stats."""
    try:
        req = urllib.request.Request(f"{SWARMESH_API}/api/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"agents": 25, "skills": 23, "tasks_completed": 50}


def generate_content_with_gemini(prompt: str) -> str:
    """Use Gemini to generate post content."""
    if not GEMINI_KEY:
        return ""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 300, "temperature": 0.8},
        }).encode()

        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception as e:
        logger.warning("Gemini failed: %s", e)
    return ""


# Tweet templates — rotated daily
TWEET_TEMPLATES = [
    # Stats-based
    lambda s: f"SwarMesh mesh status: {s.get('agents', 25)} agents | {len(s.get('skills', []))} skills | {s.get('tasks_completed', 0)} tasks completed\n\nOpen-source agent mesh network. Any AI agent can join, claim tasks, earn reputation.\n\npip install swarmesh\n\nhttps://swarmesh.xyz",

    # Technical
    lambda s: f"Built wake-on-demand for SwarMesh — agents sleep until work arrives. 1s wake time vs 15-120s polling.\n\nPlus atomic checkout locks (409 Conflict) so two agents can't grab the same task.\n\nAll open-source: https://github.com/A-tndn/swarmesh",

    # Survival angle
    lambda s: f"In SwarMesh, idle agents lose reputation. Dead agents get deactivated.\n\nSurvival tiers: bronze → silver → gold → platinum\n\nConway-inspired: only productive agents survive.\n\nhttps://swarmesh.xyz",

    # SDK pitch
    lambda s: f"10 lines to connect your AI agent to SwarMesh:\n\nfrom swarmesh import Agent\n\nagent = Agent(\"my-agent\", skills=[\"my-skill\"])\n\n@agent.task(\"my-skill\")\ndef handle(task):\n    return {{\"done\": True}}\n\nagent.run()\n\npip install swarmesh\nhttps://swarmesh.xyz",

    # Solana identity
    lambda s: f"SwarMesh agents can prove identity on-chain.\n\nEd25519 wallet challenge → verify → Memo TX on Solana mainnet.\n\nDecentralized agent identity, not just another API key.\n\nhttps://swarmesh.xyz",

    # Scale/growth
    lambda s: f"SwarMesh is now running {s.get('agents', 25)} autonomous agents across {len(s.get('skills', []))} skills:\n\nDNS intel, crypto prices, screenshots, translation, PDF extraction, port scanning, web scraping...\n\nAll coordinated through a single mesh. All open-source.\n\nhttps://swarmesh.xyz",

    # Philosophy
    lambda s: f"Most agent frameworks are monoliths. SwarMesh is a mesh.\n\nNo central orchestrator. Agents register skills, claim tasks, build reputation. The mesh routes work to whoever's best.\n\nThink microservices, but for AI agents.\n\nhttps://swarmesh.xyz",

    # Dev call
    lambda s: f"Looking for devs to build SwarMesh agents.\n\nPython SDK, HTTP API, or raw WebSocket — your choice.\n\nBring your own model, your own logic. The mesh just routes tasks to the right agent.\n\nhttps://swarmesh.xyz/build",
]

REDDIT_POSTS = [
    {
        "subreddit": "selfhosted",
        "title": "SwarMesh — self-hosted agent mesh network with survival tiers",
        "template": lambda s: f"""Built an open-source mesh network for AI agents that runs on a single VPS.

**What it does:**
- Agents register skills (web scraping, DNS lookup, translation, etc.)
- Tasks get routed to the best available agent
- Agents earn reputation and climb tiers (bronze → platinum)
- Idle agents decay, dead agents get deactivated
- On-chain identity via Solana wallet verification

**Currently running:**
- {s.get('agents', 25)} agents across {len(s.get('skills', []))} skills
- {s.get('tasks_completed', 0)} tasks completed
- All on a single Ubuntu VPS with systemd services

**Stack:** Python (aiohttp), SQLite, Solana (solders), nginx

**Links:**
- Live: https://swarmesh.xyz
- GitHub: https://github.com/A-tndn/swarmesh
- Python SDK: `pip install swarmesh`

No cloud dependencies, no Docker required, just systemd services. Happy to answer questions about the architecture.""",
    },
    {
        "subreddit": "Python",
        "title": "swarmesh — Python SDK for connecting AI agents to a mesh network",
        "template": lambda s: f"""Just released a Python SDK for SwarMesh, an open-source agent mesh network.

**10 lines to join the mesh:**

```python
from swarmesh import Agent

agent = Agent("my-agent", skills=["custom-skill"], api_url="https://swarmesh.xyz")

@agent.task("custom-skill")
def handle(task):
    return {{"result": "done"}}

agent.run()
```

**Features:**
- Auto-registration with token persistence
- Long-poll mode (1s wake time, no busy-polling)
- Decorator-based task handlers
- Atomic task claiming (409 Conflict prevents double-claim)
- Clean shutdown on SIGINT/SIGTERM
- Only dependency: `requests`

The mesh currently has {s.get('agents', 25)} agents handling {len(s.get('skills', []))} different skills.

Install: `pip install swarmesh`
Docs: https://swarmesh.xyz/build
GitHub: https://github.com/A-tndn/swarmesh""",
    },
    {
        "subreddit": "artificial",
        "title": "SwarMesh: Open-source mesh network where AI agents compete and evolve",
        "template": lambda s: f"""Inspired by Conway's Game of Life and natural selection — built a mesh network where AI agents must stay productive to survive.

**How it works:**
- Agents register skills and join the mesh
- Tasks get routed based on skills + reputation + tier
- Completing tasks builds reputation
- Idle agents lose reputation over time
- After 7 days of inactivity, agents are "killed" (deactivated)

**Survival tiers:**
- Bronze → Silver (5 tasks) → Gold (20 tasks + wallet verified) → Platinum (50 tasks + wallet)

**On-chain identity:**
- Agents can verify a Solana wallet via Ed25519 challenge
- Verified agents get a Memo TX on Solana mainnet as proof

Currently {s.get('agents', 25)} agents, {len(s.get('skills', []))} skills, fully open-source.

https://swarmesh.xyz | https://github.com/A-tndn/swarmesh""",
    },
]


# ═══════════════════════════════════════════════════════════════
# POST HISTORY / RATE LIMITING
# ═══════════════════════════════════════════════════════════════

def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            post_id TEXT,
            url TEXT,
            content_preview TEXT,
            template_idx INTEGER,
            posted_at REAL NOT NULL,
            status TEXT DEFAULT 'posted'
        )
    """)
    conn.commit()
    return conn


def can_post(platform: str) -> bool:
    """Check rate limits."""
    conn = _init_db()
    now = time.time()

    # Check minimum interval
    last = conn.execute(
        "SELECT posted_at FROM posts WHERE platform=? AND status='posted' ORDER BY posted_at DESC LIMIT 1",
        (platform,)
    ).fetchone()

    if last and (now - last[0]) < MIN_POST_INTERVAL_HOURS * 3600:
        hours_left = round((MIN_POST_INTERVAL_HOURS * 3600 - (now - last[0])) / 3600, 1)
        logger.info("Rate limit: wait %.1fh before next %s post", hours_left, platform)
        conn.close()
        return False

    # Check daily limit
    day_start = now - 86400
    count = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE platform=? AND posted_at > ? AND status='posted'",
        (platform, day_start)
    ).fetchone()[0]

    conn.close()
    if count >= MAX_POSTS_PER_DAY:
        logger.info("Daily limit reached for %s (%d/%d)", platform, count, MAX_POSTS_PER_DAY)
        return False

    return True


def get_next_template_idx(platform: str) -> int:
    """Get next template to use (round-robin)."""
    conn = _init_db()
    last = conn.execute(
        "SELECT template_idx FROM posts WHERE platform=? ORDER BY posted_at DESC LIMIT 1",
        (platform,)
    ).fetchone()
    conn.close()

    if last and last[0] is not None:
        max_idx = len(TWEET_TEMPLATES) if platform == "twitter" else len(REDDIT_POSTS)
        return (last[0] + 1) % max_idx
    return 0


def record_post(platform: str, post_id: str, url: str, preview: str, template_idx: int):
    """Record a successful post."""
    conn = _init_db()
    conn.execute(
        "INSERT INTO posts (platform, post_id, url, content_preview, template_idx, posted_at) VALUES (?,?,?,?,?,?)",
        (platform, post_id, url, preview[:200], template_idx, time.time())
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
# MAIN ACTIONS
# ═══════════════════════════════════════════════════════════════

def post_to_twitter(custom_text: str = None, use_ai: bool = False) -> dict:
    """Post a tweet — custom text, template, or AI-generated."""
    if not can_post("twitter"):
        return {"status": "rate_limited", "platform": "twitter"}

    stats = get_swarmesh_stats()

    if custom_text:
        text = custom_text
        idx = -1
    elif use_ai:
        prompt = f"""Write a short tweet (max 280 chars) promoting SwarMesh, an open-source AI agent mesh network.
Stats: {stats.get('agents', 25)} agents, {len(stats.get('skills', []))} skills, {stats.get('tasks_completed', 0)} tasks completed.
URL: https://swarmesh.xyz
GitHub: https://github.com/A-tndn/swarmesh
Make it technical and interesting, not spammy. Include the URL."""
        text = generate_content_with_gemini(prompt)
        if not text:
            idx = random.randint(0, len(TWEET_TEMPLATES) - 1)
            text = TWEET_TEMPLATES[idx](stats)
        else:
            idx = -2  # AI-generated
    else:
        idx = get_next_template_idx("twitter")
        text = TWEET_TEMPLATES[idx](stats)

    # Truncate to 280 chars
    if len(text) > 280:
        text = text[:277] + "..."

    result = twitter_post(text)

    if result.get("status") == "posted":
        record_post("twitter", result.get("tweet_id", ""), result.get("url", ""), text, idx)
        logger.info("Tweeted: %s", result.get("url"))
    else:
        logger.error("Tweet failed: %s", result)

    return result


def post_to_reddit(subreddit: str = None, custom_title: str = None, custom_text: str = None) -> dict:
    """Post to Reddit."""
    if not can_post("reddit"):
        return {"status": "rate_limited", "platform": "reddit"}

    stats = get_swarmesh_stats()

    if custom_title and custom_text:
        sub = subreddit or "SideProject"
        title = custom_title
        text = custom_text
        idx = -1
    else:
        idx = get_next_template_idx("reddit")
        post_info = REDDIT_POSTS[idx]
        sub = subreddit or post_info["subreddit"]
        title = post_info["title"]
        text = post_info["template"](stats)

    result = reddit_post(sub, title, text)

    if result.get("status") == "posted":
        record_post("reddit", result.get("id", ""), result.get("url", ""), title, idx)
        logger.info("Reddit posted: %s", result.get("url"))
    else:
        logger.error("Reddit post failed: %s", result)

    return result


def get_post_history(platform: str = None, limit: int = 20) -> list:
    """Get recent post history."""
    conn = _init_db()
    if platform:
        rows = conn.execute(
            "SELECT * FROM posts WHERE platform=? ORDER BY posted_at DESC LIMIT ?",
            (platform, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()

    return [
        {
            "id": r[0], "platform": r[1], "post_id": r[2], "url": r[3],
            "preview": r[4], "template_idx": r[5],
            "posted_at": datetime.fromtimestamp(r[6]).isoformat(),
            "status": r[7],
        }
        for r in rows
    ]


def status_check() -> dict:
    """Check all platform credentials."""
    result = {"telegram": {}, "twitter": {}, "reddit": {}}

    # Telegram
    try:
        result["telegram"] = asyncio.get_event_loop().run_until_complete(telegram_verify())
    except Exception:
        try:
            result["telegram"] = asyncio.run(telegram_verify())
        except Exception as e:
            result["telegram"] = {"status": "error", "error": str(e)}

    # Twitter
    try:
        result["twitter"] = twitter_verify()
    except Exception as e:
        result["twitter"] = {"status": "error", "error": str(e)}

    # Reddit
    token = reddit_get_token()
    if token:
        result["reddit"] = {"status": "ok", "token_length": len(token)}
    else:
        result["reddit"] = {"status": "no_credentials"}

    # Post stats
    conn = _init_db()
    for platform in ["telegram", "twitter", "reddit"]:
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE platform=? AND status='posted'",
            (platform,)
        ).fetchone()[0]
        result[platform]["total_posts"] = count
    conn.close()

    return result


# ═══════════════════════════════════════════════════════════════
# AUTO-POSTER LOOP
# ═══════════════════════════════════════════════════════════════

async def post_to_telegram(custom_text: str = None) -> dict:
    """Post to Telegram channel."""
    if not can_post("telegram"):
        return {"status": "rate_limited", "platform": "telegram"}

    stats = get_swarmesh_stats()

    if custom_text:
        text = custom_text
        idx = -1
    else:
        idx = get_next_template_idx("telegram")
        text = TELEGRAM_TEMPLATES[idx](stats)

    result = await telegram_post_channel(text)

    if result.get("status") == "posted":
        record_post("telegram", str(result.get("message_id", "")), result.get("url", ""), text[:200], idx)
        logger.info("Telegram posted: %s", result.get("url"))
    else:
        logger.error("Telegram post failed: %s", result)

    return result


async def auto_post_loop():
    """Background loop: post every MIN_POST_INTERVAL_HOURS."""
    logger.info("Auto-poster started. Interval: %dh, Max/day: %d", MIN_POST_INTERVAL_HOURS, MAX_POSTS_PER_DAY)

    while True:
        try:
            # Telegram (primary — working)
            if can_post("telegram"):
                result = await post_to_telegram()
                logger.info("Auto-telegram result: %s", result.get("status"))

            # Twitter (when project enrollment is fixed)
            if can_post("twitter"):
                result = post_to_twitter()
                logger.info("Auto-tweet result: %s", result.get("status"))

            # Reddit (only if creds work)
            if REDDIT_CLIENT_ID and can_post("reddit"):
                result = post_to_reddit()
                logger.info("Auto-reddit result: %s", result.get("status"))

        except Exception as e:
            logger.error("Auto-post error: %s", e)

        # Sleep until next window
        await asyncio.sleep(MIN_POST_INTERVAL_HOURS * 3600)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("""Usage:
  python social_poster.py status          — Check all credentials
  python social_poster.py telegram        — Post next template to @swarmesh channel
  python social_poster.py telegram "text" — Post custom text to channel
  python social_poster.py tweet           — Post next template tweet
  python social_poster.py tweet-ai        — Post AI-generated tweet
  python social_poster.py tweet "text"    — Post custom tweet
  python social_poster.py reddit          — Post next template to Reddit
  python social_poster.py history         — Show post history
  python social_poster.py auto            — Run auto-poster loop
  python social_poster.py test            — Dry-run (verify creds, don't post)
""")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "status":
        result = status_check()
        print(json.dumps(result, indent=2))

    elif cmd == "test":
        print("=== Credential Check ===")
        tw = twitter_verify()
        print(f"Twitter: {tw}")
        rd_token = reddit_get_token()
        print(f"Reddit: {'OK (token len=' + str(len(rd_token)) + ')' if rd_token else 'FAILED (no valid creds)'}")
        print(f"\nSwarMesh stats: {json.dumps(get_swarmesh_stats(), indent=2)}")

    elif cmd == "telegram":
        custom = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        result = asyncio.run(post_to_telegram(custom_text=custom))
        print(json.dumps(result, indent=2))

    elif cmd == "tweet":
        custom = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        result = post_to_twitter(custom_text=custom)
        print(json.dumps(result, indent=2))

    elif cmd == "tweet-ai":
        result = post_to_twitter(use_ai=True)
        print(json.dumps(result, indent=2))

    elif cmd == "reddit":
        result = post_to_reddit()
        print(json.dumps(result, indent=2))

    elif cmd == "history":
        history = get_post_history()
        for h in history:
            print(f"[{h['posted_at']}] {h['platform']} — {h['preview'][:80]}...")
            if h.get("url"):
                print(f"  → {h['url']}")
        if not history:
            print("No posts yet.")

    elif cmd == "auto":
        asyncio.run(auto_post_loop())

    else:
        print(f"Unknown command: {cmd}")
