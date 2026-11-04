"""Pre-seeding and JWT token cache manager for virtual benchmark users."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
import httpx

from benchmarks.config import BenchmarkConfig

logger = logging.getLogger("benchmarks.seeder")


class Seeder:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.cache_path = config.resolve_token_cache_path()

    async def ensure_users_and_tokens(self, count: int | None = None) -> list[str]:
        """Ensure `count` users are registered and return their valid JWT Bearer tokens."""
        needed_count = count or self.config.user_pool_size
        cached_tokens = self._load_cache()

        # Check if cached tokens are still valid against target API
        if len(cached_tokens) >= needed_count:
            test_token = cached_tokens[0]
            if await self._is_token_valid(test_token):
                logger.info("Reusing %d valid pre-seeded tokens from cache.", needed_count)
                return cached_tokens[:needed_count]
            logger.warning("Cached tokens appear expired or invalid. Re-authenticating...")

        logger.info("Pre-seeding %d virtual users on %s...", needed_count, self.config.target_url)
        tokens: list[str] = []
        semaphore = asyncio.Semaphore(15)

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0) as client:
            tasks = [
                self._ensure_single_user(client, i, semaphore)
                for i in range(1, needed_count + 1)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, str):
                tokens.append(res)
            elif isinstance(res, Exception):
                logger.error("Failed to seed user: %s", res)

        if len(tokens) < needed_count:
            raise RuntimeError(f"Expected {needed_count} seeded users, but only got {len(tokens)}.")

        self._save_cache(tokens)
        logger.info("Successfully seeded %d users and updated token cache.", len(tokens))
        return tokens

    async def resolve_assignment_id(self, auth_token: str) -> str:
        """Fetch available assignments and select the optimal benchmark target."""
        if self.config.assignment_id:
            return self.config.assignment_id

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=10.0) as client:
            resp = await client.get(
                "/api/assignments",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            resp.raise_for_status()
            assignments: list[dict[str, Any]] = resp.json()

        if not assignments:
            raise RuntimeError("No assignments found in SWCP database.")

        # Prefer 'C sandbox smoke test' (empty test cases = pure compilation & execution)
        for a in assignments:
            title = a.get("title", "").lower()
            if "smoke" in title or "smoke test" in title:
                return str(a["id"])

        # Fallback to the first available assignment
        selected_id = str(assignments[0]["id"])
        logger.info("Auto-selected assignment: %s (%s)", selected_id, assignments[0].get("title"))
        return selected_id

    async def _ensure_single_user(
        self, client: httpx.AsyncClient, user_num: int, semaphore: asyncio.Semaphore
    ) -> str:
        username = f"{self.config.user_prefix}{user_num:04d}"
        password = self.config.password

        async with semaphore:
            # 1. Register user (ignore 409 Conflict if user already exists)
            try:
                reg_resp = await client.post(
                    "/api/auth/register",
                    json={"username": username, "password": password},
                )
                if reg_resp.status_code not in (201, 409):
                    reg_resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 409:
                    raise

            # 2. Login to get JWT
            login_resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            login_resp.raise_for_status()
            data = login_resp.json()
            return str(data.get("accessToken") or data.get("token"))

    async def _is_token_valid(self, token: str) -> bool:
        try:
            # Check JWT expiry header/payload: require at least 30 minutes of remaining lifetime
            parts = token.split(".")
            if len(parts) == 3:
                padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
                exp = payload.get("exp")
                if exp is not None and (exp - time.time()) < 1800:
                    logger.info("Cached JWT token expires within 30 minutes (exp=%s). Treating as expired.", exp)
                    return False

            async with httpx.AsyncClient(base_url=self.config.target_url, timeout=5.0) as client:
                resp = await client.get(
                    "/api/assignments",
                    headers={"Authorization": f"Bearer {token}"},
                )
                return resp.status_code == 200
        except Exception:
            return False

    def _load_cache(self) -> list[str]:
        if not self.cache_path.exists():
            return []
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("target_url") == self.config.target_url:
                    return data.get("tokens", [])
        except Exception as e:
            logger.warning("Could not read token cache: %s", e)
        return []

    def _save_cache(self, tokens: list[str]) -> None:
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "target_url": self.config.target_url,
                        "tokens": tokens,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            logger.warning("Could not write token cache: %s", e)
