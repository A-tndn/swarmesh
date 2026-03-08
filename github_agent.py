"""SwarMesh GitHub Intel Agent — Repository analysis.

Repo stats, language breakdown, contributors, recent commits,
issues/PRs summary via GitHub REST API (no auth needed for public repos).
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

logging.basicConfig(level="INFO", format="%(asctime)s [github-agent] %(levelname)s: %(message)s")
logger = logging.getLogger("github-agent")

API_URL = os.getenv("SWARMESH_API", "http://127.0.0.1:7771")
AGENT_NAME = "github-intel"
AGENT_SKILLS = ["github-lookup"]
POLL_INTERVAL = 15
TOKEN_FILE = os.path.expanduser("~/.swarmesh/github_agent_token.json")

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "SwarMesh-GitHubAgent/1.0",
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
        "description": "GitHub repository intelligence — stars, forks, languages, contributors, recent activity, issues/PRs.",
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


async def gh_get(path: str) -> dict | list | None:
    """GitHub API GET request."""
    url = f"{GH_API}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=GH_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 403:
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                return {"error": f"rate_limited (remaining: {remaining})"}
            if resp.status == 404:
                return {"error": "not_found"}
            if resp.status != 200:
                return {"error": f"HTTP {resp.status}"}
            return await resp.json()


async def analyze_repo(owner: str, repo: str) -> dict:
    """Full repository analysis."""
    # Fetch repo info, languages, contributors, recent commits in parallel
    repo_data, languages, contributors, commits, issues = await asyncio.gather(
        gh_get(f"/repos/{owner}/{repo}"),
        gh_get(f"/repos/{owner}/{repo}/languages"),
        gh_get(f"/repos/{owner}/{repo}/contributors?per_page=10"),
        gh_get(f"/repos/{owner}/{repo}/commits?per_page=10"),
        gh_get(f"/repos/{owner}/{repo}/issues?state=open&per_page=5&sort=updated"),
        return_exceptions=True,
    )

    result = {"status": "success", "repo": f"{owner}/{repo}"}

    # Repo info
    if isinstance(repo_data, dict) and "error" not in repo_data:
        result["info"] = {
            "name": repo_data.get("full_name", ""),
            "description": (repo_data.get("description", "") or "")[:200],
            "stars": repo_data.get("stargazers_count", 0),
            "forks": repo_data.get("forks_count", 0),
            "watchers": repo_data.get("subscribers_count", 0),
            "open_issues": repo_data.get("open_issues_count", 0),
            "language": repo_data.get("language", ""),
            "license": (repo_data.get("license") or {}).get("spdx_id", ""),
            "created": (repo_data.get("created_at", ""))[:10],
            "updated": (repo_data.get("updated_at", ""))[:10],
            "pushed": (repo_data.get("pushed_at", ""))[:10],
            "size_kb": repo_data.get("size", 0),
            "default_branch": repo_data.get("default_branch", "main"),
            "is_fork": repo_data.get("fork", False),
            "is_archived": repo_data.get("archived", False),
            "topics": repo_data.get("topics", [])[:10],
            "homepage": repo_data.get("homepage", ""),
        }
    elif isinstance(repo_data, dict):
        return {"status": "error", "repo": f"{owner}/{repo}", "error": repo_data.get("error")}

    # Languages
    if isinstance(languages, dict) and "error" not in languages:
        total = sum(languages.values()) or 1
        result["languages"] = {k: round(v / total * 100, 1) for k, v in languages.items()}

    # Contributors
    if isinstance(contributors, list):
        result["top_contributors"] = [
            {"login": c.get("login", ""), "contributions": c.get("contributions", 0)}
            for c in contributors[:10] if isinstance(c, dict)
        ]

    # Recent commits
    if isinstance(commits, list):
        result["recent_commits"] = []
        for c in commits[:10]:
            if not isinstance(c, dict):
                continue
            commit = c.get("commit", {})
            result["recent_commits"].append({
                "sha": c.get("sha", "")[:7],
                "message": (commit.get("message", "")).split("\n")[0][:100],
                "author": commit.get("author", {}).get("name", ""),
                "date": (commit.get("author", {}).get("date", ""))[:10],
            })

    # Open issues/PRs
    if isinstance(issues, list):
        result["recent_issues"] = []
        for i in issues[:5]:
            if not isinstance(i, dict):
                continue
            result["recent_issues"].append({
                "number": i.get("number", 0),
                "title": (i.get("title", ""))[:80],
                "state": i.get("state", ""),
                "is_pr": "pull_request" in i,
                "created": (i.get("created_at", ""))[:10],
                "labels": [l.get("name", "") for l in i.get("labels", [])[:5]],
            })

    result["analyzed_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return result


async def analyze_user(username: str) -> dict:
    """GitHub user/org profile."""
    user_data = await gh_get(f"/users/{username}")
    if isinstance(user_data, dict) and "error" in user_data:
        return {"status": "error", "error": user_data["error"]}

    repos_data = await gh_get(f"/users/{username}/repos?sort=stars&per_page=10")

    result = {
        "status": "success",
        "user": username,
        "profile": {
            "name": user_data.get("name", ""),
            "bio": (user_data.get("bio", "") or "")[:200],
            "type": user_data.get("type", ""),
            "company": user_data.get("company", ""),
            "location": user_data.get("location", ""),
            "public_repos": user_data.get("public_repos", 0),
            "followers": user_data.get("followers", 0),
            "following": user_data.get("following", 0),
            "created": (user_data.get("created_at", ""))[:10],
        },
    }

    if isinstance(repos_data, list):
        result["top_repos"] = [
            {
                "name": r.get("full_name", ""),
                "description": (r.get("description", "") or "")[:100],
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "language": r.get("language", ""),
            }
            for r in repos_data[:10] if isinstance(r, dict)
        ]

    return result


def extract_repo_info(task_data: dict) -> dict:
    """Extract owner/repo or username from task."""
    input_data = task_data.get("input_data", {})
    info = {}

    if isinstance(input_data, dict):
        repo = input_data.get("repo", "") or input_data.get("repository", "")
        user = input_data.get("user", "") or input_data.get("username", "")
        if repo:
            parts = repo.strip().split("/")
            if len(parts) >= 2:
                info["owner"] = parts[-2]
                info["repo"] = parts[-1]
        if user:
            info["username"] = user.strip()

    desc = task_data.get("description", "")

    # Match github.com URLs
    gh_urls = re.findall(r'github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)', desc)
    if gh_urls and "owner" not in info:
        info["owner"] = gh_urls[0][0]
        info["repo"] = gh_urls[0][1].rstrip(".,;:")

    # Match owner/repo pattern
    if "owner" not in info:
        repos = re.findall(r'\b([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)\b', desc)
        for owner, repo in repos:
            if owner.lower() not in ("http", "https", "api", "www", "v1", "v2", "v3"):
                info["owner"] = owner
                info["repo"] = repo
                break

    # Match username mentions
    if "owner" not in info and "username" not in info:
        users = re.findall(r'@([a-zA-Z0-9_-]+)', desc)
        if users:
            info["username"] = users[0]

    return info


async def process_task(task: dict) -> dict:
    task_data = task.get("task", {})
    info = extract_repo_info(task_data)

    if "owner" in info and "repo" in info:
        return await analyze_repo(info["owner"], info["repo"])
    elif "username" in info:
        return await analyze_user(info["username"])
    else:
        return {"error": "No GitHub repo (owner/repo) or username found in task"}


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
                        output = {"error": "GitHub lookup timed out (30s)"}
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
