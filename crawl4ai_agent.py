"""SwarMesh Crawl4AI Agent — Production web scraper powered by Crawl4AI.

Registers as an HTTP agent, polls for tasks, scrapes with Crawl4AI,
submits clean structured results. Runs autonomously.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [crawl4ai-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("crawl4ai-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "crawl4ai-scraper"
AGENT_SKILLS = ["web-scrape", "fetch-url", "extract-links"]
POLL_INTERVAL = 15  # seconds between task polls
TOKEN_FILE = os.path.expanduser("~/.swarmesh/crawl4ai_agent_token.json")


async def register_or_load() -> dict:
    """Register agent or load existing token."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            creds = json.load(f)
        # Verify token still works
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_URL}/api/agent/profile",
                             headers={"Authorization": f"Bearer {creds['token']}"},
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    logger.info("Loaded existing agent: %s", creds["agent_id"])
                    return creds

    # Register new
    payload = {
        "name": AGENT_NAME,
        "skills": AGENT_SKILLS,
        "description": "Production web scraper powered by Crawl4AI. Extracts structured data, markdown, links from any URL.",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_URL}/api/agent/register",
                          json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if r.status != 200:
                logger.error("Registration failed: %s", data)
                raise RuntimeError(f"Registration failed: {data}")

    creds = {"agent_id": data["agent_id"], "token": data["token"]}
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(creds, f)
    logger.info("Registered agent: %s (%s)", AGENT_NAME, creds["agent_id"])
    return creds


async def scrape_url(url: str, description: str = "") -> dict:
    """Scrape a URL using Crawl4AI and return structured output."""
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10,
        remove_overlay_elements=True,
        process_iframes=False,
    )

    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=config)

        output = {
            "url": url,
            "status": "success",
            "title": result.metadata.get("title", "") if result.metadata else "",
            "description": result.metadata.get("description", "") if result.metadata else "",
        }

        # Markdown content
        if result.markdown:
            md = result.markdown.raw_markdown
            output["markdown"] = md[:5000]
            output["markdown_length"] = len(md)

            # Word count
            words = md.split()
            output["word_count"] = len(words)

        # Links
        if result.links:
            internal = [l.get("href", "") for l in result.links.get("internal", [])][:30]
            external = [l.get("href", "") for l in result.links.get("external", [])][:30]
            output["internal_links"] = internal
            output["external_links"] = external
            output["total_links"] = len(internal) + len(external)

        # Media
        if result.media:
            images = [img.get("src", "") for img in result.media.get("images", [])][:20]
            output["images"] = images

        return output

    except Exception as e:
        logger.error("Scrape failed for %s: %s", url, e)
        return {"url": url, "status": "error", "error": str(e)}


async def extract_links_from_url(url: str) -> dict:
    """Extract all links from a URL."""
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=config)

        internal = [l.get("href", "") for l in result.links.get("internal", [])] if result.links else []
        external = [l.get("href", "") for l in result.links.get("external", [])] if result.links else []

        return {
            "url": url,
            "internal_links": list(set(internal)),
            "external_links": list(set(external)),
            "total": len(internal) + len(external),
        }
    except Exception as e:
        return {"url": url, "error": str(e)}


async def fetch_raw(url: str, method: str = "GET") -> dict:
    """Simple URL fetch — raw content."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                return {
                    "url": url,
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type", ""),
                    "body": text[:10000],
                    "length": len(text),
                    "headers": {k: v for k, v in list(resp.headers.items())[:20]},
                }
    except Exception as e:
        return {"url": url, "error": str(e)}


def extract_url_from_task(task_data: dict) -> str:
    """Try to extract a URL from task input data or description."""
    # Check input_data
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        url = input_data.get("url", "")
        if url:
            return url

    # Check description
    desc = task_data.get("description", "")
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    if urls:
        return urls[0]

    # Check for text that looks like it should be analyzed
    text = input_data.get("text", "")
    if text:
        urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', text)
        if urls:
            return urls[0]

    return ""


async def process_task(task: dict) -> dict:
    """Process a single task based on skill type."""
    task_data = task.get("task", {})
    skill = task.get("skill", "")
    url = extract_url_from_task(task_data)
    description = task_data.get("description", "")

    if skill == "web-scrape":
        if url:
            return await scrape_url(url, description)
        else:
            # No URL — try to do text analysis on description
            return {
                "status": "no_url",
                "note": "No URL found in task. Returning description analysis.",
                "description_length": len(description),
                "word_count": len(description.split()),
            }

    elif skill == "extract-links":
        if url:
            return await extract_links_from_url(url)
        return {"error": "No URL provided for link extraction"}

    elif skill == "fetch-url":
        if url:
            method = task_data.get("input_data", {}).get("method", "GET") if isinstance(task_data.get("input_data"), dict) else "GET"
            return await fetch_raw(url, method)
        return {"error": "No URL provided for fetch"}

    return {"error": f"Unknown skill: {skill}"}


async def run_agent():
    """Main agent loop: poll -> claim -> process -> submit."""
    creds = await register_or_load()
    token = creds["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    logger.info("Agent running: %s | skills: %s | polling every %ds",
                AGENT_NAME, AGENT_SKILLS, POLL_INTERVAL)

    consecutive_empty = 0

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # Poll for tasks
                async with session.get(f"{API_URL}/api/agent/tasks",
                                       headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        logger.warning("Poll failed: %d", r.status)
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    data = await r.json()

                tasks = data.get("tasks", [])
                if not tasks:
                    consecutive_empty += 1
                    # Back off if nothing available
                    wait = min(POLL_INTERVAL * (1 + consecutive_empty // 10), 120)
                    await asyncio.sleep(wait)
                    continue

                consecutive_empty = 0

                for task in tasks:
                    task_id = task.get("task_id", "")
                    skill = task.get("skill", "")
                    logger.info("Found task: %s (skill: %s)", task_id, skill)

                    # Claim
                    async with session.post(f"{API_URL}/api/agent/claim/{task_id}",
                                             headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=10)) as cr:
                        if cr.status != 200:
                            claim_data = await cr.json()
                            logger.warning("Claim failed for %s: %s", task_id, claim_data)
                            continue

                    logger.info("Claimed task: %s", task_id)

                    # Process
                    try:
                        output = await asyncio.wait_for(process_task(task), timeout=60)
                    except asyncio.TimeoutError:
                        output = {"error": "Task processing timed out (60s)"}
                    except Exception as e:
                        output = {"error": str(e)}

                    # Submit
                    async with session.post(f"{API_URL}/api/agent/submit/{task_id}",
                                             headers=headers, json={"output": output},
                                             timeout=aiohttp.ClientTimeout(total=10)) as sr:
                        if sr.status == 200:
                            logger.info("Submitted result for %s: %s",
                                       task_id, str(output)[:100])
                        else:
                            sub_data = await sr.json()
                            logger.error("Submit failed for %s: %s", task_id, sub_data)

                    await asyncio.sleep(2)  # Brief pause between tasks

        except Exception as e:
            logger.error("Agent loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_agent())
