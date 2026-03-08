"""SwarMesh Image Analyzer Agent — Image metadata and analysis.

Downloads images from URLs, extracts dimensions, format, EXIF data,
file size, color depth, dominant colors estimation.
"""
import asyncio
import io
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [image-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("image-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "image-analyzer"
AGENT_SKILLS = ["image-analyze"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/image_agent_token.json")

MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20MB


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
        "description": "Image metadata analyzer — dimensions, format, EXIF, file size, color info from image URLs.",
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


async def analyze_image(url: str) -> dict:
    """Download and analyze an image from URL."""
    result = {"url": url, "status": "success"}

    try:
        # Download image
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20),
                                   headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}) as resp:
                if resp.status != 200:
                    return {"url": url, "status": "error", "error": f"HTTP {resp.status}"}

                content_type = resp.headers.get("Content-Type", "")
                content_length = resp.headers.get("Content-Length", "")

                # Check size
                if content_length and int(content_length) > MAX_IMAGE_SIZE:
                    return {"url": url, "status": "error", "error": f"Image too large ({content_length} bytes)"}

                image_data = await resp.read()
                if len(image_data) > MAX_IMAGE_SIZE:
                    return {"url": url, "status": "error", "error": f"Image too large ({len(image_data)} bytes)"}

        result["download"] = {
            "size_bytes": len(image_data),
            "size_kb": round(len(image_data) / 1024, 1),
            "size_mb": round(len(image_data) / (1024 * 1024), 2),
            "content_type": content_type,
        }

        # Try PIL/Pillow
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS

            img = Image.open(io.BytesIO(image_data))

            result["image"] = {
                "format": img.format or "unknown",
                "mode": img.mode,
                "width": img.width,
                "height": img.height,
                "aspect_ratio": f"{img.width}:{img.height}",
                "megapixels": round((img.width * img.height) / 1_000_000, 2),
            }

            # Color info
            if img.mode in ("RGB", "RGBA"):
                result["image"]["color_depth"] = "24-bit" if img.mode == "RGB" else "32-bit"
                result["image"]["has_alpha"] = img.mode == "RGBA"

                # Dominant colors (sample-based)
                try:
                    small = img.copy()
                    small.thumbnail((50, 50))
                    if small.mode == "RGBA":
                        small = small.convert("RGB")
                    pixels = list(small.getdata())
                    from collections import Counter
                    color_counts = Counter(pixels)
                    top_colors = color_counts.most_common(5)
                    result["dominant_colors"] = [
                        {"rgb": list(c), "hex": "#{:02x}{:02x}{:02x}".format(*c), "frequency": round(n/len(pixels)*100, 1)}
                        for c, n in top_colors
                    ]
                except Exception:
                    pass
            elif img.mode in ("L", "1"):
                result["image"]["color_depth"] = "8-bit grayscale" if img.mode == "L" else "1-bit"
            elif img.mode == "P":
                result["image"]["color_depth"] = "palette"

            # Animation
            if hasattr(img, "n_frames"):
                result["image"]["frames"] = img.n_frames
                result["image"]["is_animated"] = img.n_frames > 1

            # EXIF data
            try:
                exif_data = img._getexif()
                if exif_data:
                    exif = {}
                    for tag_id, value in exif_data.items():
                        tag = TAGS.get(tag_id, str(tag_id))
                        if isinstance(value, bytes):
                            continue  # Skip binary data
                        if isinstance(value, (int, float, str)):
                            exif[tag] = value
                        elif isinstance(value, tuple) and len(value) <= 4:
                            exif[tag] = list(value)
                    if exif:
                        # Keep only interesting EXIF fields
                        interesting = {}
                        for key in ["Make", "Model", "DateTime", "DateTimeOriginal",
                                    "ExposureTime", "FNumber", "ISOSpeedRatings",
                                    "FocalLength", "LensModel", "Software",
                                    "ImageWidth", "ImageLength", "Orientation"]:
                            if key in exif:
                                interesting[key] = exif[key]
                        if interesting:
                            result["exif"] = interesting
            except Exception:
                pass

            img.close()

        except ImportError:
            # Fallback: basic analysis without Pillow
            result["note"] = "Pillow not available — basic analysis only"
            # Detect format from magic bytes
            if image_data[:8] == b'\x89PNG\r\n\x1a\n':
                result["image"] = {"format": "PNG"}
                # Extract dimensions from IHDR
                if len(image_data) > 24:
                    w = int.from_bytes(image_data[16:20], "big")
                    h = int.from_bytes(image_data[20:24], "big")
                    result["image"]["width"] = w
                    result["image"]["height"] = h
            elif image_data[:2] == b'\xff\xd8':
                result["image"] = {"format": "JPEG"}
            elif image_data[:4] == b'GIF8':
                result["image"] = {"format": "GIF"}
            elif image_data[:4] == b'RIFF' and image_data[8:12] == b'WEBP':
                result["image"] = {"format": "WEBP"}
            else:
                result["image"] = {"format": "unknown"}

        # Hash
        import hashlib
        result["sha256"] = hashlib.sha256(image_data).hexdigest()

    except Exception as e:
        return {"url": url, "status": "error", "error": str(e)}

    result["analyzed_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return result


async def multi_analyze(urls: list) -> dict:
    """Analyze multiple images."""
    tasks = [analyze_image(u) for u in urls[:5]]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    analyses = []
    for r in results:
        if isinstance(r, Exception):
            analyses.append({"error": str(r)})
        else:
            analyses.append(r)

    return {
        "status": "success",
        "images_analyzed": len(analyses),
        "results": analyses,
    }


def extract_image_urls(task_data: dict) -> list:
    """Extract image URLs from task."""
    input_data = task_data.get("input_data", {})
    urls = []

    if isinstance(input_data, dict):
        url = input_data.get("url", "") or input_data.get("image_url", "") or input_data.get("image", "")
        if url:
            urls.append(re.sub(r'[).,;:]+$', '', url))
        url_list = input_data.get("urls", []) or input_data.get("images", [])
        if url_list:
            urls.extend(url_list)

    desc = task_data.get("description", "")
    # Find URLs that look like images
    found = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', desc)
    for u in found:
        clean = re.sub(r'[).,;:]+$', '', u)
        if clean not in urls:
            urls.append(clean)

    return urls


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    urls = extract_image_urls(task_data)
    if not urls:
        return {"error": "No image URL found in task"}

    if len(urls) == 1:
        return await analyze_image(urls[0])
    else:
        return await multi_analyze(urls)


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
                        output = {"error": "Image analysis timed out (30s)"}
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
