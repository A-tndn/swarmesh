"""SwarMesh Text Processing Agent — NLP-powered text analysis.

Handles sentiment analysis, keyword extraction, summarization,
entity extraction using TextBlob + built-in Python.
"""
import asyncio
import json
import logging
import os
import re
import time
import hashlib
import collections

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [text-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("text-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "text-processor"
AGENT_SKILLS = ["text-process"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/text_agent_token.json")


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
        "description": "NLP text processor — sentiment, keywords, summarization, entity extraction.",
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


def analyze_sentiment(text: str) -> dict:
    """Sentiment analysis using TextBlob."""
    try:
        from textblob import TextBlob
        blob = TextBlob(text)
        polarity = blob.sentiment.polarity
        subjectivity = blob.sentiment.subjectivity

        if polarity > 0.3:
            label = "positive"
        elif polarity < -0.3:
            label = "negative"
        else:
            label = "neutral"

        return {
            "polarity": round(polarity, 4),
            "subjectivity": round(subjectivity, 4),
            "label": label,
        }
    except Exception as e:
        return {"error": str(e)}


def extract_keywords(text: str) -> dict:
    """Extract keywords and noun phrases."""
    try:
        from textblob import TextBlob
        blob = TextBlob(text)
        noun_phrases = list(set(blob.noun_phrases))[:30]

        # Word frequency (excluding stop words)
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "shall", "can", "need", "dare", "ought",
            "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above", "below",
            "between", "out", "off", "over", "under", "again", "further", "then",
            "once", "and", "but", "or", "nor", "not", "so", "yet", "both",
            "either", "neither", "each", "every", "all", "any", "few", "more",
            "most", "other", "some", "such", "no", "only", "own", "same", "than",
            "too", "very", "just", "because", "if", "when", "that", "this", "it",
        }
        words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
        filtered = [w for w in words if w not in stop_words]
        freq = collections.Counter(filtered).most_common(20)

        return {
            "noun_phrases": noun_phrases,
            "top_words": [{"word": w, "count": c} for w, c in freq],
            "unique_words": len(set(words)),
            "total_words": len(words),
        }
    except Exception as e:
        return {"error": str(e)}


def extract_entities(text: str) -> dict:
    """Extract emails, URLs, IPs, phone numbers from text."""
    emails = list(set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)))
    urls = list(set(re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', text)))
    urls = [re.sub(r'[).,;:]+$', '', u) for u in urls]
    ips = list(set(re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text)))
    phones = list(set(re.findall(r'\+?\d[\d\s\-()]{7,}\d', text)))
    hashes_hex = list(set(re.findall(r'\b[a-fA-F0-9]{32,64}\b', text)))

    return {
        "emails": emails,
        "urls": urls,
        "ip_addresses": ips,
        "phone_numbers": phones,
        "hex_hashes": hashes_hex,
        "total_entities": len(emails) + len(urls) + len(ips) + len(phones),
    }


def summarize_text(text: str, sentences: int = 3) -> dict:
    """Extractive summarization — pick most representative sentences."""
    sents = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sents) <= sentences:
        return {"summary": text, "original_sentences": len(sents), "summary_sentences": len(sents)}

    # Score sentences by word overlap with full text
    words_all = set(re.findall(r'\b[a-zA-Z]{3,}\b', text.lower()))
    scored = []
    for i, s in enumerate(sents):
        s_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', s.lower()))
        overlap = len(s_words & words_all) / max(len(s_words), 1)
        position_bonus = 1.5 if i == 0 else (1.2 if i == len(sents) - 1 else 1.0)
        scored.append((overlap * position_bonus, i, s))

    scored.sort(reverse=True)
    top = sorted(scored[:sentences], key=lambda x: x[1])
    summary = " ".join(s for _, _, s in top)

    return {
        "summary": summary,
        "original_sentences": len(sents),
        "summary_sentences": sentences,
        "compression_ratio": round(len(summary) / max(len(text), 1), 3),
    }


def text_stats(text: str) -> dict:
    """Compute text statistics."""
    words = text.split()
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    paragraphs = [p for p in text.split('\n\n') if p.strip()]
    chars = len(text)
    avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
    avg_sent_len = len(words) / max(len(sentences), 1)

    # Flesch reading ease approximation
    syllable_count = sum(max(1, len(re.findall(r'[aeiouy]+', w.lower()))) for w in words)
    if len(words) > 0 and len(sentences) > 0:
        flesch = 206.835 - 1.015 * (len(words) / len(sentences)) - 84.6 * (syllable_count / len(words))
    else:
        flesch = 0

    return {
        "characters": chars,
        "words": len(words),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "avg_word_length": round(avg_word_len, 2),
        "avg_sentence_length": round(avg_sent_len, 2),
        "flesch_reading_ease": round(flesch, 1),
        "estimated_read_time_sec": round(len(words) / 4.2),
        "md5": hashlib.md5(text.encode()).hexdigest(),
    }


def process_text(task_data: dict) -> dict:
    """Route text processing task."""
    input_data = task_data.get("input_data", {})
    text = ""
    if isinstance(input_data, dict):
        text = input_data.get("text", "") or input_data.get("description", "")
    if not text:
        text = task_data.get("description", "")
    if not text:
        return {"error": "No text content found in task"}

    # Run all analyses
    result = {
        "status": "success",
        "text_preview": text[:200],
        "stats": text_stats(text),
        "sentiment": analyze_sentiment(text),
        "keywords": extract_keywords(text),
        "entities": extract_entities(text),
        "summary": summarize_text(text),
    }
    return result


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
                        logger.warning("Poll failed: %d", r.status)
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

                    # Claim
                    async with session.post(f"{API_URL}/api/agent/claim/{task_id}",
                                             headers=headers,
                                             timeout=aiohttp.ClientTimeout(total=10)) as cr:
                        if cr.status != 200:
                            logger.warning("Claim failed for %s", task_id)
                            continue

                    logger.info("Claimed task: %s", task_id)

                    # Process
                    try:
                        output = process_text(task.get("task", {}))
                    except Exception as e:
                        output = {"error": str(e)}

                    # Submit
                    async with session.post(f"{API_URL}/api/agent/submit/{task_id}",
                                             headers=headers, json={"output": output},
                                             timeout=aiohttp.ClientTimeout(total=10)) as sr:
                        if sr.status == 200:
                            logger.info("Submitted result for %s", task_id)
                        else:
                            logger.error("Submit failed for %s", task_id)

                    await asyncio.sleep(2)

        except Exception as e:
            logger.error("Agent loop error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_agent())
