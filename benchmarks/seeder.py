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


def _parse_jwt_claims(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None


class Seeder:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.cache_path = config.resolve_token_cache_path()

    async def ensure_users_and_tokens(self, count: int | None = None) -> list[str]:
        """Ensure `count` users are registered and return their valid JWT Bearer tokens."""
        needed_count = count or self.config.user_pool_size
        cached_tokens = self._load_cache()
        now = time.time()

        # 1. Parse and validate all cached tokens locally
        valid_by_user: dict[int, str] = {}
        for token in cached_tokens:
            payload = _parse_jwt_claims(token)
            if not payload:
                continue
            exp = payload.get("exp")
            # Require at least 15 minutes of remaining validity
            if exp is None or (exp - now) < 900:
                continue
            uname = (
                payload.get("preferred_username")
                or payload.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
                or payload.get("unique_name")
                or payload.get("sub")
            )
            if uname and uname.startswith(self.config.user_prefix):
                try:
                    user_num = int(uname[len(self.config.user_prefix):])
                    valid_by_user[user_num] = token
                except ValueError:
                    pass

        # 2. If candidate tokens exist, probe server once to verify signing key hasn't changed
        if valid_by_user:
            probe_user = min(valid_by_user.keys())
            probe_token = valid_by_user[probe_user]
            try:
                async with httpx.AsyncClient(base_url=self.config.target_url, timeout=5.0) as client:
                    resp = await client.get(
                        "/api/assignments",
                        headers={"Authorization": f"Bearer {probe_token}"},
                    )
                    if resp.status_code in (401, 403):
                        logger.warning("Server rejected cached token (signing key changed or invalidated). Invalidating cache...")
                        valid_by_user.clear()
            except Exception as e:
                logger.debug("Probe check warning: %s", e)

        # 3. Determine which virtual users from 1..needed_count are missing or expired
        missing_users = [i for i in range(1, needed_count + 1) if i not in valid_by_user]

        if not missing_users:
            logger.info("Reusing %d valid pre-seeded tokens from cache.", needed_count)
            return [valid_by_user[i] for i in range(1, needed_count + 1)]

        logger.info(
            "Token cache: %d valid tokens found. Authenticating %d users (missing/expired) on %s...",
            len(valid_by_user), len(missing_users), self.config.target_url,
        )

        # 4. Authenticate missing users in parallel
        semaphore = asyncio.Semaphore(30)
        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0) as client:
            tasks = [
                self._ensure_single_user(client, i, semaphore)
                for i in missing_users
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for user_num, res in zip(missing_users, results):
            if isinstance(res, str):
                valid_by_user[user_num] = res
            elif isinstance(res, Exception):
                logger.error("Failed to authenticate user %d: %s", user_num, res)

        final_tokens = [valid_by_user[i] for i in range(1, needed_count + 1) if i in valid_by_user]
        if len(final_tokens) < needed_count:
            raise RuntimeError(f"Expected {needed_count} authenticated users, but only got {len(final_tokens)}.")

        # 5. Save all currently valid tokens back to disk cache sorted by user_num
        sorted_tokens = [valid_by_user[k] for k in sorted(valid_by_user.keys())]
        self._save_cache(sorted_tokens)
        logger.info("Successfully updated token cache with %d valid tokens.", len(sorted_tokens))

        return final_tokens

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
            # 1. Try login directly first to avoid 409 conflict roundtrip if user exists
            try:
                login_resp = await client.post(
                    "/api/auth/login",
                    json={"username": username, "password": password},
                )
                if login_resp.status_code == 200:
                    data = login_resp.json()
                    return str(data.get("accessToken") or data.get("token"))
            except Exception:
                pass

            # 2. Register user if not existing
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

            # 3. Login to get JWT
            login_resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            login_resp.raise_for_status()
            data = login_resp.json()
            return str(data.get("accessToken") or data.get("token"))

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
