"""End-to-end test: Worker + Buyer on live mesh node."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import aiohttp


MESH_URL = "ws://localhost:7770"


async def receive_with_type(ws, expected_type, timeout=5):
    """Receive messages until we get the expected type."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == expected_type:
                    return data
                # Skip other message types
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return None
        except asyncio.TimeoutError:
            return None
    return None


async def test():
    print("\n=== SwarMesh E2E Test ===\n")

    # 1. Connect worker
    ws1 = aiohttp.ClientSession()
    w = await ws1.ws_connect(f"{MESH_URL}/ws")
    print("[1] Worker connected")

    await w.send_str(json.dumps({
        "type": "agent_announce", "sender": "worker_e2e",
        "payload": {"skills": ["echo", "math"], "description": "E2E test worker"},
        "msg_id": "a1", "timestamp": 1, "target": None
    }))
    await asyncio.sleep(1)

    # 2. Connect buyer
    ws2 = aiohttp.ClientSession()
    b = await ws2.ws_connect(f"{MESH_URL}/ws")
    print("[2] Buyer connected")

    await b.send_str(json.dumps({
        "type": "agent_announce", "sender": "buyer_e2e",
        "payload": {"skills": [], "description": "E2E test buyer"},
        "msg_id": "a2", "timestamp": 2, "target": None
    }))
    await asyncio.sleep(1)

    # 3. Verify health
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://localhost:7770/health") as resp:
            health = await resp.json()
            print(f"[3] Mesh health: {health['peers']} peers, {health['agents']} agents, skills={health['skills']}")

    # 4. Buyer posts task
    await b.send_str(json.dumps({
        "type": "task_post", "sender": "buyer_e2e",
        "payload": {"task": {
            "task_id": "e2e_001", "buyer": "buyer_e2e", "skill": "echo",
            "description": "Echo test", "input_data": {"message": "Hello SwarMesh!"},
            "bounty_lamports": 1000000, "status": "open"
        }},
        "msg_id": "p1", "timestamp": 3, "target": None
    }))
    print("[4] Task posted: e2e_001 (echo, 0.001 SOL)")

    # 5. Worker receives task
    task_msg = await receive_with_type(w, "task_post", timeout=5)
    if task_msg:
        print(f"[5] Worker received task: {task_msg['payload']['task']['task_id']}")
    else:
        print("[5] FAIL: Worker didn't receive task")
        await cleanup(w, b, ws1, ws2)
        return

    # 6. Worker claims task
    await w.send_str(json.dumps({
        "type": "task_claim", "sender": "worker_e2e",
        "payload": {"task_id": "e2e_001"},
        "msg_id": "c1", "timestamp": 4, "target": None
    }))
    print("[6] Worker claiming task...")

    # 7. Buyer gets claim ack
    claim_msg = await receive_with_type(b, "task_claim_ack", timeout=5)
    if claim_msg:
        print(f"[7] Buyer got claim ack: worker={claim_msg['payload']['worker']}")
    else:
        print("[7] FAIL: Buyer didn't get claim ack")
        await cleanup(w, b, ws1, ws2)
        return

    # 8. Worker submits result
    await w.send_str(json.dumps({
        "type": "task_submit", "sender": "worker_e2e",
        "payload": {"task_id": "e2e_001", "output": {"echo": "Hello SwarMesh!", "processed": True}},
        "msg_id": "s1", "timestamp": 5, "target": "buyer_e2e"
    }))
    print("[8] Worker submitted result")

    # 9. Buyer gets result
    result_msg = await receive_with_type(b, "task_submit", timeout=5)
    if result_msg:
        print(f"[9] Buyer got result: {result_msg['payload']['output']}")
    else:
        print("[9] FAIL: Buyer didn't get result")
        await cleanup(w, b, ws1, ws2)
        return

    await cleanup(w, b, ws1, ws2)
    print("\nE2E TEST PASSED\n")


async def cleanup(w, b, ws1, ws2):
    await w.close()
    await b.close()
    await ws1.close()
    await ws2.close()


if __name__ == "__main__":
    asyncio.run(test())
