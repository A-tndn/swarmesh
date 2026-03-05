"""
SwarMesh Agent Runner — Starts all worker agents in one process.

Usage:
    python -m swarmesh.agents.runner
"""

import asyncio
import logging
import os

from ..core.wallet import Wallet
from .scraper import create_scraper_agent
from .data_agent import create_data_agent

logging.basicConfig(
    level=os.getenv("SWARMESH_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("swarmesh.runner")


async def main():
    mesh_url = os.getenv("SWARMESH_MESH_URL", "ws://localhost:7770")

    # Create wallets for each agent (or load saved ones)
    try:
        scraper_wallet = Wallet.load("agent_scraper")
    except FileNotFoundError:
        scraper_wallet = Wallet()
        scraper_wallet.save("agent_scraper")

    try:
        data_wallet = Wallet.load("agent_data")
    except FileNotFoundError:
        data_wallet = Wallet()
        data_wallet.save("agent_data")

    logger.info("=" * 50)
    logger.info("  SwarMesh Agent Runner")
    logger.info(f"  Mesh: {mesh_url}")
    logger.info(f"  Scraper: {scraper_wallet.address[:12]}...")
    logger.info(f"  Data:    {data_wallet.address[:12]}...")
    logger.info("=" * 50)

    scraper = create_scraper_agent(mesh_url=mesh_url, wallet=scraper_wallet)
    data = create_data_agent(mesh_url=mesh_url, wallet=data_wallet)

    await scraper.connect()
    await data.connect()

    logger.info("All agents connected. Skills available:")
    logger.info(f"  Scraper: {scraper.skills}")
    logger.info(f"  Data:    {data.skills}")

    # Run all agents concurrently
    await asyncio.gather(
        scraper.listen(),
        data.listen(),
    )


if __name__ == "__main__":
    asyncio.run(main())


def main_sync():
    asyncio.run(main())
