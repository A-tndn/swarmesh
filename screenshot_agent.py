"""SwarMesh Screenshot Agent — Capture website screenshots.

Uses Playwright (installed with Crawl4AI) to capture full-page
or viewport screenshots, returns base64 + metadata.
"""
import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [screenshot-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("screenshot-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "screenshot-taker"
AGENT_SKILLS = ["screenshot"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/screenshot_agent_token.json")


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
        "description": "Website screenshot capture — full-page or viewport screenshots via headless browser.",
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


async def take_screenshot(url: str, full_page: bool = False, width: int = 1280, height: int = 720) -> dict:
    """Capture screenshot of a URL using Playwright."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            start = time.monotonic()
            await page.goto(url, wait_until="networkidle", timeout=20000)
            load_time = round((time.monotonic() - start) * 1000)

            # Get page info
            title = await page.title()
            page_url = page.url

            # Take screenshot
            screenshot_bytes = await page.screenshot(full_page=full_page, type="png")
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

            # Get page dimensions
            dimensions = await page.evaluate("""() => ({
                width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight,
                viewport_width: window.innerWidth,
                viewport_height: window.innerHeight,
            })""")

            await browser.close()

            return {
                "url": url,
                "final_url": page_url,
                "status": "success",
                "title": title,
                "load_time_ms": load_time,
                "screenshot_b64": screenshot_b64[:100] + "..." if len(screenshot_b64) > 100 else screenshot_b64,
                "screenshot_size_bytes": len(screenshot_bytes),
                "screenshot_b64_length": len(screenshot_b64),
                "dimensions": dimensions,
                "full_page": full_page,
                "viewport": f"{width}x{height}",
                "format": "png",
                "note": "Full base64 screenshot available — truncated in preview to save bandwidth",
            }

    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}


def extract_url(task_data: dict) -> str:
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        url = input_data.get("url", "")
        if url:
            return re.sub(r'[).,;:]+$', '', url)
    desc = task_data.get("description", "")
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    if urls:
        return re.sub(r'[).,;:]+$', '', urls[0])
    return ""


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    url = extract_url(task_data)
    if not url:
        return {"error": "No URL found in task"}

    desc = task_data.get("description", "").lower()
    full_page = "full" in desc or "full-page" in desc or "entire" in desc

    input_data = task_data.get("input_data", {})
    width = 1280
    height = 720
    if isinstance(input_data, dict):
        width = int(input_data.get("width", 1280))
        height = int(input_data.get("height", 720))

    return await take_screenshot(url, full_page=full_page, width=width, height=height)


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
                        output = await asyncio.wait_for(process_task(task), timeout=45)
                    except asyncio.TimeoutError:
                        output = {"error": "Screenshot timed out (45s)"}
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
