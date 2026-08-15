"""Thin async client for the Overseerr/Jellyseerr/Seerr REST API."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# MediaRequest.status values, per the Overseerr API spec.
STATUS_PENDING = 1
STATUS_APPROVED = 2
STATUS_DECLINED = 3

STATUS_NAMES = {
    STATUS_PENDING: "pending",
    STATUS_APPROVED: "approved",
    STATUS_DECLINED: "declined",
}


class SeerrError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SeerrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: aiohttp.ClientSession,
        timeout: float = 15.0,
    ) -> None:
        self._base = f"{base_url}/api/v1"
        self._api_key = api_key
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self._base}{path}"
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["X-Api-Key"] = self._api_key

        try:
            async with self._session.request(
                method, url, headers=headers, params=params, timeout=self._timeout
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise SeerrError(_describe(resp.status, body, path), resp.status)
                if not body:
                    return None
                try:
                    return await resp.json(content_type=None)
                except ValueError as exc:
                    raise SeerrError(
                        f"Seerr returned a non-JSON response for {path}"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise SeerrError(f"Could not reach Seerr at {url}: {exc}") from exc
        except TimeoutError as exc:
            raise SeerrError(f"Seerr timed out on {method} {path}") from exc

    # --- read-only ---------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """Public endpoint; proves the URL points at a real Seerr instance."""
        return await self._call("GET", "/status", authenticated=False)

    async def request_counts(self) -> dict[str, Any]:
        """Authenticated and side-effect free; proves the API key works."""
        return await self._call("GET", "/request/count")

    async def get_request(self, request_id: int | str) -> dict[str, Any]:
        return await self._call("GET", f"/request/{request_id}")

    async def pending_requests(self, take: int = 10) -> list[dict[str, Any]]:
        payload = await self._call(
            "GET",
            "/request",
            params={"filter": "pending", "take": take, "sort": "added"},
        )
        return payload.get("results", []) if isinstance(payload, dict) else []

    async def media_details(
        self, media_type: str, tmdb_id: int | str
    ) -> dict[str, str | None]:
        """Look up title and poster; a MediaRequest itself only carries IDs."""
        blank: dict[str, str | None] = {"title": None, "poster": None}
        if not tmdb_id:
            return blank
        path = "/movie" if media_type == "movie" else "/tv"
        try:
            details = await self._call("GET", f"{path}/{tmdb_id}")
        except SeerrError as exc:
            logger.debug("Title lookup failed for %s/%s: %s", media_type, tmdb_id, exc)
            return blank

        title = details.get("title") or details.get("name")
        date = details.get("releaseDate") or details.get("firstAirDate") or ""
        if title and date[:4].isdigit():
            title = f"{title} ({date[:4]})"
        poster = details.get("posterPath")
        return {
            "title": title,
            "poster": f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{poster}"
            if poster
            else None,
        }

    # --- mutating ----------------------------------------------------------

    async def set_request_status(
        self, request_id: int | str, decision: str
    ) -> dict[str, Any]:
        if decision not in {"approve", "decline"}:
            raise ValueError(f"invalid decision {decision!r}")
        return await self._call("POST", f"/request/{request_id}/{decision}")


def _describe(status: int, body: str, path: str) -> str:
    if status == 403:
        return (
            "Seerr rejected the API key (403). Check SEERR_API_KEY matches "
            "Settings -> General -> API Key."
        )
    if status == 404:
        return f"Seerr has no record of {path} (404)."
    snippet = body.strip()[:200]
    return f"Seerr returned HTTP {status} for {path}{': ' + snippet if snippet else ''}"
