"""SwarMesh Feed Agent — RSS/Atom feed parser.

Parses any RSS/Atom feed URL, extracts entries, summaries, metadata.
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

import aiohttp
import feedparser

logging.basicConfig(level="INFO", format="%(asctime)s [feed-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("feed-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "feed-parser"
AGENT_SKILLS = ["rss-parse", "web-scrape"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/feed_agent_token.json")


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
        "description": "RSS/Atom feed parser — extracts entries, titles, summaries, dates from any feed URL.",
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


def clean_html(html: str) -> str:
    """Strip HTML tags."""
    text = re.sub(r'<[^>]+>', '', html)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def parse_feed(url: str) -> dict:
    """Download and parse an RSS/Atom feed."""
    try:
        # Download feed content
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   headers={"User-Agent": "SwarMesh-FeedParser/1.0"}) as resp:
                if resp.status != 200:
                    return {"url": url, "error": f"HTTP {resp.status}", "status": "error"}
                content = await resp.text()

        # Parse with feedparser
        feed = feedparser.parse(content)

        if feed.bozo and not feed.entries:
            return {"url": url, "error": "Invalid feed format", "status": "error"}

        # Feed metadata
        result = {
            "url": url,
            "status": "success",
            "feed": {
                "title": feed.feed.get("title", ""),
                "description": clean_html(feed.feed.get("description", "") or feed.feed.get("subtitle", "")),
                "link": feed.feed.get("link", ""),
                "language": feed.feed.get("language", ""),
                "updated": feed.feed.get("updated", ""),
                "generator": feed.feed.get("generator", ""),
            },
            "entries": [],
            "total_entries": len(feed.entries),
        }

        # Parse entries (max 30)
        for entry in feed.entries[:30]:
            parsed_entry = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", "") or entry.get("updated", ""),
                "author": entry.get("author", ""),
            }

            # Summary/content
            summary = entry.get("summary", "")
            if summary:
                parsed_entry["summary"] = clean_html(summary)[:500]

            # Content (if different from summary)
            content_list = entry.get("content", [])
            if content_list:
                full_content = clean_html(content_list[0].get("value", ""))
                if full_content and full_content != parsed_entry.get("summary", ""):
                    parsed_entry["content_preview"] = full_content[:300]

            # Categories/tags
            tags = entry.get("tags", [])
            if tags:
                parsed_entry["tags"] = [t.get("term", "") for t in tags][:10]

            # Enclosures (media)
            enclosures = entry.get("enclosures", [])
            if enclosures:
                parsed_entry["media"] = [{"url": e.get("href", ""), "type": e.get("type", "")} for e in enclosures[:3]]

            result["entries"].append(parsed_entry)

        return result

    except Exception as e:
        return {"url": url, "error": str(e), "status": "error"}


async def multi_feed(urls: list) -> dict:
    """Parse multiple feeds."""
    tasks = [parse_feed(url) for url in urls[:10]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    feeds = []
    for r in results:
        if isinstance(r, Exception):
            feeds.append({"error": str(r)})
        else:
            feeds.append(r)

    total_entries = sum(f.get("total_entries", 0) for f in feeds if isinstance(f, dict))

    return {
        "status": "success",
        "feeds_parsed": len(feeds),
        "total_entries": total_entries,
        "feeds": feeds,
    }


def extract_urls(task_data: dict) -> list:
    """Extract feed URLs from task."""
    input_data = task_data.get("input_data", {})
    urls = []

    if isinstance(input_data, dict):
        url = input_data.get("url", "") or input_data.get("feed_url", "")
        if url:
            urls.append(re.sub(r'[).,;:]+$', '', url))
        url_list = input_data.get("urls", []) or input_data.get("feeds", [])
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
        return {"error": "No feed URL found in task"}

    if len(urls) == 1:
        return await parse_feed(urls[0])
    else:
        return await multi_feed(urls)


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
                        output = {"error": "Feed parsing timed out (30s)"}
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
