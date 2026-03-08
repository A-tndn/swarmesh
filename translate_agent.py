"""SwarMesh Translate Agent — Text translation.

Translates text between languages using MyMemory API (free, no key needed).
Auto-detects source language, supports 50+ languages.
"""
import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import quote

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [translate-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("translate-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "translator"
AGENT_SKILLS = ["translate"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/translate_agent_token.json")

MYMEMORY_API = "https://api.mymemory.translated.net/get"

# Common language codes
LANG_MAP = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "russian": "ru", "japanese": "ja",
    "chinese": "zh", "korean": "ko", "arabic": "ar", "hindi": "hi",
    "dutch": "nl", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "polish": "pl", "turkish": "tr", "thai": "th",
    "vietnamese": "vi", "indonesian": "id", "malay": "ms", "tagalog": "tl",
    "czech": "cs", "romanian": "ro", "hungarian": "hu", "greek": "el",
    "hebrew": "he", "persian": "fa", "urdu": "ur", "bengali": "bn",
    "tamil": "ta", "telugu": "te", "marathi": "mr", "gujarati": "gu",
    "kannada": "kn", "punjabi": "pa", "ukrainian": "uk", "croatian": "hr",
    "serbian": "sr", "bulgarian": "bg", "slovak": "sk", "slovenian": "sl",
    "estonian": "et", "latvian": "lv", "lithuanian": "lt",
}

LANG_NAMES = {v: k.title() for k, v in LANG_MAP.items()}


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
        "description": "Text translation — 50+ languages via MyMemory API. Auto-detects source language.",
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


def resolve_lang(lang_input: str) -> str:
    """Resolve language name or code to ISO code."""
    lang = lang_input.strip().lower()
    if lang in LANG_MAP:
        return LANG_MAP[lang]
    if len(lang) <= 3:
        return lang  # Already a code
    # Partial match
    for name, code in LANG_MAP.items():
        if name.startswith(lang):
            return code
    return lang


async def translate_text(text: str, source: str = "auto", target: str = "en") -> dict:
    """Translate text using MyMemory API."""
    if not text.strip():
        return {"error": "Empty text"}

    # Limit text length (MyMemory has 500 char limit per request)
    chunks = []
    remaining = text.strip()
    while remaining:
        chunk = remaining[:450]
        # Try to break at sentence boundary
        if len(remaining) > 450:
            last_period = chunk.rfind(".")
            last_newline = chunk.rfind("\n")
            break_at = max(last_period, last_newline)
            if break_at > 200:
                chunk = remaining[:break_at + 1]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].strip()
        if len(chunks) >= 10:
            break

    langpair = f"{source}|{target}" if source != "auto" else f"autodetect|{target}"

    translations = []
    detected_lang = None

    for chunk in chunks:
        try:
            url = f"{MYMEMORY_API}?q={quote(chunk)}&langpair={langpair}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return {"error": f"API returned HTTP {resp.status}"}
                    data = await resp.json()

            rd = data.get("responseData", {})
            translated = rd.get("translatedText", "")
            if not translated:
                return {"error": "No translation returned"}

            translations.append(translated)

            if not detected_lang and rd.get("detectedLanguage"):
                detected_lang = rd["detectedLanguage"]

            # Rate limit courtesy
            if len(chunks) > 1:
                await asyncio.sleep(0.5)

        except Exception as e:
            return {"error": str(e)}

    full_translation = " ".join(translations)

    result = {
        "status": "success",
        "original": text[:500] + ("..." if len(text) > 500 else ""),
        "translated": full_translation,
        "source_language": source if source != "auto" else (detected_lang or "auto"),
        "target_language": target,
        "target_language_name": LANG_NAMES.get(target, target),
        "char_count": len(text),
        "chunks_used": len(chunks),
    }

    if detected_lang:
        result["detected_language"] = detected_lang
        result["detected_language_name"] = LANG_NAMES.get(detected_lang, detected_lang)

    return result


async def multi_translate(text: str, targets: list) -> dict:
    """Translate to multiple languages."""
    tasks = [translate_text(text, "auto", t) for t in targets[:8]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    translations = []
    for r in results:
        if isinstance(r, Exception):
            translations.append({"error": str(r)})
        else:
            translations.append(r)

    return {
        "status": "success",
        "original": text[:300] + ("..." if len(text) > 300 else ""),
        "translations": translations,
        "languages": len(translations),
    }


def extract_translation_request(task_data: dict) -> dict:
    """Parse translation request from task."""
    input_data = task_data.get("input_data", {})
    req = {}

    if isinstance(input_data, dict):
        req["text"] = input_data.get("text", "")
        req["source"] = input_data.get("source", input_data.get("from", "auto"))
        req["target"] = input_data.get("target", input_data.get("to", "en"))
        if input_data.get("targets"):
            req["targets"] = input_data["targets"]

    desc = task_data.get("description", "")

    # Extract target language from description
    if not req.get("target") or req["target"] == "en":
        for lang_name, lang_code in LANG_MAP.items():
            patterns = [
                rf'(?:translate|convert)\s+(?:to|into)\s+{lang_name}',
                rf'(?:in|to)\s+{lang_name}',
                rf'{lang_name}\s+translation',
            ]
            for p in patterns:
                if re.search(p, desc.lower()):
                    req["target"] = lang_code
                    break

    # Extract text to translate (everything in quotes or after "translate:")
    if not req.get("text"):
        # Quoted text
        quoted = re.findall(r'"([^"]+)"', desc) or re.findall(r"'([^']+)'", desc)
        if quoted:
            req["text"] = quoted[0]
        else:
            # After "translate" keyword
            match = re.search(r'translate\s*:?\s*(.+?)(?:\s+(?:to|into|from)\s+\w+|$)', desc, re.IGNORECASE)
            if match:
                text = match.group(1).strip()
                # Remove language hints
                for lang in LANG_MAP:
                    text = re.sub(rf'\b{lang}\b', '', text, flags=re.IGNORECASE).strip()
                if text:
                    req["text"] = text

        # Fallback: use the whole description
        if not req.get("text"):
            # Remove common instruction words
            text = re.sub(r'^(translate|convert|please|can you|help me)\s+', '', desc, flags=re.IGNORECASE)
            text = re.sub(r'\s+(to|into|from)\s+\w+\s*$', '', text, flags=re.IGNORECASE)
            if text and len(text) > 5:
                req["text"] = text.strip()

    return req


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    req = extract_translation_request(task_data)

    if not req.get("text"):
        return {"error": "No text to translate found in task"}

    source = resolve_lang(req.get("source", "auto"))
    target = resolve_lang(req.get("target", "en"))

    if req.get("targets"):
        targets = [resolve_lang(t) for t in req["targets"]]
        return await multi_translate(req["text"], targets)

    return await translate_text(req["text"], source, target)


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
                        output = {"error": "Translation timed out (30s)"}
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
