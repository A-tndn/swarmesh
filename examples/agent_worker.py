"""
Example: Worker Agent — Earns SOL by completing tasks.

This agent connects to the mesh, announces it can do "summarize" and "web-scrape",
and automatically processes incoming tasks.

Run:
    python -m swarmesh.examples.agent_worker
"""

import asyncio
import logging

from swarmesh.sdk import SwarMeshServer
from swarmesh.core.wallet import Wallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


async def main():
    # Create or load a wallet
    wallet = Wallet()
    print(f"Worker wallet: {wallet.address}")

    server = SwarMeshServer(
        mesh_url="ws://localhost:7770",
        wallet=wallet,
    )

    @server.handle("summarize")
    async def summarize(input_data):
        """Summarize text — simple example."""
        text = input_data.get("text", "")
        words = text.split()
        if len(words) > 50:
            summary = " ".join(words[:50]) + "..."
        else:
            summary = text
        return {"summary": summary, "word_count": len(words)}

    @server.handle("web-scrape")
    async def scrape(input_data):
        """Scrape a URL — example using aiohttp."""
        import aiohttp
        url = input_data.get("url", "")
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                text = await resp.text()
                return {
                    "status": resp.status,
                    "length": len(text),
                    "title": text.split("<title>")[1].split("</title>")[0] if "<title>" in text else "",
                    "preview": text[:500],
                }

    @server.handle("echo")
    async def echo(input_data):
        """Simple echo for testing."""
        return {"echo": input_data, "agent": wallet.address}

    await server.connect()
    print("Worker is online and listening for tasks...")
    await server.listen()


if __name__ == "__main__":
    asyncio.run(main())
