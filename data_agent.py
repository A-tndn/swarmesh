"""SwarMesh Data Agent — JSON transforms, CSV parsing, hash computation.

Multi-skill agent handling structured data operations.
"""
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [data-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("data-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "data-transformer"
AGENT_SKILLS = ["json-transform", "csv-parse", "hash-compute"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/data_agent_token.json")


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
        "description": "Data transformer — JSON filtering/sorting, CSV parsing, hash computation.",
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


# --- JSON Transform ---

def json_transform(input_data: dict) -> dict:
    """Apply filter/sort/limit/aggregate operations on JSON data."""
    data = input_data.get("data", [])
    operations = input_data.get("operations", [])

    if not isinstance(data, list):
        return {"error": "data must be a list", "status": "error"}

    result = list(data)

    for op in operations:
        op_type = op.get("type", "")

        if op_type == "filter":
            key = op.get("key", "")
            operator = op.get("operator", "eq")
            value = op.get("value")
            if key:
                filtered = []
                for item in result:
                    item_val = item.get(key)
                    if item_val is None:
                        continue
                    try:
                        if operator == "eq" and item_val == value:
                            filtered.append(item)
                        elif operator == "neq" and item_val != value:
                            filtered.append(item)
                        elif operator == "gt" and float(item_val) > float(value):
                            filtered.append(item)
                        elif operator == "gte" and float(item_val) >= float(value):
                            filtered.append(item)
                        elif operator == "lt" and float(item_val) < float(value):
                            filtered.append(item)
                        elif operator == "lte" and float(item_val) <= float(value):
                            filtered.append(item)
                        elif operator == "contains" and str(value).lower() in str(item_val).lower():
                            filtered.append(item)
                    except (ValueError, TypeError):
                        continue
                result = filtered

        elif op_type == "sort":
            key = op.get("key", "")
            reverse = op.get("reverse", False)
            if key:
                try:
                    result.sort(key=lambda x: x.get(key, 0), reverse=reverse)
                except TypeError:
                    result.sort(key=lambda x: str(x.get(key, "")), reverse=reverse)

        elif op_type == "limit":
            count = int(op.get("count", 10))
            result = result[:count]

        elif op_type == "pick":
            fields = op.get("fields", [])
            if fields:
                result = [{k: item.get(k) for k in fields} for item in result]

        elif op_type == "aggregate":
            key = op.get("key", "")
            agg_type = op.get("agg", "sum")
            if key:
                values = [float(item.get(key, 0)) for item in result if item.get(key) is not None]
                agg_result = {}
                if agg_type in ("sum", "all"):
                    agg_result["sum"] = sum(values)
                if agg_type in ("avg", "all"):
                    agg_result["avg"] = sum(values) / max(len(values), 1)
                if agg_type in ("min", "all"):
                    agg_result["min"] = min(values) if values else 0
                if agg_type in ("max", "all"):
                    agg_result["max"] = max(values) if values else 0
                if agg_type in ("count", "all"):
                    agg_result["count"] = len(values)
                return {"status": "success", "aggregation": agg_result, "field": key}

        elif op_type == "group_by":
            key = op.get("key", "")
            if key:
                groups = {}
                for item in result:
                    group_val = str(item.get(key, "unknown"))
                    groups.setdefault(group_val, []).append(item)
                return {
                    "status": "success",
                    "groups": {k: {"count": len(v), "items": v[:10]} for k, v in groups.items()},
                    "total_groups": len(groups),
                }

    return {
        "status": "success",
        "result": result,
        "count": len(result),
        "operations_applied": len(operations),
    }


# --- CSV Parse ---

def csv_parse(input_data: dict) -> dict:
    """Parse CSV data and return structured analysis."""
    csv_text = input_data.get("csv", "") or input_data.get("text", "")
    if not csv_text:
        return {"error": "No CSV data provided", "status": "error"}

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        if not rows:
            return {"status": "success", "rows": 0, "columns": [], "data": []}

        columns = list(rows[0].keys())

        # Basic stats per column
        col_stats = {}
        for col in columns:
            values = [r[col] for r in rows if r.get(col)]
            # Try numeric
            numeric_vals = []
            for v in values:
                try:
                    numeric_vals.append(float(v))
                except (ValueError, TypeError):
                    pass

            if numeric_vals and len(numeric_vals) > len(values) * 0.5:
                col_stats[col] = {
                    "type": "numeric",
                    "count": len(numeric_vals),
                    "min": min(numeric_vals),
                    "max": max(numeric_vals),
                    "avg": round(sum(numeric_vals) / len(numeric_vals), 4),
                    "sum": round(sum(numeric_vals), 4),
                }
            else:
                unique = list(set(values))
                col_stats[col] = {
                    "type": "text",
                    "count": len(values),
                    "unique": len(unique),
                    "sample": unique[:5],
                }

        return {
            "status": "success",
            "rows": len(rows),
            "columns": columns,
            "column_stats": col_stats,
            "preview": rows[:5],
        }
    except Exception as e:
        return {"error": str(e), "status": "error"}


# --- Hash Compute ---

def hash_compute(input_data: dict) -> dict:
    """Compute hashes for given input."""
    text = input_data.get("text", "") or input_data.get("description", "")

    # Replace {timestamp} placeholder
    if "{timestamp}" in text:
        text = text.replace("{timestamp}", str(int(time.time())))

    if not text:
        text = f"swarmesh-hash-{int(time.time())}"

    data = text.encode("utf-8")

    return {
        "status": "success",
        "input": text[:200],
        "input_length": len(text),
        "hashes": {
            "md5": hashlib.md5(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "sha512": hashlib.sha512(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "blake2b": hashlib.blake2b(data).hexdigest(),
        },
        "timestamp": int(time.time()),
    }


# --- Task Router ---

def process_task(task: dict) -> dict:
    """Route task to correct handler based on skill."""
    task_data = task.get("task", {})
    skill = task.get("skill", "")
    input_data = task_data.get("input_data", {})

    if not isinstance(input_data, dict):
        input_data = {"text": str(input_data)}

    # Merge description into input_data if text is missing
    if not input_data.get("text") and not input_data.get("data") and not input_data.get("csv"):
        desc = task_data.get("description", "")
        if desc:
            input_data["text"] = desc
            input_data["description"] = desc

    if skill == "json-transform":
        return json_transform(input_data)
    elif skill == "csv-parse":
        return csv_parse(input_data)
    elif skill == "hash-compute":
        return hash_compute(input_data)

    return {"error": "Unknown skill: " + skill}


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
                    skill = task.get("skill", "")
                    logger.info("Found task: %s (skill: %s)", task_id, skill)

                    async with session.post(f"{API_URL}/api/agent/claim/{task_id}",
                                             headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=10)) as cr:
                        if cr.status != 200:
                            continue

                    logger.info("Claimed: %s", task_id)

                    try:
                        output = process_task(task)
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
