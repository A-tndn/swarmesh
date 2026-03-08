"""SwarMesh Port Scanner Agent — TCP service discovery.

Async TCP connect scan for open ports, service detection,
banner grabbing on common ports.
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

logging.basicConfig(level="INFO", format="%(asctime)s [port-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("port-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "port-scanner"
AGENT_SKILLS = ["port-scan"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/port_agent_token.json")

# Common ports with service names
COMMON_PORTS = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
    80: "HTTP", 110: "POP3", 111: "RPCBind", 135: "MSRPC",
    139: "NetBIOS", 143: "IMAP", 443: "HTTPS", 445: "SMB",
    465: "SMTPS", 587: "Submission", 993: "IMAPS", 995: "POP3S",
    1433: "MSSQL", 1521: "Oracle", 2082: "cPanel", 2083: "cPanel-SSL",
    3000: "Dev/Grafana", 3306: "MySQL", 3389: "RDP",
    5432: "PostgreSQL", 5900: "VNC", 6379: "Redis",
    7770: "SwarMesh", 8000: "HTTP-Alt", 8080: "HTTP-Proxy",
    8443: "HTTPS-Alt", 8888: "HTTP-Alt", 9090: "Prometheus",
    9200: "Elasticsearch", 11211: "Memcached", 27017: "MongoDB",
}

# Quick scan: top 30 most common
QUICK_PORTS = [21, 22, 23, 25, 53, 80, 110, 143, 443, 445, 465, 587,
               993, 995, 3000, 3306, 3389, 5432, 5900, 6379, 8000,
               8080, 8443, 8888, 9090, 9200, 27017]


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
        "description": "TCP port scanner — async connect scan, service detection, banner grab on common ports.",
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


async def scan_port(host: str, port: int, timeout: float = 3.0) -> dict:
    """Check if a single port is open."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return {"port": port, "state": "open", "service": COMMON_PORTS.get(port, "unknown")}
    except asyncio.TimeoutError:
        return {"port": port, "state": "filtered"}
    except ConnectionRefusedError:
        return {"port": port, "state": "closed"}
    except OSError:
        return {"port": port, "state": "filtered"}


async def grab_banner(host: str, port: int, timeout: float = 3.0) -> str:
    """Try to grab service banner."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )

        # Some services send banner immediately
        try:
            banner = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            return banner.decode(errors="replace").strip()[:200]
        except asyncio.TimeoutError:
            # Try sending probe
            if port in (80, 8080, 8000, 8443, 443):
                writer.write(b"HEAD / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
                await writer.drain()
                try:
                    banner = await asyncio.wait_for(reader.read(1024), timeout=2.0)
                    writer.close()
                    await writer.wait_closed()
                    return banner.decode(errors="replace").strip()[:200]
                except asyncio.TimeoutError:
                    pass
            writer.close()
            await writer.wait_closed()
            return ""
    except Exception:
        return ""


async def scan_host(host: str, ports: list = None, grab_banners: bool = True) -> dict:
    """Scan a host for open ports."""
    if ports is None:
        ports = QUICK_PORTS

    # Resolve hostname
    try:
        ip = socket.gethostbyname(host)
    except socket.gaierror:
        return {"host": host, "status": "error", "error": "Could not resolve hostname"}

    start = time.monotonic()

    # Scan all ports concurrently (with semaphore to limit concurrency)
    sem = asyncio.Semaphore(20)

    async def limited_scan(port):
        async with sem:
            return await scan_port(ip, port, timeout=3.0)

    results = await asyncio.gather(*[limited_scan(p) for p in ports])

    open_ports = [r for r in results if r["state"] == "open"]
    filtered_ports = [r for r in results if r["state"] == "filtered"]

    # Banner grab on open ports
    if grab_banners and open_ports:
        for port_info in open_ports[:10]:
            banner = await grab_banner(ip, port_info["port"])
            if banner:
                port_info["banner"] = banner

    scan_time = round((time.monotonic() - start) * 1000)

    return {
        "host": host,
        "ip": ip,
        "status": "success",
        "ports_scanned": len(ports),
        "open": len(open_ports),
        "filtered": len(filtered_ports),
        "closed": len(ports) - len(open_ports) - len(filtered_ports),
        "open_ports": open_ports,
        "scan_time_ms": scan_time,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


def extract_target(task_data: dict) -> dict:
    """Extract scan target from task."""
    input_data = task_data.get("input_data", {})
    target = {}

    if isinstance(input_data, dict):
        host = input_data.get("host", "") or input_data.get("target", "") or input_data.get("domain", "")
        if host:
            target["host"] = host.strip()
        ports = input_data.get("ports", [])
        if ports:
            target["ports"] = [int(p) for p in ports[:100]]

    desc = task_data.get("description", "")

    if "host" not in target:
        # Find URLs
        urls = re.findall(r'https?://([^\s/<>"{}|\\^`\[\]:]+)', desc)
        if urls:
            target["host"] = urls[0].rstrip(".,;:")
        else:
            # Find domains
            domains = re.findall(r'\b([a-zA-Z0-9][-a-zA-Z0-9]*\.(?:com|org|net|io|xyz|dev|co|ai|app|me|info|biz))\b', desc)
            if domains:
                target["host"] = domains[0]
            else:
                # Find IPs
                ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', desc)
                if ips:
                    target["host"] = ips[0]

    # Check for "full scan" or specific port mentions
    if "full" in desc.lower() or "all ports" in desc.lower():
        target["ports"] = list(COMMON_PORTS.keys())
    elif "ports" not in target:
        port_nums = re.findall(r'\bport\s*(\d+)\b', desc.lower())
        if port_nums:
            target["ports"] = [int(p) for p in port_nums if 1 <= int(p) <= 65535]

    return target


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    target = extract_target(task_data)

    if not target.get("host"):
        return {"error": "No target host/domain/IP found in task"}

    host = target["host"]
    if host.startswith("http"):
        host = urlparse(host).hostname or host

    ports = target.get("ports", QUICK_PORTS)
    return await scan_host(host, ports)


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
                        output = {"error": "Port scan timed out (45s)"}
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
