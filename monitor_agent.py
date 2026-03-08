"""SwarMesh Monitor Agent — Uptime checks, response time, site diff.

Monitors URLs for availability, performance, and content changes.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [monitor-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("monitor-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "uptime-monitor"
AGENT_SKILLS = ["site-monitor", "fetch-url"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/monitor_agent_token.json")


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
        "description": "Uptime monitor — checks site availability, response time, SSL expiry, content hash.",
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


async def check_url(url: str) -> dict:
    """Full health check on a URL."""
    result = {
        "url": url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        start = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=15),
                                   allow_redirects=True) as resp:
                elapsed = round((time.monotonic() - start) * 1000)  # ms
                body = await resp.text()

                result["status"] = "up"
                result["status_code"] = resp.status
                result["response_time_ms"] = elapsed
                result["content_length"] = len(body)
                result["content_hash"] = hashlib.md5(body.encode()).hexdigest()
                result["content_type"] = resp.headers.get("content-type", "")
                result["server"] = resp.headers.get("server", "")

                # Performance rating
                if elapsed < 200:
                    result["performance"] = "excellent"
                elif elapsed < 500:
                    result["performance"] = "good"
                elif elapsed < 1000:
                    result["performance"] = "fair"
                elif elapsed < 3000:
                    result["performance"] = "slow"
                else:
                    result["performance"] = "very_slow"

                # Redirect chain
                if resp.history:
                    result["redirects"] = [
                        {"url": str(h.url), "status": h.status}
                        for h in resp.history
                    ]
                    result["final_url"] = str(resp.url)

                # Check for error pages
                body_lower = body.lower()[:2000]
                if resp.status >= 400:
                    result["status"] = "error"
                elif any(err in body_lower for err in [
                    "502 bad gateway", "503 service", "500 internal",
                    "site is down", "maintenance mode", "under construction"
                ]):
                    result["status"] = "degraded"
                    result["note"] = "Page content suggests issues despite 200 status"

    except aiohttp.ClientConnectorError:
        result["status"] = "down"
        result["error"] = "Connection refused"
        result["response_time_ms"] = -1
    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["error"] = "Request timed out (15s)"
        result["response_time_ms"] = 15000
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        result["response_time_ms"] = -1

    return result


async def multi_check(urls: list) -> dict:
    """Check multiple URLs concurrently."""
    tasks = [check_url(url) for url in urls[:20]]  # Max 20 URLs
    results = await asyncio.gather(*tasks, return_exceptions=True)

    checks = []
    up_count = 0
    total_time = 0
    for r in results:
        if isinstance(r, Exception):
            checks.append({"error": str(r)})
        else:
            checks.append(r)
            if r.get("status") == "up":
                up_count += 1
            rt = r.get("response_time_ms", 0)
            if rt > 0:
                total_time += rt

    valid_count = sum(1 for c in checks if c.get("response_time_ms", 0) > 0)

    return {
        "status": "success",
        "checks": checks,
        "summary": {
            "total": len(checks),
            "up": up_count,
            "down": len(checks) - up_count,
            "avg_response_ms": round(total_time / max(valid_count, 1)),
            "uptime_pct": round(up_count / max(len(checks), 1) * 100, 1),
        },
    }


def extract_urls(task_data: dict) -> list:
    """Extract URLs from task data."""
    input_data = task_data.get("input_data", {})
    urls = []

    if isinstance(input_data, dict):
        url = input_data.get("url", "")
        if url:
            urls.append(re.sub(r'[).,;:]+$', '', url))
        url_list = input_data.get("urls", [])
        if url_list:
            urls.extend(url_list)

    desc = task_data.get("description", "")
    found = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    for u in found:
        clean = re.sub(r'[).,;:]+$', '', u)
        if clean not in urls:
            urls.append(clean)

    return urls


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    urls = extract_urls(task_data)

    if not urls:
        return {"error": "No URLs found in task"}

    if len(urls) == 1:
        return await check_url(urls[0])
    else:
        return await multi_check(urls)


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
                        output = await asyncio.wait_for(process_task(task), timeout=60)
                    except asyncio.TimeoutError:
                        output = {"error": "Monitor check timed out (60s)"}
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
