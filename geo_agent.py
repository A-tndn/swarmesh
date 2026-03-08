"""SwarMesh Geo/IP Intelligence Agent — IP geolocation and network intel.

IP address lookups: geolocation, ASN, ISP, reverse DNS, threat indicators.
Uses free public APIs (ip-api.com, ipapi.co).
"""
import asyncio
import json
import logging
import os
import re
import socket
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [geo-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("geo-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "geo-intel"
AGENT_SKILLS = ["ip-lookup"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/geo_agent_token.json")


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
        "description": "IP geolocation and network intelligence — location, ASN, ISP, reverse DNS, threat indicators.",
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


async def lookup_ip(ip: str) -> dict:
    """Full IP intelligence lookup."""
    result = {"ip": ip, "status": "success"}

    # Primary: ip-api.com (free, 45 req/min)
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,asname,reverse,mobile,proxy,hosting,query"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if data.get("status") == "success":
                    result["geo"] = {
                        "country": data.get("country", ""),
                        "country_code": data.get("countryCode", ""),
                        "region": data.get("regionName", ""),
                        "city": data.get("city", ""),
                        "zip": data.get("zip", ""),
                        "lat": data.get("lat", 0),
                        "lon": data.get("lon", 0),
                        "timezone": data.get("timezone", ""),
                    }
                    result["network"] = {
                        "isp": data.get("isp", ""),
                        "org": data.get("org", ""),
                        "as_number": data.get("as", ""),
                        "as_name": data.get("asname", ""),
                    }
                    result["reverse_dns"] = data.get("reverse", "")
                    result["flags"] = {
                        "is_mobile": data.get("mobile", False),
                        "is_proxy": data.get("proxy", False),
                        "is_hosting": data.get("hosting", False),
                    }
                else:
                    result["error"] = data.get("message", "lookup failed")
                    result["status"] = "error"
    except Exception as e:
        result["primary_error"] = str(e)

    # Reverse DNS fallback
    if not result.get("reverse_dns"):
        try:
            hostname = socket.gethostbyaddr(ip)
            result["reverse_dns"] = hostname[0]
        except (socket.herror, socket.gaierror, OSError):
            pass

    # Secondary: ipapi.co (free, 1000/day)
    try:
        url2 = f"https://ipapi.co/{ip}/json/"
        async with aiohttp.ClientSession() as session:
            async with session.get(url2, timeout=aiohttp.ClientTimeout(total=8),
                                   headers={"User-Agent": "SwarMesh-GeoAgent/1.0"}) as resp:
                if resp.status == 200:
                    data2 = await resp.json()
                    if not data2.get("error"):
                        result["extra"] = {
                            "asn": data2.get("asn", ""),
                            "org": data2.get("org", ""),
                            "currency": data2.get("currency", ""),
                            "languages": data2.get("languages", ""),
                            "country_calling_code": data2.get("country_calling_code", ""),
                            "country_area": data2.get("country_area", 0),
                            "country_population": data2.get("country_population", 0),
                        }
    except Exception:
        pass

    result["looked_up_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return result


async def multi_ip_lookup(ips: list) -> dict:
    """Look up multiple IPs."""
    tasks = [lookup_ip(ip) for ip in ips[:10]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    lookups = []
    for r in results:
        if isinstance(r, Exception):
            lookups.append({"error": str(r)})
        else:
            lookups.append(r)

    return {
        "status": "success",
        "ips_looked_up": len(lookups),
        "results": lookups,
    }


def extract_ips(task_data: dict) -> list:
    """Extract IP addresses from task."""
    input_data = task_data.get("input_data", {})
    ips = []

    if isinstance(input_data, dict):
        ip = input_data.get("ip", "") or input_data.get("address", "")
        if ip:
            ips.append(ip.strip())
        ip_list = input_data.get("ips", []) or input_data.get("addresses", [])
        if ip_list:
            ips.extend(ip_list)

    desc = task_data.get("description", "")
    # IPv4
    found_v4 = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', desc)
    for ip in found_v4:
        parts = ip.split(".")
        if all(0 <= int(p) <= 255 for p in parts) and ip not in ips:
            ips.append(ip)

    # Domain to resolve
    if not ips:
        domains = re.findall(r'\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|xyz|dev|co|ai|app|me|info|biz))\b', desc)
        for domain in domains[:3]:
            try:
                resolved = socket.gethostbyname(domain)
                ips.append(resolved)
            except (socket.gaierror, OSError):
                pass

    return ips


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    ips = extract_ips(task_data)
    if not ips:
        return {"error": "No IP address or resolvable domain found in task"}

    if len(ips) == 1:
        return await lookup_ip(ips[0])
    else:
        return await multi_ip_lookup(ips)


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
                        output = {"error": "IP lookup timed out (30s)"}
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
