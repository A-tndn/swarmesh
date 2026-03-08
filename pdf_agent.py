"""SwarMesh PDF Agent — Extract text, tables, metadata from PDFs.

Uses PyMuPDF for fast text extraction and pdfplumber for tables.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [pdf-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("pdf-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "pdf-extractor"
AGENT_SKILLS = ["pdf-extract"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/pdf_agent_token.json")


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
        "description": "PDF extractor — text, tables, metadata, page count from any PDF URL.",
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


async def download_pdf(url: str) -> str:
    """Download PDF to temp file, return path."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise Exception(f"Download failed: HTTP {resp.status}")
            content_type = resp.headers.get("content-type", "")
            data = await resp.read()
            if len(data) > 50_000_000:  # 50MB limit
                raise Exception("PDF too large (>50MB)")

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(data)
                return f.name


def extract_with_pymupdf(pdf_path: str) -> dict:
    """Fast text extraction with PyMuPDF."""
    import fitz

    doc = fitz.open(pdf_path)
    result = {
        "page_count": len(doc),
        "metadata": {
            "title": doc.metadata.get("title", ""),
            "author": doc.metadata.get("author", ""),
            "subject": doc.metadata.get("subject", ""),
            "creator": doc.metadata.get("creator", ""),
            "producer": doc.metadata.get("producer", ""),
            "creation_date": doc.metadata.get("creationDate", ""),
        },
        "pages": [],
        "full_text": "",
    }

    full_text = []
    for i, page in enumerate(doc):
        if i >= 20:  # Limit to 20 pages
            break
        text = page.get_text()
        full_text.append(text)
        result["pages"].append({
            "page": i + 1,
            "text_length": len(text),
            "text_preview": text[:500],
        })

    result["full_text"] = "\n\n".join(full_text)[:10000]
    result["total_chars"] = sum(len(t) for t in full_text)
    result["total_words"] = sum(len(t.split()) for t in full_text)

    # File info
    file_size = os.path.getsize(pdf_path)
    result["file_size_bytes"] = file_size
    with open(pdf_path, "rb") as f:
        result["sha256"] = hashlib.sha256(f.read()).hexdigest()

    doc.close()
    return result


def extract_tables(pdf_path: str) -> list:
    """Extract tables with pdfplumber."""
    try:
        import pdfplumber
        tables = []
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[:10]):  # Limit to 10 pages
                page_tables = page.extract_tables()
                for j, table in enumerate(page_tables):
                    if table and len(table) > 1:
                        # First row as headers
                        headers = [str(h or "").strip() for h in table[0]]
                        rows = []
                        for row in table[1:10]:  # Max 10 rows per table
                            rows.append([str(c or "").strip() for c in row])
                        tables.append({
                            "page": i + 1,
                            "table_index": j,
                            "headers": headers,
                            "rows": rows,
                            "total_rows": len(table) - 1,
                        })
        return tables
    except Exception as e:
        return [{"error": str(e)}]


async def process_pdf(url: str) -> dict:
    """Download and process a PDF."""
    pdf_path = None
    try:
        pdf_path = await download_pdf(url)
        result = extract_with_pymupdf(pdf_path)
        result["tables"] = extract_tables(pdf_path)
        result["url"] = url
        result["status"] = "success"
        return result
    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}
    finally:
        if pdf_path:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass


def extract_url(task_data: dict) -> str:
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        url = input_data.get("url", "")
        if url:
            return re.sub(r'[).,;:]+$', '', url)
    desc = task_data.get("description", "")
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    urls = [re.sub(r'[).,;:]+$', '', u) for u in urls]
    pdf_urls = [u for u in urls if u.lower().endswith(".pdf")]
    return pdf_urls[0] if pdf_urls else (urls[0] if urls else "")


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    url = extract_url(task_data)
    if not url:
        return {"error": "No PDF URL found in task"}
    return await process_pdf(url)


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
                        output = {"error": "PDF processing timed out (45s)"}
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
