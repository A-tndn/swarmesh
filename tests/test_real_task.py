"""Test posting a real task to the live mesh with worker agents."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import aiohttp


async def receive_type(ws, expected, timeout=10):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == expected:
                    return data
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return None
        except asyncio.TimeoutError:
            return None
    return None


async def main():
    print("\n=== Real Task Test ===\n")

    s = aiohttp.ClientSession()
    ws = await s.ws_connect("ws://localhost:7770/ws")

    # Announce as buyer
    await ws.send_str(json.dumps({
        "type": "agent_announce", "sender": "buyer_real",
        "payload": {"skills": []}, "msg_id": "a1", "timestamp": 1, "target": None
    }))
    await asyncio.sleep(1)

    # ---- Test 1: Web Scrape ----
    print("[Test 1] Web Scrape — example.com")
    await ws.send_str(json.dumps({
        "type": "task_post", "sender": "buyer_real",
        "payload": {"task": {
            "task_id": "rt_001", "buyer": "buyer_real", "skill": "web-scrape",
            "description": "Scrape example.com",
            "input_data": {"url": "https://example.com"},
            "bounty_lamports": 100000, "status": "open"
        }},
        "msg_id": "p1", "timestamp": 2, "target": None
    }))

    # Wait for claim
    claim = await receive_type(ws, "task_claim_ack", timeout=5)
    if claim:
        print(f"  Claimed by: {claim['payload']['worker'][:12]}...")
    else:
        print("  No claim received")

    # Wait for result
    result = await receive_type(ws, "task_submit", timeout=15)
    if result:
        output = result["payload"].get("output", {})
        print(f"  Title: {output.get('title', 'N/A')}")
        print(f"  Status: {output.get('status', 'N/A')}")
        print(f"  Length: {output.get('length', 'N/A')} bytes")
        print(f"  Links: {len(output.get('links', []))} found")
        print(f"  PASS")
    else:
        print(f"  FAIL: No result received")

    await asyncio.sleep(1)

    # ---- Test 2: Text Process ----
    print("\n[Test 2] Text Process — word frequency")
    await ws.send_str(json.dumps({
        "type": "task_post", "sender": "buyer_real",
        "payload": {"task": {
            "task_id": "rt_002", "buyer": "buyer_real", "skill": "text-process",
            "description": "Analyze text",
            "input_data": {
                "text": "The quick brown fox jumps over the lazy dog. The dog barked at the fox.",
                "operation": "frequency"
            },
            "bounty_lamports": 50000, "status": "open"
        }},
        "msg_id": "p2", "timestamp": 3, "target": None
    }))

    claim = await receive_type(ws, "task_claim_ack", timeout=5)
    if claim:
        print(f"  Claimed by: {claim['payload']['worker'][:12]}...")

    result = await receive_type(ws, "task_submit", timeout=10)
    if result:
        output = result["payload"].get("output", {})
        freq = output.get("frequency", {})
        print(f"  Top words: {dict(list(freq.items())[:5])}")
        print(f"  Total words: {output.get('total_words', 'N/A')}")
        print(f"  PASS")
    else:
        print(f"  FAIL: No result received")

    await asyncio.sleep(1)

    # ---- Test 3: Hash Compute ----
    print("\n[Test 3] Hash Compute")
    await ws.send_str(json.dumps({
        "type": "task_post", "sender": "buyer_real",
        "payload": {"task": {
            "task_id": "rt_003", "buyer": "buyer_real", "skill": "hash-compute",
            "description": "Hash a string",
            "input_data": {"data": "SwarMesh v0.1.0", "algorithms": ["sha256", "md5", "sha1"]},
            "bounty_lamports": 25000, "status": "open"
        }},
        "msg_id": "p3", "timestamp": 4, "target": None
    }))

    claim = await receive_type(ws, "task_claim_ack", timeout=5)
    result = await receive_type(ws, "task_submit", timeout=10)
    if result:
        hashes = result["payload"]["output"].get("hashes", {})
        for algo, h in hashes.items():
            print(f"  {algo}: {h[:16]}...")
        print(f"  PASS")
    else:
        print(f"  FAIL")

    # Check mesh stats
    print("\n--- Mesh Status ---")
    async with aiohttp.ClientSession() as http:
        async with http.get("http://localhost:7770/health") as resp:
            health = await resp.json()
            print(f"Peers: {health['peers']} | Agents: {health['agents']} | Skills: {len(health['skills'])}")

    await ws.close()
    await s.close()
    print("\nALL REAL TASK TESTS COMPLETE\n")


if __name__ == "__main__":
    asyncio.run(main())
