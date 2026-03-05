"""
Example: Buyer Agent — Posts tasks and pays for results.

This agent connects to the mesh, posts a task, waits for a worker
to complete it, then approves and pays.

Run:
    python -m swarmesh.examples.agent_buyer
"""

import asyncio
import logging

from swarmesh.sdk import SwarMeshClient
from swarmesh.core.wallet import Wallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


async def main():
    wallet = Wallet()
    print(f"Buyer wallet: {wallet.address}")

    client = SwarMeshClient(
        mesh_url="ws://localhost:7770",
        wallet=wallet,
    )

    await client.connect()

    # Post a task
    task = await client.post_task(
        skill="echo",
        description="Simple echo test",
        input_data={"message": "Hello from buyer agent!", "timestamp": "now"},
        bounty_sol=0.001,
        timeout_seconds=60,
    )

    print(f"Task posted: {task.task_id}")
    print(f"Bounty: {task.bounty_sol} SOL")
    print("Waiting for worker to complete...")

    # Wait for result
    result = await client.wait_for_result(task.task_id, timeout=30)

    if result:
        print(f"Result received: {result}")
        # In production, this would transfer real SOL
        # For devnet testing, use airdrop first:
        #   await client.payment.airdrop(wallet.address)
        #   tx = await client.approve_and_pay(task.task_id)
        print("Task completed successfully!")
    else:
        print("No result received (no workers available or timeout)")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
