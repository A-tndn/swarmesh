"""SwarMesh Solana Agent — On-chain data lookups.

Wallet balances, token info, transaction history, validator stats
via public Solana RPC and APIs.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [solana-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("solana-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "solana-oracle"
AGENT_SKILLS = ["solana-lookup", "web-scrape"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/solana_agent_token.json")

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"


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
        "description": "Solana blockchain oracle — wallet balances, token info, transaction history, SOL price.",
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


async def rpc_call(method: str, params: list) -> dict:
    """Make a Solana RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with aiohttp.ClientSession() as session:
        async with session.post(SOLANA_RPC, json=payload,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            if "error" in data:
                return {"error": data["error"]}
            return data.get("result", {})


async def get_balance(address: str) -> dict:
    """Get SOL balance for an address."""
    result = await rpc_call("getBalance", [address])
    if "error" in result:
        return result
    lamports = result.get("value", 0)
    sol = lamports / 1_000_000_000
    return {
        "address": address,
        "lamports": lamports,
        "sol": round(sol, 9),
        "sol_formatted": f"{sol:.4f} SOL",
    }


async def get_token_accounts(address: str) -> dict:
    """Get SPL token accounts for an address."""
    result = await rpc_call("getTokenAccountsByOwner", [
        address,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"}
    ])
    if "error" in result:
        return result

    accounts = result.get("value", [])
    tokens = []
    for acc in accounts[:30]:
        parsed = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        mint = parsed.get("mint", "")
        amount_info = parsed.get("tokenAmount", {})
        ui_amount = amount_info.get("uiAmount", 0)
        if ui_amount and ui_amount > 0:
            tokens.append({
                "mint": mint,
                "amount": ui_amount,
                "decimals": amount_info.get("decimals", 0),
            })

    tokens.sort(key=lambda x: x["amount"], reverse=True)
    return {
        "address": address,
        "token_accounts": len(accounts),
        "non_zero_tokens": len(tokens),
        "tokens": tokens[:20],
    }


async def get_recent_transactions(address: str, limit: int = 10) -> dict:
    """Get recent transaction signatures for an address."""
    result = await rpc_call("getSignaturesForAddress", [
        address, {"limit": min(limit, 20)}
    ])
    if "error" in result:
        return result
    if not isinstance(result, list):
        return {"error": "unexpected response format"}

    txs = []
    for sig in result:
        txs.append({
            "signature": sig.get("signature", "")[:20] + "...",
            "full_signature": sig.get("signature", ""),
            "slot": sig.get("slot", 0),
            "block_time": sig.get("blockTime", 0),
            "status": "success" if sig.get("err") is None else "failed",
            "memo": sig.get("memo", ""),
        })

    return {
        "address": address,
        "transactions": txs,
        "count": len(txs),
    }


async def get_sol_price() -> dict:
    """Get current SOL price from CoinGecko."""
    url = f"{COINGECKO_API}/simple/price?ids=solana&vs_currencies=usd,btc,eth&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                sol = data.get("solana", {})
                return {
                    "sol_usd": sol.get("usd", 0),
                    "sol_btc": sol.get("btc", 0),
                    "sol_eth": sol.get("eth", 0),
                    "change_24h": round(sol.get("usd_24h_change", 0), 2),
                    "market_cap_usd": sol.get("usd_market_cap", 0),
                    "volume_24h_usd": sol.get("usd_24h_vol", 0),
                    "timestamp": int(time.time()),
                }
    except Exception as e:
        return {"error": str(e)}


async def get_network_stats() -> dict:
    """Get Solana network statistics."""
    epoch_info = await rpc_call("getEpochInfo", [])
    supply = await rpc_call("getSupply", [])
    perf = await rpc_call("getRecentPerformanceSamples", [5])

    result = {"network": "mainnet-beta"}

    if not isinstance(epoch_info, dict) or "error" in epoch_info:
        result["epoch"] = {"error": "failed"}
    else:
        result["epoch"] = {
            "epoch": epoch_info.get("epoch", 0),
            "slot": epoch_info.get("absoluteSlot", 0),
            "slot_index": epoch_info.get("slotIndex", 0),
            "slots_in_epoch": epoch_info.get("slotsInEpoch", 0),
            "progress_pct": round(epoch_info.get("slotIndex", 0) / max(epoch_info.get("slotsInEpoch", 1), 1) * 100, 1),
        }

    if isinstance(supply, dict) and "value" in supply:
        s = supply["value"]
        total_sol = s.get("total", 0) / 1_000_000_000
        circulating_sol = s.get("circulating", 0) / 1_000_000_000
        result["supply"] = {
            "total_sol": round(total_sol, 0),
            "circulating_sol": round(circulating_sol, 0),
        }

    if isinstance(perf, list) and perf:
        tps_samples = []
        for sample in perf:
            num_txs = sample.get("numTransactions", 0)
            slot_time = sample.get("samplePeriodSecs", 1)
            tps_samples.append(num_txs / max(slot_time, 1))
        result["performance"] = {
            "avg_tps": round(sum(tps_samples) / max(len(tps_samples), 1), 0),
            "samples": len(tps_samples),
        }

    return result


async def full_wallet_lookup(address: str) -> dict:
    """Complete wallet analysis."""
    balance = await get_balance(address)
    tokens = await get_token_accounts(address)
    txs = await get_recent_transactions(address, 10)
    price = await get_sol_price()

    sol_amount = balance.get("sol", 0)
    usd_price = price.get("sol_usd", 0)
    usd_value = round(sol_amount * usd_price, 2)

    return {
        "status": "success",
        "address": address,
        "balance": balance,
        "usd_value": usd_value,
        "tokens": tokens,
        "recent_transactions": txs,
        "sol_price": price,
    }


def extract_address(task_data: dict) -> str:
    """Extract Solana address from task."""
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        addr = input_data.get("address", "") or input_data.get("wallet", "")
        if addr:
            return addr.strip()

    desc = task_data.get("description", "")
    # Solana addresses are base58, 32-44 chars
    addresses = re.findall(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b', desc)
    # Filter out common false positives
    for addr in addresses:
        if len(addr) >= 32 and not addr.startswith("http"):
            return addr
    return ""


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    desc = task_data.get("description", "").lower()
    address = extract_address(task_data)

    # Price check doesn't need an address
    if "price" in desc or "sol/usd" in desc or "coingecko" in desc:
        price = await get_sol_price()
        if address:
            balance = await get_balance(address)
            return {"status": "success", "price": price, "balance": balance}
        return {"status": "success", "price": price}

    # Network stats
    if "network" in desc or "epoch" in desc or "tps" in desc or "supply" in desc:
        stats = await get_network_stats()
        price = await get_sol_price()
        return {"status": "success", "network": stats, "price": price}

    # Wallet lookup
    if address:
        return await full_wallet_lookup(address)

    # Fallback — return price + network
    price = await get_sol_price()
    stats = await get_network_stats()
    return {"status": "success", "price": price, "network": stats, "note": "No address found, returning market + network data"}


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
                        output = {"error": "Solana lookup timed out (30s)"}
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
