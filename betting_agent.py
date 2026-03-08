"""SwarMesh Betting Intel Agent — Odds scraping and analysis.

Scrapes live odds, match data, and casino game results from
white-label betting platforms (brownexch-style API).
Educational/research purposes — no bets placed.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [betting-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("betting-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "betting-intel"
AGENT_SKILLS = ["betting-odds", "web-scrape"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/betting_agent_token.json")

# Known white-label platform base URLs (brownexch API pattern)
# These all share the same backend API structure
KNOWN_PLATFORMS = {
    "brownexch": "https://brownexch.com",
    "11xplay": "https://11xplay.com",
    "fairexch": "https://fairexch.com",
}

# Casino game types available on these platforms
CASINO_GAMES = [
    "mogambo", "lucky7", "dt20", "teen", "poker", "dolidana",
    "teen20", "aaa", "baccarat", "lucky7eu", "dt6", "trap",
    "queen", "cmatch20", "teensin", "dt202", "race20",
]


async def register_or_load() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            creds = json.load(f)
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_URL}/api/agent/profile",
                             headers={"Authorization": f"Bearer {creds['token']}"},
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    return creds

    payload = {
        "name": AGENT_NAME,
        "skills": AGENT_SKILLS,
        "description": "Betting platform intelligence — live odds, match data, casino results, platform reconnaissance.",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_URL}/api/agent/register",
                          json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Registration failed: {data}")

    creds = {"agent_id": data["agent_id"], "token": data["token"]}
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(creds, f)
    logger.info("Registered: %s (%s)", AGENT_NAME, creds["agent_id"])
    return creds


async def _api_post(base_url: str, path: str, body: dict = None, session: aiohttp.ClientSession = None) -> dict:
    """Make a POST request to a betting platform API."""
    url = f"{base_url}{path}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": base_url,
        "Referer": f"{base_url}/",
    }
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True

    try:
        async with session.post(url, json=body or {},
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {"error": f"HTTP {resp.status}"}
            return await resp.json()
    except Exception as e:
        return {"error": str(e)}
    finally:
        if close_session:
            await session.close()


async def get_demo_session(base_url: str) -> str:
    """Get a demo session cookie for read-only access."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base_url}/api/front/demo-login",
                                    json={},
                                    headers={"Content-Type": "application/json",
                                             "User-Agent": "Mozilla/5.0"},
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    cookies = resp.cookies
                    g_token = cookies.get("g_token")
                    if g_token:
                        return g_token.value
                    # Some platforms return token in body
                    data = await resp.json()
                    return data.get("token", "")
    except Exception:
        pass
    return ""


async def get_sports_tree(base_url: str) -> dict:
    """Get the full sports navigation tree — live events and categories."""
    data = await _api_post(base_url, "/api/front/treedata", {})
    if "error" in data:
        return data

    tree = data.get("data", {})
    t1 = tree.get("t1", [])  # Categories
    t2 = tree.get("t2", [])  # Live events

    categories = []
    for cat in t1[:20]:
        categories.append({
            "id": cat.get("etid", ""),
            "name": cat.get("ename", ""),
            "count": cat.get("cnt", 0),
        })

    live_events = []
    for ev in t2[:30]:
        live_events.append({
            "id": ev.get("gmid", ""),
            "name": ev.get("ename", ""),
            "sport": ev.get("etname", ""),
            "league": ev.get("cname", ""),
            "time": ev.get("bm_start", ""),
            "in_play": ev.get("inplay", False),
        })

    return {
        "status": "success",
        "platform": base_url,
        "categories": categories,
        "live_events": live_events,
        "total_live": len(t2),
        "total_categories": len(t1),
    }


async def get_casino_data(base_url: str, game_type: str) -> dict:
    """Get live casino game data — current round, odds, cards."""
    data = await _api_post(base_url, "/api/front/casino/data2", {"type": game_type})
    if "error" in data:
        return data

    result_data = data.get("data", data)
    if not result_data:
        return {"error": "No data returned", "game_type": game_type}

    result = {
        "status": "success",
        "platform": base_url,
        "game_type": game_type,
        "mid": result_data.get("mid", ""),
        "cards": result_data.get("card", ""),
    }

    # Parse sections (betting options)
    subs = result_data.get("sub", [])
    sections = []
    for s in subs:
        section = {
            "sid": s.get("sid", ""),
            "name": s.get("nat", ""),
            "status": s.get("gstatus", ""),
            "min_bet": s.get("min", 0),
            "max_bet": s.get("max", 0),
        }
        # Back odds
        back = s.get("b", [])
        if back:
            section["back_odds"] = back[0] if isinstance(back[0], (int, float)) else back
        # Lay odds
        lay = s.get("l", [])
        if lay:
            section["lay_odds"] = lay[0] if isinstance(lay[0], (int, float)) else lay
        sections.append(section)

    result["sections"] = sections
    return result


async def get_last_results(base_url: str, game_type: str) -> dict:
    """Get recent casino game results."""
    data = await _api_post(base_url, "/api/front/casino/lastresultsnew", {"gType": game_type})
    if "error" in data:
        return data

    results = data.get("data", [])
    if not isinstance(results, list):
        results = []

    parsed = []
    for r in results[:20]:
        parsed.append({
            "mid": r.get("mid", ""),
            "result": r.get("result", ""),
            "winner": r.get("winner", ""),
            "time": r.get("createdAt", ""),
        })

    return {
        "status": "success",
        "platform": base_url,
        "game_type": game_type,
        "results": parsed,
        "count": len(parsed),
    }


