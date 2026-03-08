"""SwarMesh Email Validator Agent — Email verification and intelligence.

MX record validation, SMTP handshake check, disposable email detection,
syntax validation, domain reputation.
"""
import asyncio
import json
import logging
import os
import re
import socket
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [email-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("email-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "email-validator"
AGENT_SKILLS = ["email-verify"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/email_agent_token.json")

# Known disposable email domains
DISPOSABLE_DOMAINS = {
    "tempmail.com", "throwaway.email", "guerrillamail.com", "mailinator.com",
    "yopmail.com", "sharklasers.com", "guerrillamailblock.com", "grr.la",
    "guerrillamail.info", "guerrillamail.net", "guerrillamail.de",
    "trashmail.com", "trashmail.me", "trashmail.net", "dispostable.com",
    "maildrop.cc", "temp-mail.org", "fakeinbox.com", "tempail.com",
    "tempr.email", "10minutemail.com", "mohmal.com", "burnermail.io",
    "mailnesia.com", "tmpmail.net", "tmpmail.org", "boun.cr",
    "discard.email", "discardmail.com", "discardmail.de", "emailondeck.com",
    "getnada.com", "inboxkitten.com", "jetable.org", "mailcatch.com",
    "mintemail.com", "mytemp.email", "throwawaymail.com", "tmail.ws",
    "wegwerfmail.de", "yopmail.fr", "mailsac.com", "harakirimail.com",
}

# Known free email providers
FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "gmx.com",
    "live.com", "msn.com", "yandex.com", "fastmail.com", "tutanota.com",
    "pm.me", "proton.me",
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
        "description": "Email verification — syntax check, MX validation, disposable detection, domain reputation.",
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


def validate_syntax(email: str) -> dict:
    """Validate email syntax."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    valid = bool(re.match(pattern, email))
    parts = email.split("@") if "@" in email else ["", ""]
    return {
        "valid_syntax": valid,
        "local_part": parts[0] if len(parts) == 2 else "",
        "domain": parts[1] if len(parts) == 2 else "",
    }


def check_mx(domain: str) -> dict:
    """Check MX records for domain."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.timeout = 5
        resolver.lifetime = 8

        try:
            mx_records = resolver.resolve(domain, "MX")
            records = sorted(
                [{"priority": r.preference, "host": str(r.exchange).rstrip(".")} for r in mx_records],
                key=lambda x: x["priority"]
            )
            return {"has_mx": True, "mx_records": records}
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # Fallback: check A record
            try:
                a_records = resolver.resolve(domain, "A")
                return {"has_mx": False, "has_a_record": True, "a_records": [str(r) for r in a_records],
                        "note": "No MX but has A record — may accept mail"}
            except Exception:
                return {"has_mx": False, "has_a_record": False}
        except dns.resolver.NoNameservers:
            return {"has_mx": False, "error": "no nameservers"}
    except ImportError:
        # Fallback without dnspython
        try:
            mx_host = socket.getfqdn(domain)
            ip = socket.gethostbyname(domain)
            return {"has_mx": None, "resolved_ip": ip, "note": "dnspython not available, basic resolution only"}
        except (socket.gaierror, OSError):
            return {"has_mx": False, "error": "domain does not resolve"}


async def smtp_check(domain: str, email: str) -> dict:
    """Basic SMTP check — connect to MX and verify RCPT TO."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.timeout = 5

        try:
            mx_answers = resolver.resolve(domain, "MX")
            mx_host = str(sorted(mx_answers, key=lambda x: x.preference)[0].exchange).rstrip(".")
        except Exception:
            mx_host = domain

        # Connect to SMTP
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _smtp_handshake, mx_host, email),
            timeout=10
        )
        return result
    except asyncio.TimeoutError:
        return {"smtp_reachable": None, "note": "SMTP check timed out"}
    except Exception as e:
        return {"smtp_reachable": None, "error": str(e)}


def _smtp_handshake(mx_host: str, email: str) -> dict:
    """Synchronous SMTP handshake."""
    import smtplib
    try:
        smtp = smtplib.SMTP(mx_host, 25, timeout=8)
        smtp.ehlo("swarmesh.xyz")
        code, msg = smtp.mail("verify@swarmesh.xyz")
        if code != 250:
            smtp.quit()
            return {"smtp_reachable": True, "accepts_mail": None, "note": "MAIL FROM rejected"}

        code, msg = smtp.rcpt(email)
        smtp.quit()

        if code == 250:
            return {"smtp_reachable": True, "accepts_mail": True}
        elif code == 550:
            return {"smtp_reachable": True, "accepts_mail": False, "note": "Mailbox does not exist"}
        else:
            return {"smtp_reachable": True, "accepts_mail": None, "smtp_code": code,
                    "note": msg.decode(errors="replace")[:100]}
    except smtplib.SMTPConnectError:
        return {"smtp_reachable": False, "note": "Connection refused"}
    except smtplib.SMTPServerDisconnected:
        return {"smtp_reachable": True, "accepts_mail": None, "note": "Server disconnected (greylisting?)"}
    except Exception as e:
        return {"smtp_reachable": None, "error": str(e)}


async def verify_email(email: str) -> dict:
    """Full email verification."""
    email = email.strip().lower()
    result = {"email": email, "status": "success"}

    # Syntax
    syntax = validate_syntax(email)
    result["syntax"] = syntax
    if not syntax["valid_syntax"]:
        result["verdict"] = "invalid"
        result["reason"] = "Invalid email syntax"
        return result

    domain = syntax["domain"]

    # Domain classification
    result["domain_info"] = {
        "domain": domain,
        "is_disposable": domain in DISPOSABLE_DOMAINS,
        "is_free_provider": domain in FREE_PROVIDERS,
        "type": "disposable" if domain in DISPOSABLE_DOMAINS else
               ("free" if domain in FREE_PROVIDERS else "business/custom"),
    }

    if domain in DISPOSABLE_DOMAINS:
        result["verdict"] = "risky"
        result["reason"] = "Disposable email domain"

    # MX check
    mx_result = check_mx(domain)
    result["mx"] = mx_result

    if not mx_result.get("has_mx") and not mx_result.get("has_a_record"):
        result["verdict"] = "invalid"
        result["reason"] = "Domain has no mail server"
        return result

    # SMTP check (only for non-disposable)
    if domain not in DISPOSABLE_DOMAINS:
        smtp_result = await smtp_check(domain, email)
        result["smtp"] = smtp_result

        if smtp_result.get("accepts_mail") is False:
            result["verdict"] = "invalid"
            result["reason"] = "Mailbox does not exist"
        elif smtp_result.get("accepts_mail") is True:
            if "verdict" not in result:
                result["verdict"] = "valid"
        else:
            if "verdict" not in result:
                result["verdict"] = "unknown"
                result["reason"] = "Could not verify mailbox existence"

    if "verdict" not in result:
        result["verdict"] = "valid" if mx_result.get("has_mx") else "unknown"

    result["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return result


async def multi_verify(emails: list) -> dict:
    """Verify multiple emails."""
    tasks = [verify_email(e) for e in emails[:20]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    verified = []
    for r in results:
        if isinstance(r, Exception):
            verified.append({"error": str(r)})
        else:
            verified.append(r)

    valid = sum(1 for v in verified if isinstance(v, dict) and v.get("verdict") == "valid")
    invalid = sum(1 for v in verified if isinstance(v, dict) and v.get("verdict") == "invalid")
    risky = sum(1 for v in verified if isinstance(v, dict) and v.get("verdict") == "risky")

    return {
        "status": "success",
        "total": len(verified),
        "valid": valid,
        "invalid": invalid,
        "risky": risky,
        "results": verified,
    }


def extract_emails(task_data: dict) -> list:
    """Extract emails from task."""
    input_data = task_data.get("input_data", {})
    emails = []

    if isinstance(input_data, dict):
        email = input_data.get("email", "") or input_data.get("address", "")
        if email:
            emails.append(email.strip())
        email_list = input_data.get("emails", []) or input_data.get("addresses", [])
        if email_list:
            emails.extend(email_list)

    desc = task_data.get("description", "")
    found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', desc)
    for e in found:
        if e not in emails:
            emails.append(e)

    return emails


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    emails = extract_emails(task_data)
    if not emails:
        return {"error": "No email address found in task"}

    if len(emails) == 1:
        return await verify_email(emails[0])
    else:
        return await multi_verify(emails)


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
                        output = {"error": "Email verification timed out (30s)"}
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
