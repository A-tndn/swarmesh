"""SwarMesh Task Feeder — Auto-generates real tasks to keep the mesh alive.

Posts tasks on a schedule: web scraping, data processing, monitoring.
These are real tasks with real outputs — not fake busywork.
"""
import asyncio
import json
import logging
import os
import random
import time
import secrets

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [feeder] %(levelname)s: %(message)s")
logger = logging.getLogger("feeder")

API_URL = "http://127.0.0.1:7771"

# Real task templates — each produces genuinely useful output
TASK_TEMPLATES = [
    # --- Web Scraping Tasks ---
    {
        "category": "data",
        "description": "Scrape the top stories from Hacker News (https://news.ycombinator.com). Extract title, link, points, and comment count for the top 30 stories.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Extract the current trending repositories from GitHub (https://github.com/trending). Get repo name, description, language, stars today, and URL for each.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Scrape the latest Solana ecosystem news from https://solana.com/news. Extract article titles, dates, and summary for the 10 most recent posts.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Fetch the current SOL/USD price and 24h stats from CoinGecko API (https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd&include_24hr_change=true&include_market_cap=true).",
        "bounty": "0.0005",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Scrape the front page of Reddit r/solana (https://www.reddit.com/r/solana/.json). Extract post titles, scores, comment counts, and URLs for top 20 posts.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Fetch the latest crypto fear and greed index from https://api.alternative.me/fng/?limit=7. Return the index values for the last 7 days with timestamps.",
        "bounty": "0.0005",
        "skill": "web-scrape",
    },
    {
        "category": "security",
        "description": "Check the HTTP security headers for https://swarmesh.xyz. Report on X-Frame-Options, CSP, HSTS, X-Content-Type-Options, and any missing headers.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Scrape the top 10 AI/ML papers from https://arxiv.org/list/cs.AI/recent. Extract title, authors, and abstract summary for each paper.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Fetch the current gas prices for major blockchains from https://api.owlracle.info/v4/eth/gas. Return current, average, and fast gas prices.",
        "bounty": "0.0005",
        "skill": "web-scrape",
    },
    {
        "category": "data",
        "description": "Extract the latest product launches from Product Hunt (https://www.producthunt.com). Get the top 10 products with name, tagline, votes, and URL.",
        "bounty": "0.001",
        "skill": "web-scrape",
    },
    # --- Text Processing Tasks ---
    {
        "category": "analysis",
        "description": "Analyze the word frequency distribution in the Solana whitepaper abstract: 'Solana is a high-performance blockchain supporting builders around the world creating crypto apps that scale. Solana is known for its speed, with 400 millisecond block times and sub-cent transaction fees. The network is designed to scale with Moore s law, doubling in capacity every two years.' Return top words, unique count, and readability metrics.",
        "bounty": "0.0005",
        "skill": "text-process",
    },
    {
        "category": "analysis",
        "description": "Process and clean this raw data dump, extracting all email addresses, URLs, and IP addresses: 'Contact us at info@example.com or visit https://swarmesh.xyz. Server at 192.168.1.1. Backup admin@mesh.io available at https://docs.swarmesh.xyz from 10.0.0.1 gateway.'",
        "bounty": "0.0005",
        "skill": "text-process",
    },
    {
        "category": "analysis",
        "description": "Analyze sentiment and key themes in this crypto market text: 'Bitcoin surged past 100k as institutional adoption accelerates. Solana ecosystem sees record TVL growth while Ethereum gas fees remain a concern. DeFi protocols are exploring cross-chain bridges. NFT market shows signs of recovery with AI-generated collections gaining traction.'",
        "bounty": "0.0005",
        "skill": "text-process",
    },
    # --- JSON Transform Tasks ---
    {
        "category": "automation",
        "description": "Transform this dataset: filter items where price > 50, sort by rating descending, and return top 5.",
        "bounty": "0.0005",
        "skill": "json-transform",
        "input_override": {
            "data": [
                {"name": "Widget A", "price": 29.99, "rating": 4.5},
                {"name": "Widget B", "price": 89.99, "rating": 4.8},
                {"name": "Widget C", "price": 55.00, "rating": 3.9},
                {"name": "Widget D", "price": 120.00, "rating": 4.2},
                {"name": "Widget E", "price": 15.00, "rating": 4.9},
                {"name": "Widget F", "price": 75.50, "rating": 4.7},
                {"name": "Widget G", "price": 200.00, "rating": 4.1},
                {"name": "Widget H", "price": 60.00, "rating": 3.5},
            ],
            "operations": [
                {"type": "filter", "key": "price", "operator": "gt", "value": 50},
                {"type": "sort", "key": "rating", "reverse": True},
                {"type": "limit", "count": 5},
            ]
        },
    },
    # --- Hash Compute Tasks ---
    {
        "category": "automation",
        "description": "Compute SHA256, MD5, and SHA512 hashes for the string 'swarmesh-integrity-check-{timestamp}' where timestamp is current unix time.",
        "bounty": "0.0003",
        "skill": "hash-compute",
    },
    # --- Code Execution Tasks ---
    {
        "category": "code",
        "description": "Execute this Python code and return output:\n```python\nimport math\nprimes = [n for n in range(2, 100) if all(n % i != 0 for i in range(2, int(math.sqrt(n))+1))]\nprint(f'Primes under 100: {len(primes)}')\nprint(primes)\n```",
        "bounty": "0.0005",
        "skill": "code-execute",
    },
    {
        "category": "code",
        "description": "Execute this Python code:\n```python\nimport json, datetime, sys, platform, os\nreport = {'timestamp': datetime.datetime.utcnow().isoformat(), 'python': sys.version.split()[0], 'platform': platform.platform(), 'cpus': os.cpu_count()}\nprint(json.dumps(report, indent=2))\n```",
        "bounty": "0.0005",
        "skill": "code-execute",
    },
    # --- Site Monitor Tasks ---
    {
        "category": "monitor",
        "description": "Check uptime and response time for https://swarmesh.xyz https://solana.com https://api.coingecko.com https://github.com https://news.ycombinator.com",
        "bounty": "0.001",
        "skill": "site-monitor",
    },
    {
        "category": "monitor",
        "description": "Check uptime and response time for https://swarmesh.xyz — report status, response time, server header, content hash.",
        "bounty": "0.0005",
        "skill": "site-monitor",
    },
    # --- PDF Extract Tasks ---
    {
        "category": "pdf",
        "description": "Extract text and metadata from the Bitcoin whitepaper PDF: https://bitcoin.org/bitcoin.pdf",
        "bounty": "0.001",
        "skill": "pdf-extract",
    },
]

# How many tasks to post per cycle
TASKS_PER_CYCLE = 5
# Interval between cycles (seconds)
CYCLE_INTERVAL = 1800  # 30 minutes
# Max pending tasks before we slow down
MAX_PENDING = 10


async def post_task(session: aiohttp.ClientSession, template: dict):
    """Post a single task to the mesh."""
    payload = {
        "description": template["description"],
        "category": template["category"],
        "bounty": template["bounty"],
        "contact": "",
    }
    try:
        async with session.post(f"{API_URL}/api/task", json=payload,
                                 timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if resp.status == 200:
                logger.info("Posted task: %s (mesh_id=%s, bounty=%s SOL)",
                           template["description"][:60], data.get("mesh_task_id", "?"), template["bounty"])
                return True
            else:
                logger.error("Failed to post task: %s", data)
                return False
    except Exception as e:
        logger.error("Post task error: %s", e)
        return False


async def get_pending_count(session: aiohttp.ClientSession) -> int:
    """Check how many tasks are currently pending."""
    try:
        async with session.get(f"{API_URL}/api/health",
                                timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            return data.get("tasks_pending", 0)
    except Exception:
        return 0


async def run_feeder():
    """Main feeder loop."""
    logger.info("Task feeder started (interval: %ds, tasks/cycle: %d)", CYCLE_INTERVAL, TASKS_PER_CYCLE)

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # Check pending count
                pending = await get_pending_count(session)
                if pending >= MAX_PENDING:
                    logger.info("Too many pending tasks (%d), skipping cycle", pending)
                    await asyncio.sleep(CYCLE_INTERVAL)
                    continue

                # Pick random tasks
                tasks_to_post = random.sample(TASK_TEMPLATES, min(TASKS_PER_CYCLE, len(TASK_TEMPLATES)))
                posted = 0
                for template in tasks_to_post:
                    success = await post_task(session, template)
                    if success:
                        posted += 1
                    await asyncio.sleep(2)  # Small delay between posts

                logger.info("Cycle complete: posted %d/%d tasks (pending: %d)",
                           posted, len(tasks_to_post), pending + posted)

        except Exception as e:
            logger.error("Feeder cycle error: %s", e)

        await asyncio.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_feeder())
