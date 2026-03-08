"""SwarMesh DNS/WHOIS Agent — Domain intelligence.

WHOIS lookups, DNS record enumeration, subdomain discovery.
"""
import asyncio
import json
import logging
import os
import re
import socket
import time
from urllib.parse import urlparse

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [dns-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("dns-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "dns-intel"
AGENT_SKILLS = ["dns-lookup"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/dns_agent_token.json")


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
        "description": "Domain intelligence — WHOIS, DNS records, MX/NS/TXT lookups, subdomain probing.",
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


def whois_lookup(domain: str) -> dict:
    """WHOIS lookup for a domain."""
    try:
        import whois
        w = whois.whois(domain)
        result = {
            "domain": domain,
            "registrar": w.registrar or "",
            "creation_date": str(w.creation_date) if w.creation_date else "",
            "expiration_date": str(w.expiration_date) if w.expiration_date else "",
            "updated_date": str(w.updated_date) if w.updated_date else "",
            "name_servers": [],
            "status": [],
            "org": w.org or "",
            "country": w.country or "",
            "state": w.state or "",
        }
        if w.name_servers:
            ns = w.name_servers if isinstance(w.name_servers, list) else [w.name_servers]
            result["name_servers"] = [str(n).lower() for n in ns]
        if w.status:
            st = w.status if isinstance(w.status, list) else [w.status]
            result["status"] = [str(s) for s in st[:10]]
        return result
    except Exception as e:
        return {"domain": domain, "error": str(e)}


def dns_records(domain: str) -> dict:
    """Get all DNS records for a domain."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 10

        records = {}
        record_types = ["A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA"]

        for rtype in record_types:
            try:
                answers = resolver.resolve(domain, rtype)
                if rtype == "MX":
                    records[rtype] = [{"priority": r.preference, "host": str(r.exchange).rstrip(".")} for r in answers]
                elif rtype == "SOA":
                    for r in answers:
                        records[rtype] = {
                            "mname": str(r.mname).rstrip("."),
                            "rname": str(r.rname).rstrip("."),
                            "serial": r.serial,
                            "refresh": r.refresh,
                            "retry": r.retry,
                            "expire": r.expire,
                        }
                else:
                    records[rtype] = [str(r).strip('"') for r in answers]
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
                pass
            except Exception:
                pass

        return {"domain": domain, "records": records, "record_types_found": list(records.keys())}
    except Exception as e:
        return {"domain": domain, "error": str(e)}


async def probe_subdomains(domain: str) -> dict:
    """Probe common subdomains."""
    common_subs = [
        "www", "mail", "ftp", "smtp", "pop", "imap", "webmail",
        "api", "dev", "staging", "test", "admin", "portal",
        "app", "cdn", "static", "media", "img", "images",
        "docs", "help", "support", "status", "blog", "shop",
        "store", "m", "mobile", "ns1", "ns2", "vpn", "remote",
        "git", "gitlab", "jenkins", "ci", "dashboard",
    ]

    found = []
    for sub in common_subs:
        hostname = f"{sub}.{domain}"
        try:
            ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
            ip_list = list(set(addr[4][0] for addr in ips))
            if ip_list:
                found.append({"subdomain": hostname, "ips": ip_list})
        except (socket.gaierror, OSError):
            pass

    return {
        "domain": domain,
        "probed": len(common_subs),
        "found": len(found),
        "subdomains": found,
    }


async def full_domain_intel(domain: str) -> dict:
    """Complete domain intelligence report."""
    # Clean domain
    domain = domain.strip().lower()
    if domain.startswith("http"):
        domain = urlparse(domain).hostname or domain
    domain = domain.rstrip("/.")

    whois_data = whois_lookup(domain)
    dns_data = dns_records(domain)
    subdomain_data = await probe_subdomains(domain)

    return {
        "status": "success",
        "domain": domain,
        "whois": whois_data,
        "dns": dns_data,
        "subdomains": subdomain_data,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


def extract_domain(task_data: dict) -> str:
    """Extract domain from task."""
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        domain = input_data.get("domain", "") or input_data.get("url", "")
        if domain:
            if domain.startswith("http"):
                return urlparse(domain).hostname or ""
            return domain.strip()

    desc = task_data.get("description", "")
    # Find URLs first
    urls = re.findall(r'https?://([^\s/<>"{}|\\^`\[\])]+)', desc)
    if urls:
        return urls[0].rstrip(".,;:")

    # Find bare domains
    domains = re.findall(r'\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|xyz|dev|co|ai|app|me|info|biz))\b', desc)
    if domains:
        return domains[0]

    return ""


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    domain = extract_domain(task_data)
    if not domain:
        return {"error": "No domain found in task"}
    return await full_domain_intel(domain)


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
                        output = await asyncio.wait_for(process_task(task), timeout=45)
                    except asyncio.TimeoutError:
                        output = {"error": "DNS lookup timed out (45s)"}
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
