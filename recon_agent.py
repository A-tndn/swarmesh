"""SwarMesh Recon Agent — Security headers, SSL, DNS checks.

Lightweight security reconnaissance using built-in Python + aiohttp.
"""
import asyncio
import json
import logging
import os
import re
import socket
import ssl
import time
from datetime import datetime
from urllib.parse import urlparse

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [recon-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("recon-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "recon-scanner"
AGENT_SKILLS = ["web-scrape", "fetch-url"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/recon_agent_token.json")

# Security headers to check
SECURITY_HEADERS = [
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "x-xss-protection",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "cross-origin-embedder-policy",
]


async def register_or_load() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            creds = json.load(f)
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{API_URL}/api/agent/profile",
                             headers={"Authorization": f"Bearer {creds['token']}"},
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    logger.info("Loaded existing agent: %s", creds["agent_id"])
                    return creds

    payload = {
        "name": AGENT_NAME,
        "skills": AGENT_SKILLS,
        "description": "Security recon — HTTP headers, SSL certs, DNS, tech stack detection.",
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
    logger.info("Registered agent: %s (%s)", AGENT_NAME, creds["agent_id"])
    return creds


async def check_security_headers(url: str) -> dict:
    """Check HTTP security headers for a URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   allow_redirects=True) as resp:
                headers = dict(resp.headers)
                headers_lower = {k.lower(): v for k, v in headers.items()}

                present = {}
                missing = []
                for h in SECURITY_HEADERS:
                    if h in headers_lower:
                        present[h] = headers_lower[h]
                    else:
                        missing.append(h)

                # Score
                score = len(present) / len(SECURITY_HEADERS) * 100

                # Detect server/tech
                server = headers_lower.get("server", "unknown")
                powered_by = headers_lower.get("x-powered-by", "")
                via = headers_lower.get("via", "")

                # Check cookies for security flags
                cookies = resp.cookies
                cookie_analysis = []
                for name, cookie in cookies.items():
                    flags = {
                        "secure": cookie.get("secure", "") != "",
                        "httponly": cookie.get("httponly", "") != "",
                        "samesite": cookie.get("samesite", "not set"),
                    }
                    cookie_analysis.append({"name": name, "flags": flags})

                return {
                    "url": str(resp.url),
                    "status_code": resp.status,
                    "security_headers_present": present,
                    "security_headers_missing": missing,
                    "security_score": round(score, 1),
                    "server": server,
                    "x_powered_by": powered_by,
                    "via": via,
                    "cookies": cookie_analysis[:10],
                    "content_type": headers_lower.get("content-type", ""),
                    "redirect_chain": [str(h.url) for h in resp.history] if resp.history else [],
                }
    except Exception as e:
        return {"url": url, "error": str(e)}


def check_ssl(hostname: str) -> dict:
    """Check SSL certificate details."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
                cipher = ssock.cipher()

                not_before = cert.get("notBefore", "")
                not_after = cert.get("notAfter", "")

                # Parse expiry
                days_left = None
                if not_after:
                    try:
                        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                        days_left = (expiry - datetime.utcnow()).days
                    except ValueError:
                        pass

                subject = dict(x[0] for x in cert.get("subject", []))
                issuer = dict(x[0] for x in cert.get("issuer", []))
                san = [v for _, v in cert.get("subjectAltName", [])]

                return {
                    "hostname": hostname,
                    "valid": True,
                    "subject": subject,
                    "issuer": issuer,
                    "not_before": not_before,
                    "not_after": not_after,
                    "days_until_expiry": days_left,
                    "san": san[:20],
                    "protocol": ssock.version(),
                    "cipher": cipher[0] if cipher else "",
                    "cipher_bits": cipher[2] if cipher and len(cipher) > 2 else 0,
                }
    except ssl.SSLError as e:
        return {"hostname": hostname, "valid": False, "error": str(e)}
    except Exception as e:
        return {"hostname": hostname, "error": str(e)}


def dns_lookup(hostname: str) -> dict:
    """Basic DNS lookup."""
    try:
        ips = socket.getaddrinfo(hostname, None)
        ipv4 = list(set(addr[4][0] for addr in ips if addr[0] == socket.AF_INET))
        ipv6 = list(set(addr[4][0] for addr in ips if addr[0] == socket.AF_INET6))

        return {
            "hostname": hostname,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "total_records": len(ipv4) + len(ipv6),
        }
    except Exception as e:
        return {"hostname": hostname, "error": str(e)}


def detect_tech(headers: dict, body: str = "") -> list:
    """Detect technologies from headers and HTML."""
    tech = []
    headers_lower = {k.lower(): v.lower() for k, v in headers.items()}

    server = headers_lower.get("server", "")
    if "nginx" in server:
        tech.append("Nginx")
    if "apache" in server:
        tech.append("Apache")
    if "cloudflare" in server:
        tech.append("Cloudflare")

    powered = headers_lower.get("x-powered-by", "")
    if "php" in powered:
        tech.append("PHP")
    if "express" in powered:
        tech.append("Express.js")
    if "asp.net" in powered:
        tech.append("ASP.NET")

    if "x-amzn" in str(headers_lower):
        tech.append("AWS")
    if "x-vercel" in str(headers_lower):
        tech.append("Vercel")

    body_lower = body.lower()[:5000]
    if "react" in body_lower or "_next" in body_lower:
        tech.append("React/Next.js")
    if "vue" in body_lower:
        tech.append("Vue.js")
    if "wordpress" in body_lower or "wp-content" in body_lower:
        tech.append("WordPress")
    if "shopify" in body_lower:
        tech.append("Shopify")

    return list(set(tech))


async def full_recon(url: str) -> dict:
    """Run full recon on a URL."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Run checks
    headers_result = await check_security_headers(url)
    ssl_result = check_ssl(hostname) if parsed.scheme == "https" else {"skipped": "not HTTPS"}
    dns_result = dns_lookup(hostname)

    # Tech detection from headers
    raw_headers = headers_result.get("security_headers_present", {})
    tech = detect_tech(raw_headers)

    return {
        "url": url,
        "hostname": hostname,
        "status": "success",
        "security_headers": headers_result,
        "ssl_certificate": ssl_result,
        "dns": dns_result,
        "technologies": tech,
        "scanned_at": datetime.utcnow().isoformat(),
    }


def extract_url(task_data: dict) -> str:
    """Extract URL from task."""
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        url = input_data.get("url", "")
        if url:
            return re.sub(r'[).,;:]+$', '', url)

    desc = task_data.get("description", "")
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    if urls:
        return re.sub(r'[).,;:]+$', '', urls[0])
    return ""


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    url = extract_url(task_data)
    desc = task_data.get("description", "").lower()

    if not url:
        return {"error": "No URL found in task"}

    # Decide what kind of scan based on description
    if "security" in desc or "header" in desc or "ssl" in desc or "check" in desc:
        return await full_recon(url)
    else:
        # Default: security headers + basic fetch
        return await check_security_headers(url)


async def run_agent():
    creds = await register_or_load()
    token = creds["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    logger.info("Agent running: %s | skills: %s | polling every %ds",
                AGENT_NAME, AGENT_SKILLS, POLL_INTERVAL)

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
                        output = {"error": "Scan timed out (30s)"}
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
