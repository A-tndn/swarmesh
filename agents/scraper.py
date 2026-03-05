"""
SwarMesh Web Scraper Agent — Earns SOL by fetching and extracting web data.

Skills: web-scrape, fetch-url, extract-links
"""

import asyncio
import logging
import re
from typing import Any, Dict

import aiohttp

from ..core.wallet import Wallet
from ..sdk.server import SwarMeshServer

logger = logging.getLogger("swarmesh.agent.scraper")


def create_scraper_agent(mesh_url: str = "ws://localhost:7770",
                         wallet: Wallet = None) -> SwarMeshServer:
    wallet = wallet or Wallet()
    server = SwarMeshServer(mesh_url=mesh_url, wallet=wallet)

    @server.handle("web-scrape")
    async def scrape(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Scrape a URL and return structured data."""
        url = input_data.get("url", "")
        if not url:
            return {"error": "No URL provided"}

        selectors = input_data.get("selectors", {})
        timeout = input_data.get("timeout", 15)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                html = await resp.text()

                result = {
                    "url": str(resp.url),
                    "status": resp.status,
                    "content_type": resp.headers.get("content-type", ""),
                    "length": len(html),
                }

                # Extract title
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                result["title"] = title_match.group(1).strip() if title_match else ""

                # Extract meta description
                desc_match = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', html, re.IGNORECASE)
                result["description"] = desc_match.group(1) if desc_match else ""

                # Extract all links
                links = re.findall(r'href="(https?://[^"]+)"', html)
                result["links"] = list(set(links))[:50]

                # Extract text content (strip tags)
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()
                result["text_preview"] = text[:2000]

                # If specific selectors requested (basic CSS-like)
                if selectors:
                    extracted = {}
                    for name, pattern in selectors.items():
                        matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
                        extracted[name] = matches[:20]
                    result["extracted"] = extracted

                return result

    @server.handle("fetch-url")
    async def fetch(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Simple URL fetch — returns raw content."""
        url = input_data.get("url", "")
        method = input_data.get("method", "GET").upper()
        headers = input_data.get("headers", {})
        body = input_data.get("body")

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, json=body,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": text[:10000],
                    "length": len(text),
                }

    @server.handle("extract-links")
    async def extract_links(input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all links from a URL."""
        url = input_data.get("url", "")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
                links = re.findall(r'href="([^"]+)"', html)
                absolute = [l for l in links if l.startswith("http")]
                relative = [l for l in links if not l.startswith("http") and not l.startswith("#")]
                return {
                    "absolute_links": list(set(absolute)),
                    "relative_links": list(set(relative))[:50],
                    "total": len(links),
                }

    return server


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    wallet = Wallet()
    logger.info(f"Scraper agent wallet: {wallet.address}")
    agent = create_scraper_agent(wallet=wallet)
    await agent.connect()
    logger.info("Scraper agent online — accepting web-scrape, fetch-url, extract-links tasks")
    await agent.listen()


if __name__ == "__main__":
    asyncio.run(main())
