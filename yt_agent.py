"""SwarMesh YouTube Metadata Agent — Video intelligence.

Extracts video metadata using yt-dlp --dump-json (no download).
Title, description, views, duration, thumbnails, channel info.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [yt-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("yt-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "yt-metadata"
AGENT_SKILLS = ["youtube-lookup"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/yt_agent_token.json")


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
        "description": "YouTube video metadata — title, views, duration, channel, description, thumbnails via yt-dlp.",
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


async def get_video_metadata(url: str) -> dict:
    """Extract video metadata using yt-dlp."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--dump-json", "--no-download", "--no-playlist",
            "--socket-timeout", "15", url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            # Try noembed as fallback for restricted videos
            return await _noembed_fallback(url, err)

        data = json.loads(stdout.decode())

        # Format duration
        duration_s = data.get("duration", 0) or 0
        mins, secs = divmod(int(duration_s), 60)
        hours, mins = divmod(mins, 60)
        duration_fmt = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

        # View count formatting
        views = data.get("view_count", 0) or 0
        if views >= 1_000_000:
            views_fmt = "%.1fM" % (views / 1_000_000)
        elif views >= 1_000:
            views_fmt = "%.1fK" % (views / 1_000)
        else:
            views_fmt = str(views)

        result = {
            "url": url,
            "status": "success",
            "title": data.get("title", ""),
            "description": (data.get("description", "") or "")[:500],
            "channel": data.get("channel", "") or data.get("uploader", ""),
            "channel_id": data.get("channel_id", ""),
            "channel_url": data.get("channel_url", ""),
            "upload_date": data.get("upload_date", ""),
            "duration": duration_s,
            "duration_formatted": duration_fmt,
            "view_count": views,
            "views_formatted": views_fmt,
            "like_count": data.get("like_count", 0),
            "comment_count": data.get("comment_count", 0),
            "categories": data.get("categories", []),
            "tags": (data.get("tags", []) or [])[:15],
            "language": data.get("language", ""),
            "age_limit": data.get("age_limit", 0),
            "is_live": data.get("is_live", False),
            "was_live": data.get("was_live", False),
        }

        # Thumbnail
        thumbnails = data.get("thumbnails", [])
        if thumbnails:
            # Get highest quality
            best = sorted(thumbnails, key=lambda t: (t.get("height", 0) or 0), reverse=True)
            if best:
                result["thumbnail"] = best[0].get("url", "")

        # Formats summary
        formats = data.get("formats", [])
        if formats:
            video_formats = [f for f in formats if f.get("vcodec", "none") != "none"]
            audio_formats = [f for f in formats if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"]
            max_res = max((f.get("height", 0) or 0 for f in video_formats), default=0)
            result["max_resolution"] = f"{max_res}p" if max_res else "unknown"
            result["format_count"] = len(formats)

        return result

    except asyncio.TimeoutError:
        return await _noembed_fallback(url, "yt-dlp timed out")
    except json.JSONDecodeError:
        return await _noembed_fallback(url, "yt-dlp returned invalid JSON")
    except FileNotFoundError:
        return await _noembed_fallback(url, "yt-dlp not installed")
    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}


async def _noembed_fallback(url: str, original_error: str) -> dict:
    """Fallback to noembed.com for basic metadata."""
    try:
        api_url = f"https://noembed.com/embed?url={url}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {"url": url, "status": "error", "error": original_error}
                data = await resp.json()

        if "error" in data:
            return {"url": url, "status": "error", "error": original_error, "noembed_error": data["error"]}

        return {
            "url": url,
            "status": "partial",
            "title": data.get("title", ""),
            "channel": data.get("author_name", ""),
            "channel_url": data.get("author_url", ""),
            "thumbnail": data.get("thumbnail_url", ""),
            "provider": data.get("provider_name", ""),
            "note": f"Limited metadata (yt-dlp failed: {original_error})",
        }
    except Exception:
        return {"url": url, "status": "error", "error": original_error}


async def multi_video(urls: list) -> dict:
    """Get metadata for multiple videos."""
    tasks = [get_video_metadata(u) for u in urls[:5]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    videos = []
    for r in results:
        if isinstance(r, Exception):
            videos.append({"error": str(r)})
        else:
            videos.append(r)

    return {
        "status": "success",
        "videos_analyzed": len(videos),
        "results": videos,
    }


def extract_urls(task_data: dict) -> list:
    """Extract YouTube URLs from task."""
    input_data = task_data.get("input_data", {})
    urls = []

    if isinstance(input_data, dict):
        url = input_data.get("url", "") or input_data.get("video_url", "")
        if url:
            urls.append(re.sub(r'[).,;:]+$', '', url))
        url_list = input_data.get("urls", []) or input_data.get("videos", [])
        if url_list:
            urls.extend(url_list)

    desc = task_data.get("description", "")
    # YouTube URL patterns
    yt_patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[a-zA-Z0-9_-]+',
        r'(?:https?://)?youtu\.be/[a-zA-Z0-9_-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/[a-zA-Z0-9_-]+',
    ]
    for pattern in yt_patterns:
        found = re.findall(pattern, desc)
        for u in found:
            if not u.startswith("http"):
                u = "https://" + u
            clean = re.sub(r'[).,;:]+$', '', u)
            if clean not in urls:
                urls.append(clean)

    # Generic URLs that might be videos
    if not urls:
        all_urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
        for u in all_urls:
            clean = re.sub(r'[).,;:]+$', '', u)
            if any(x in clean.lower() for x in ["youtube", "youtu.be", "vimeo", "dailymotion"]):
                urls.append(clean)

    return urls


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    urls = extract_urls(task_data)
    if not urls:
        return {"error": "No YouTube/video URL found in task"}

    if len(urls) == 1:
        return await get_video_metadata(urls[0])
    else:
        return await multi_video(urls)


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
                        output = await asyncio.wait_for(process_task(task), timeout=40)
                    except asyncio.TimeoutError:
                        output = {"error": "YouTube lookup timed out (40s)"}
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
