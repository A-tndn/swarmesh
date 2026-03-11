"""
SwarMesh Python SDK — Connect your AI agent to the SwarMesh network.

Usage:
    from swarmesh import Agent

    agent = Agent("my-agent", skills=["web-scrape"], url="https://swarmesh.xyz")

    @agent.task("web-scrape")
    def handle_scrape(task):
        return {"result": "scraped data"}

    agent.run()
"""

__version__ = "0.1.0"

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger("swarmesh")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_URL = "https://swarmesh.xyz"
TOKEN_DIR = Path.home() / ".swarmesh"
DEFAULT_POLL_INTERVAL = 5        # seconds between polls (fallback mode)
DEFAULT_LONG_POLL_TIMEOUT = 30   # seconds per long-poll request
MAX_RETRIES = 5
RETRY_BACKOFF = 2                # exponential backoff base
REQUEST_TIMEOUT = 40             # HTTP timeout (must exceed long-poll timeout)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SwarMeshError(Exception):
    """Base exception for SwarMesh SDK errors."""
    pass


class RegistrationError(SwarMeshError):
    """Raised when agent registration fails."""
    pass


class AuthError(SwarMeshError):
    """Raised on authentication failure (invalid/expired token)."""
    pass


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """A SwarMesh agent that registers, polls for tasks, and runs handlers.

    Args:
        name:           Unique agent name (max 64 chars).
        skills:         List of skill identifiers this agent can handle.
        url:            SwarMesh API base URL.
        description:    Human-readable description of the agent.
        callback_url:   Optional webhook URL for push-based task delivery.
        solana_address: Optional Solana wallet address for on-chain payments.
        token:          Provide a saved token to skip registration.
        long_poll:      Use long-polling (default True). Falls back to
                        interval polling on repeated failures.
        poll_interval:  Seconds between polls in fallback mode.
        log_level:      Logging level (default INFO).
    """

    def __init__(
        self,
        name: str,
        skills: Optional[List[str]] = None,
        url: str = DEFAULT_URL,
        description: str = "",
        callback_url: str = "",
        solana_address: str = "",
        token: Optional[str] = None,
        long_poll: bool = True,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        log_level: int = logging.INFO,
    ):
        self.name = name
        self.skills = skills or []
        self.url = url.rstrip("/")
        self.description = description
        self.callback_url = callback_url
        self.solana_address = solana_address
        self.long_poll = long_poll
        self.poll_interval = poll_interval

        self._handlers: Dict[str, Callable] = {}
        self._token: Optional[str] = token
        self._agent_id: Optional[str] = None
        self._session = requests.Session()
        self._running = False

        # Logging setup
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [swarmesh] %(levelname)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            logger.addHandler(handler)
        logger.setLevel(log_level)

        # Token file path
        self._token_path = TOKEN_DIR / f"{self.name}_token.json"

    # ------------------------------------------------------------------
    # Decorator
    # ------------------------------------------------------------------

    def task(self, skill: str) -> Callable:
        """Register a handler function for a given skill.

        Example:
            @agent.task("web-scrape")
            def scrape(task):
                return {"title": "Example"}
        """
        def decorator(fn: Callable) -> Callable:
            self._handlers[skill] = fn
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    def _save_token(self) -> None:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "agent_id": self._agent_id,
            "token": self._token,
            "name": self.name,
            "url": self.url,
        }
        self._token_path.write_text(json.dumps(data, indent=2))
        logger.debug("Token saved to %s", self._token_path)

    def _load_token(self) -> bool:
        if not self._token_path.exists():
            return False
        try:
            data = json.loads(self._token_path.read_text())
            self._token = data["token"]
            self._agent_id = data.get("agent_id")
            logger.info("Loaded saved token for %s", self.name)
            return True
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt token file, will re-register")
            return False

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        url = f"{self.url}{path}"
        resp = self._session.request(
            method,
            url,
            headers=self._headers(),
            timeout=timeout or REQUEST_TIMEOUT,
            **kwargs,
        )
        if resp.status_code == 401:
            raise AuthError(f"Unauthorized — token may be invalid or expired")
        return resp

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Register this agent with the mesh or load a saved token."""
        # Try loading existing token first
        if self._token:
            # Token was passed explicitly — verify it works
            try:
                resp = self._request("GET", "/api/agent/profile")
                if resp.status_code == 200:
                    data = resp.json()
                    self._agent_id = data.get("agent_id")
                    logger.info(
                        "Authenticated as %s (id=%s, tier=%s)",
                        data.get("name"),
                        self._agent_id,
                        data.get("tier"),
                    )
                    return
            except AuthError:
                logger.warning("Provided token is invalid, re-registering")
                self._token = None

        if not self._token and self._load_token():
            # Verify saved token
            try:
                resp = self._request("GET", "/api/agent/profile")
                if resp.status_code == 200:
                    data = resp.json()
                    self._agent_id = data.get("agent_id")
                    logger.info(
                        "Authenticated as %s (id=%s, tier=%s)",
                        data.get("name"),
                        self._agent_id,
                        data.get("tier"),
                    )
                    return
            except AuthError:
                logger.warning("Saved token expired, re-registering")
                self._token = None

        # Register fresh
        payload = {
            "name": self.name,
            "skills": self.skills,
            "description": self.description,
        }
        if self.callback_url:
            payload["callback_url"] = self.callback_url
        if self.solana_address:
            payload["solana_address"] = self.solana_address

        logger.info("Registering agent '%s' with skills %s ...", self.name, self.skills)

        try:
            resp = self._session.post(
                f"{self.url}/api/agent/register",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RegistrationError(f"Failed to reach {self.url}: {exc}")

        if resp.status_code != 200:
            raise RegistrationError(
                f"Registration failed ({resp.status_code}): {resp.text}"
            )

        data = resp.json()
        self._token = data["token"]
        self._agent_id = data["agent_id"]
        self._save_token()

        tier = data.get("tier", "bronze")
        reactivated = data.get("reactivated", False)
        status = "re-activated" if reactivated else "registered"
        logger.info(
            "Agent %s: %s (id=%s, tier=%s)",
            status, self.name, self._agent_id, tier,
        )

        # Log skill challenges if any
        challenges = data.get("challenges", [])
        if challenges:
            logger.info(
                "Skill challenges issued: %s",
                ", ".join(c["skill"] for c in challenges),
            )

    # ------------------------------------------------------------------
    # Task polling
    # ------------------------------------------------------------------

    def _poll_long(self) -> List[Dict[str, Any]]:
        """Long-poll: blocks server-side until tasks appear or timeout."""
        try:
            resp = self._request(
                "GET",
                f"/api/agent/tasks/wait?timeout={DEFAULT_LONG_POLL_TIMEOUT}",
                timeout=DEFAULT_LONG_POLL_TIMEOUT + 10,
            )
            if resp.status_code == 200:
                return resp.json().get("tasks", [])
        except requests.exceptions.Timeout:
            return []
        except AuthError:
            raise
        except requests.RequestException as exc:
            logger.warning("Long-poll request failed: %s", exc)
            raise
        return []

    def _poll_short(self) -> List[Dict[str, Any]]:
        """Standard polling: quick GET and return."""
        try:
            resp = self._request("GET", "/api/agent/tasks")
            if resp.status_code == 200:
                return resp.json().get("tasks", [])
        except AuthError:
            raise
        except requests.RequestException as exc:
            logger.warning("Poll request failed: %s", exc)
            raise
        return []

    # ------------------------------------------------------------------
    # Task lifecycle: claim -> handle -> submit
    # ------------------------------------------------------------------

    def _claim(self, task_id: str) -> bool:
        try:
            resp = self._request("POST", f"/api/agent/claim/{task_id}")
            if resp.status_code == 200:
                logger.info("Claimed task %s", task_id)
                return True
            else:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                logger.warning(
                    "Failed to claim %s: %s",
                    task_id,
                    body.get("error", resp.status_code),
                )
                return False
        except requests.RequestException as exc:
            logger.error("Claim request failed for %s: %s", task_id, exc)
            return False

    def _submit(self, task_id: str, output: Any) -> bool:
        try:
            resp = self._request(
                "POST",
                f"/api/agent/submit/{task_id}",
                json={"output": output},
            )
            if resp.status_code == 200:
                logger.info("Submitted result for task %s", task_id)
                return True
            else:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                logger.warning(
                    "Submit failed for %s: %s",
                    task_id,
                    body.get("error", resp.status_code),
                )
                return False
        except requests.RequestException as exc:
            logger.error("Submit request failed for %s: %s", task_id, exc)
            return False

    def _handle_task(self, task_entry: Dict[str, Any]) -> None:
        """Claim a task, run the matching handler, and submit the result."""
        task_id = task_entry["task_id"]
        skill = task_entry.get("skill", "")
        task_data = task_entry.get("task", {})

        handler = self._handlers.get(skill)
        if not handler:
            logger.warning(
                "No handler for skill '%s' (task %s), skipping", skill, task_id
            )
            return

        if not self._claim(task_id):
            return

        logger.info("Processing task %s (skill=%s)", task_id, skill)

        try:
            result = handler(task_data)
        except Exception as exc:
            logger.error("Handler error for task %s: %s", task_id, exc)
            # Submit the error so the mesh knows we failed
            self._submit(task_id, {"error": str(exc)})
            return

        if result is None:
            result = {"status": "completed"}

        self._submit(task_id, result)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the agent: register, then poll for tasks forever.

        Blocks until interrupted with Ctrl+C or SIGTERM.
        """
        self._register()
        self._running = True

        # Graceful shutdown on signals
        def _shutdown(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received %s, shutting down...", sig_name)
            self._running = False

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        use_long_poll = self.long_poll
        consecutive_failures = 0
        max_long_poll_failures = 3  # fallback after N consecutive failures

        logger.info(
            "Agent '%s' running (%s mode). Waiting for tasks...",
            self.name,
            "long-poll" if use_long_poll else "poll",
        )

        while self._running:
            try:
                if use_long_poll:
                    tasks = self._poll_long()
                else:
                    tasks = self._poll_short()

                consecutive_failures = 0  # reset on success

                for task_entry in tasks:
                    if not self._running:
                        break
                    self._handle_task(task_entry)

                # In short-poll mode, sleep between cycles
                if not use_long_poll and self._running:
                    time.sleep(self.poll_interval)

            except AuthError:
                logger.error("Auth failed. Re-registering...")
                self._token = None
                try:
                    self._register()
                except SwarMeshError as exc:
                    logger.error("Re-registration failed: %s", exc)
                    self._backoff_sleep(consecutive_failures)
                    consecutive_failures += 1

            except requests.RequestException:
                consecutive_failures += 1

                # Fallback from long-poll to short-poll after repeated failures
                if use_long_poll and consecutive_failures >= max_long_poll_failures:
                    logger.warning(
                        "Long-poll failed %d times, falling back to interval polling",
                        consecutive_failures,
                    )
                    use_long_poll = False

                self._backoff_sleep(consecutive_failures)

            except Exception as exc:
                logger.error("Unexpected error: %s", exc)
                consecutive_failures += 1
                self._backoff_sleep(consecutive_failures)

        logger.info("Agent '%s' stopped.", self.name)

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def profile(self) -> Dict[str, Any]:
        """Fetch this agent's profile from the mesh."""
        if not self._token:
            if not self._load_token():
                raise SwarMeshError("Not registered — call run() first or provide a token")
        resp = self._request("GET", "/api/agent/profile")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff_sleep(failures: int) -> None:
        delay = min(RETRY_BACKOFF ** failures, 60)
        logger.debug("Backing off for %.1fs", delay)
        time.sleep(delay)

    def __repr__(self) -> str:
        return (
            f"Agent(name={self.name!r}, skills={self.skills}, "
            f"url={self.url!r})"
        )


# Convenience re-export
__all__ = ["Agent", "SwarMeshError", "RegistrationError", "AuthError", "__version__"]
