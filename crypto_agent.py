"""SwarMesh Crypto Price Agent — Multi-chain price oracle.

Any coin/token price via CoinGecko API. Trending coins, market overview,
price comparisons, historical snapshots.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [crypto-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("crypto-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "crypto-oracle"
AGENT_SKILLS = ["crypto-price"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/crypto_agent_token.json")

COINGECKO = "https://api.coingecko.com/api/v3"

# Common coin name -> CoinGecko ID mapping
COIN_MAP = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "bnb": "binancecoin", "binance": "binancecoin",
    "xrp": "ripple", "ripple": "ripple",
    "ada": "cardano", "cardano": "cardano",
    "doge": "dogecoin", "dogecoin": "dogecoin",
    "dot": "polkadot", "polkadot": "polkadot",
    "matic": "matic-network", "polygon": "matic-network",
    "avax": "avalanche-2", "avalanche": "avalanche-2",
    "link": "chainlink", "chainlink": "chainlink",
    "uni": "uniswap", "uniswap": "uniswap",
    "atom": "cosmos", "cosmos": "cosmos",
    "ltc": "litecoin", "litecoin": "litecoin",
    "near": "near", "apt": "aptos", "aptos": "aptos",
    "arb": "arbitrum", "arbitrum": "arbitrum",
    "op": "optimism", "optimism": "optimism",
    "sui": "sui", "sei": "sei-network",
    "jup": "jupiter-exchange-solana", "jupiter": "jupiter-exchange-solana",
    "bonk": "bonk", "wif": "dogwifcoin",
    "pepe": "pepe", "shib": "shiba-inu",
    "ton": "the-open-network", "toncoin": "the-open-network",
    "trx": "tron", "tron": "tron",
    "usdt": "tether", "tether": "tether",
    "usdc": "usd-coin",
}


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
        "description": "Multi-chain crypto price oracle — any coin/token price, trending, market overview via CoinGecko.",
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


async def _cg_get(path: str) -> dict:
    """CoinGecko API GET."""
    url = f"{COINGECKO}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                               headers={"Accept": "application/json"}) as resp:
            if resp.status == 429:
                return {"error": "rate_limited", "retry_after": 60}
            if resp.status != 200:
                return {"error": f"HTTP {resp.status}"}
            return await resp.json()


async def get_prices(coin_ids: list) -> dict:
    """Get prices for multiple coins."""
    ids_str = ",".join(coin_ids[:20])
    data = await _cg_get(
        f"/simple/price?ids={ids_str}&vs_currencies=usd,btc,eth"
        "&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
    )
    if "error" in data:
        return data

    results = []
    for coin_id in coin_ids:
        coin_data = data.get(coin_id, {})
        if coin_data:
            results.append({
                "id": coin_id,
                "usd": coin_data.get("usd", 0),
                "btc": coin_data.get("btc", 0),
                "eth": coin_data.get("eth", 0),
                "change_24h_pct": round(coin_data.get("usd_24h_change", 0), 2),
                "market_cap_usd": coin_data.get("usd_market_cap", 0),
                "volume_24h_usd": coin_data.get("usd_24h_vol", 0),
            })

    return {
        "status": "success",
        "coins": results,
        "count": len(results),
        "timestamp": int(time.time()),
    }


async def get_trending() -> dict:
    """Get trending coins on CoinGecko."""
    data = await _cg_get("/search/trending")
    if "error" in data:
        return data

    coins = []
    for item in (data.get("coins", []))[:15]:
        coin = item.get("item", {})
        coins.append({
            "name": coin.get("name", ""),
            "symbol": coin.get("symbol", ""),
            "market_cap_rank": coin.get("market_cap_rank", 0),
            "price_btc": coin.get("price_btc", 0),
            "score": coin.get("score", 0),
        })

    return {
        "status": "success",
        "trending": coins,
        "count": len(coins),
    }


async def get_market_overview() -> dict:
    """Top coins by market cap."""
    data = await _cg_get(
        "/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=20&page=1"
        "&sparkline=false&price_change_percentage=1h,24h,7d"
    )
    if isinstance(data, dict) and "error" in data:
        return data

    if not isinstance(data, list):
        return {"error": "unexpected response"}

    coins = []
    for coin in data[:20]:
        coins.append({
            "rank": coin.get("market_cap_rank", 0),
            "name": coin.get("name", ""),
            "symbol": (coin.get("symbol", "")).upper(),
            "price_usd": coin.get("current_price", 0),
            "market_cap": coin.get("market_cap", 0),
            "volume_24h": coin.get("total_volume", 0),
            "change_1h": round(coin.get("price_change_percentage_1h_in_currency", 0) or 0, 2),
            "change_24h": round(coin.get("price_change_percentage_24h_in_currency", 0) or 0, 2),
            "change_7d": round(coin.get("price_change_percentage_7d_in_currency", 0) or 0, 2),
            "ath": coin.get("ath", 0),
            "ath_change_pct": round(coin.get("ath_change_percentage", 0) or 0, 1),
        })

    total_mcap = sum(c["market_cap"] for c in coins)
    return {
        "status": "success",
        "top_20_market_cap_usd": total_mcap,
        "coins": coins,
    }


async def get_coin_detail(coin_id: str) -> dict:
    """Detailed info for a specific coin."""
    data = await _cg_get(f"/coins/{coin_id}?localization=false&tickers=false&community_data=false&developer_data=false")
    if "error" in data:
        return data

    market = data.get("market_data", {})
    return {
        "status": "success",
        "id": coin_id,
        "name": data.get("name", ""),
        "symbol": (data.get("symbol", "")).upper(),
        "description": (data.get("description", {}).get("en", ""))[:300],
        "market_cap_rank": data.get("market_cap_rank", 0),
        "price_usd": market.get("current_price", {}).get("usd", 0),
        "price_btc": market.get("current_price", {}).get("btc", 0),
        "market_cap_usd": market.get("market_cap", {}).get("usd", 0),
        "volume_24h_usd": market.get("total_volume", {}).get("usd", 0),
        "change_24h_pct": round(market.get("price_change_percentage_24h", 0) or 0, 2),
        "change_7d_pct": round(market.get("price_change_percentage_7d", 0) or 0, 2),
        "change_30d_pct": round(market.get("price_change_percentage_30d", 0) or 0, 2),
        "ath_usd": market.get("ath", {}).get("usd", 0),
        "ath_date": market.get("ath_date", {}).get("usd", ""),
        "atl_usd": market.get("atl", {}).get("usd", 0),
        "circulating_supply": market.get("circulating_supply", 0),
        "total_supply": market.get("total_supply", 0),
        "max_supply": market.get("max_supply"),
        "genesis_date": data.get("genesis_date", ""),
        "homepage": (data.get("links", {}).get("homepage", [""]))[0] if data.get("links", {}).get("homepage") else "",
    }


def extract_coins(task_data: dict) -> list:
    """Extract coin identifiers from task."""
    input_data = task_data.get("input_data", {})
    coins = []

    if isinstance(input_data, dict):
        coin = input_data.get("coin", "") or input_data.get("token", "")
        if coin:
            mapped = COIN_MAP.get(coin.lower().strip(), coin.lower().strip())
            coins.append(mapped)
        coin_list = input_data.get("coins", []) or input_data.get("tokens", [])
        for c in coin_list:
            mapped = COIN_MAP.get(c.lower().strip(), c.lower().strip())
            if mapped not in coins:
                coins.append(mapped)

    desc = task_data.get("description", "").lower()
    # Check for known coin names/symbols
    for key, cg_id in COIN_MAP.items():
        # Match whole word
        if re.search(r'\b' + re.escape(key) + r'\b', desc):
            if cg_id not in coins:
                coins.append(cg_id)

    return coins


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    desc = task_data.get("description", "").lower()
    coins = extract_coins(task_data)

    # Trending request
    if "trending" in desc:
        trending = await get_trending()
        if coins:
            prices = await get_prices(coins[:10])
            return {"status": "success", "trending": trending, "prices": prices}
        return trending

    # Market overview
    if "market" in desc and ("overview" in desc or "top" in desc or "cap" in desc):
        return await get_market_overview()

    # Specific coin detail
    if len(coins) == 1 and ("detail" in desc or "info" in desc or "about" in desc):
        return await get_coin_detail(coins[0])

    # Price lookup
    if coins:
        if len(coins) == 1 and ("detail" not in desc):
            # Single coin — get detailed info
            detail = await get_coin_detail(coins[0])
            if "error" not in detail:
                return detail
        # Multi-coin or fallback
        return await get_prices(coins[:20])

    # Fallback: market overview
    return await get_market_overview()


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
                        output = {"error": "Crypto lookup timed out (30s)"}
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