async def get_casino_list(base_url: str) -> dict:
    """Get available casino games."""
    data = await _api_post(base_url, "/api/front/casino/alllist", {})
    if "error" in data:
        return data

    games = []
    categories = data.get("data", [])
    if isinstance(categories, list):
        for cat in categories:
            cat_name = cat.get("name", "")
            tables = cat.get("games", cat.get("data", []))
            if isinstance(tables, list):
                for table in tables[:10]:
                    games.append({
                        "category": cat_name,
                        "name": table.get("name", ""),
                        "type": table.get("gtype", table.get("gameType", "")),
                        "id": table.get("gmid", table.get("id", "")),
                    })

    return {
        "status": "success",
        "platform": base_url,
        "games": games[:50],
        "total": len(games),
    }


async def platform_recon(base_url: str) -> dict:
    """Full platform reconnaissance."""
    sports, casino_list = await asyncio.gather(
        get_sports_tree(base_url),
        get_casino_list(base_url),
        return_exceptions=True,
    )

    # Try a few casino games for live data
    casino_samples = []
    for game in ["mogambo", "lucky7", "dt20"]:
        try:
            data = await get_casino_data(base_url, game)
            if isinstance(data, dict) and data.get("status") == "success":
                casino_samples.append(data)
        except Exception:
            pass

    result = {
        "status": "success",
        "platform": base_url,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }

    if isinstance(sports, dict) and "error" not in sports:
        result["sports"] = sports
    if isinstance(casino_list, dict) and "error" not in casino_list:
        result["casino_games"] = casino_list
    if casino_samples:
        result["live_casino_samples"] = casino_samples

    return result


def extract_request(task_data: dict) -> dict:
    """Extract betting intel request from task."""
    input_data = task_data.get("input_data", {})
    req = {}

    if isinstance(input_data, dict):
        req["platform"] = input_data.get("platform", "") or input_data.get("url", "")
        req["game_type"] = input_data.get("game_type", "") or input_data.get("game", "")
        req["action"] = input_data.get("action", "")

    desc = task_data.get("description", "").lower()

    # Detect platform
    if not req.get("platform"):
        for name, url in KNOWN_PLATFORMS.items():
            if name in desc:
                req["platform"] = url
                break
        # Generic URL
        if not req.get("platform"):
            urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', task_data.get("description", ""))
            for u in urls:
                clean = re.sub(r'[).,;:]+$', '', u)
                req["platform"] = clean.rstrip("/")
                break

    # Detect game type
    if not req.get("game_type"):
        for game in CASINO_GAMES:
            if game in desc:
                req["game_type"] = game
                break

    # Detect action
    if not req.get("action"):
        if any(w in desc for w in ["results", "history", "last", "recent"]):
            req["action"] = "results"
        elif any(w in desc for w in ["casino", "game", "live", "cards", "odds"]):
            req["action"] = "casino"
        elif any(w in desc for w in ["sports", "match", "cricket", "football", "events"]):
            req["action"] = "sports"
        elif any(w in desc for w in ["recon", "scan", "analyze", "full"]):
            req["action"] = "recon"
        elif any(w in desc for w in ["list", "games", "available"]):
            req["action"] = "list"
        else:
            req["action"] = "recon"

    return req


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    req = extract_request(task_data)

    platform = req.get("platform", "")
    if not platform:
        return {"error": "No betting platform URL found in task. Supported: " + ", ".join(KNOWN_PLATFORMS.keys())}

    # Normalize URL
    if not platform.startswith("http"):
        platform = "https://" + platform
    platform = platform.rstrip("/")

    action = req.get("action", "recon")
    game_type = req.get("game_type", "")

    if action == "recon":
        return await platform_recon(platform)
    elif action == "sports":
        return await get_sports_tree(platform)
    elif action == "casino" and game_type:
        return await get_casino_data(platform, game_type)
    elif action == "results" and game_type:
        return await get_last_results(platform, game_type)
    elif action == "list":
        return await get_casino_list(platform)
    else:
        # Default: full recon
        return await platform_recon(platform)


async def run_agent():
    creds = await register_or_load()
    token = creds["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    logger.info("Agent running: %s | skills: %s", AGENT_NAME, AGENT_SKILLS)
    consecutive_empty = 0

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{API_URL}/api/agent/tasks",
                                       headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    data = await r.json()

                tasks = data.get("tasks", [])
                if not tasks:
                    consecutive_empty += 1
                    wait = min(POLL_INTERVAL * (1 + consecutive_empty // 10), 120)
                    await asyncio.sleep(wait)
                    continue

                consecutive_empty = 0
                for task in tasks:
                    task_id = task.get("task_id", "")
                    logger.info("Found task: %s", task_id)

                    async with session.post(f"{API_URL}/api/agent/claim/{task_id}",
                                             headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=10)) as cr:
                        if cr.status != 200:
                            continue

                    logger.info("Claimed: %s", task_id)
                    try:
                        output = await asyncio.wait_for(process_task(task), timeout=30)
                    except asyncio.TimeoutError:
                        output = {"error": "Betting intel timed out (30s)"}
                    except Exception as e:
                        output = {"error": str(e)}

                    async with session.post(f"{API_URL}/api/agent/submit/{task_id}",
                                             headers=headers, json={"output": output},
                                             timeout=aiohttp.ClientTimeout(total=10)) as sr:
                        if sr.status == 200:
                            logger.info("Submitted: %s", task_id)
                        else:
                            logger.error("Submit failed: %s", task_id)

                    await asyncio.sleep(2)

        except Exception as e:
            logger.error("Agent loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_agent())
