"""SwarMesh Code Agent — Safe code execution sandbox.

Executes Python code snippets in subprocess with resource limits.
Returns stdout, stderr, execution time.
"""
import asyncio
import json
import logging
import os
import resource
import subprocess
import sys
import tempfile
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [code-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("code-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "code-runner"
AGENT_SKILLS = ["code-execute"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/code_agent_token.json")

# Safety limits
MAX_EXEC_TIME = 15  # seconds
MAX_OUTPUT = 10000  # chars
BLOCKED_IMPORTS = [
    "subprocess", "os.system", "shutil.rmtree", "socket",
    "__import__", "eval(", "exec(", "compile(",
    "open('/etc", "open('/root", "open('/proc",
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
                    return creds

    payload = {
        "name": AGENT_NAME,
        "skills": AGENT_SKILLS,
        "description": "Safe Python code execution sandbox — runs snippets with resource limits, returns output.",
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


def check_code_safety(code: str) -> str:
    """Basic safety check — returns error message or empty string."""
    code_lower = code.lower()
    for blocked in BLOCKED_IMPORTS:
        if blocked.lower() in code_lower:
            return f"Blocked: contains '{blocked}'"
    if "import os" in code and ("system" in code or "popen" in code or "exec" in code):
        return "Blocked: os.system/popen/exec not allowed"
    if len(code) > 50000:
        return "Code too long (max 50KB)"
    return ""


def execute_code(code: str, language: str = "python") -> dict:
    """Execute code in sandboxed subprocess."""
    if language != "python":
        return {"status": "error", "error": f"Only Python supported, got: {language}"}

    safety = check_code_safety(code)
    if safety:
        return {"status": "blocked", "error": safety}

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        start = time.time()
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=MAX_EXEC_TIME,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "PYTHONPATH": ""},
            cwd="/tmp",
        )
        elapsed = round(time.time() - start, 3)

        stdout = result.stdout[:MAX_OUTPUT] if result.stdout else ""
        stderr = result.stderr[:MAX_OUTPUT] if result.stderr else ""

        return {
            "status": "success" if result.returncode == 0 else "error",
            "return_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "execution_time": elapsed,
            "truncated": len(result.stdout or "") > MAX_OUTPUT,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": f"Execution exceeded {MAX_EXEC_TIME}s limit"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_code(task_data: dict) -> tuple:
    """Extract code and language from task."""
    input_data = task_data.get("input_data", {})
    if isinstance(input_data, dict):
        code = input_data.get("code", "")
        lang = input_data.get("language", "python")
        if code:
            return code, lang

    # Try to extract code block from description
    import re
    desc = task_data.get("description", "")
    # Look for ```python ... ``` blocks
    blocks = re.findall(r'```(?:python)?\s*\n(.*?)```', desc, re.DOTALL)
    if blocks:
        return blocks[0].strip(), "python"

    # Look for inline code
    if "print(" in desc or "def " in desc or "import " in desc:
        # Likely raw Python code in description
        return desc, "python"

    return "", "python"


def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    code, language = extract_code(task_data)

    if not code:
        return {"error": "No code found in task. Provide code in input_data.code or as a code block in description."}

    return execute_code(code, language)


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
