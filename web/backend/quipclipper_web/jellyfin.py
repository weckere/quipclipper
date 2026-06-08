"""Optional Jellyfin metadata client.

When ``QC_JELLYFIN_URL`` and ``QC_JELLYFIN_API_KEY`` are set, this module
queries the Jellyfin server for item metadata (title, year, poster URL) to
enrich the library browser.  When unconfigured, every call returns ``None``
and the UI falls back to folder/filename browsing.

The client is deliberately simple: it uses ``/Items`` search with a path
filter.  No write operations, no user context, read-only API key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

log = logging.getLogger(__name__)

# Timeout for Jellyfin API calls (seconds).
_TIMEOUT = 5.0


@dataclass(frozen=True)
class JellyfinMeta:
    """Subset of Jellyfin item metadata useful for the UI."""

    item_id: str
    name: str
    year: int | None = None
    overview: str | None = None
    type: str = ""  # Movie, Series, Episode, …

    def poster_url(self, base_url: str, max_width: int = 300) -> str:
        return f"{base_url.rstrip('/')}/Items/{self.item_id}/Images/Primary?maxWidth={max_width}"


class JellyfinClient:
    """Read-only Jellyfin API client."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Emby-Token": api_key}
        self._http = httpx.Client(
            base_url=self._base, headers=self._headers, timeout=_TIMEOUT
        )

    def close(self) -> None:
        self._http.close()

    @property
    def base_url(self) -> str:
        return self._base

    def search_by_path(self, path: str | Path) -> JellyfinMeta | None:
        """Find a Jellyfin item whose file path matches ``path``.

        Uses the /Items endpoint with a Path filter. Returns the first match
        or None.
        """
        try:
            resp = self._http.get(
                "/Items",
                params={
                    "Recursive": "true",
                    "Fields": "Path,Overview",
                    "EnableTotalRecordCount": "false",
                    "Limit": "1",
                    "Path": str(path),
                },
            )
            resp.raise_for_status()
            items = resp.json().get("Items", [])
            if not items:
                return None
            it = items[0]
            return JellyfinMeta(
                item_id=it["Id"],
                name=it.get("Name", ""),
                year=it.get("ProductionYear"),
                overview=it.get("Overview"),
                type=it.get("Type", ""),
            )
        except Exception as exc:
            log.debug("Jellyfin search_by_path failed for %s: %s", path, exc)
            return None

    def search_by_name(self, name: str, limit: int = 5) -> list[JellyfinMeta]:
        """Search Jellyfin items by name (for folder-level enrichment)."""
        try:
            resp = self._http.get(
                "/Items",
                params={
                    "Recursive": "true",
                    "SearchTerm": name,
                    "Fields": "Overview",
                    "IncludeItemTypes": "Movie,Series",
                    "EnableTotalRecordCount": "false",
                    "Limit": str(limit),
                },
            )
            resp.raise_for_status()
            return [
                JellyfinMeta(
                    item_id=it["Id"],
                    name=it.get("Name", ""),
                    year=it.get("ProductionYear"),
                    overview=it.get("Overview"),
                    type=it.get("Type", ""),
                )
                for it in resp.json().get("Items", [])
            ]
        except Exception as exc:
            log.debug("Jellyfin search_by_name failed for %s: %s", name, exc)
            return []

    def get_by_id(self, item_id: str) -> JellyfinMeta | None:
        """Fetch a single item by its Jellyfin id."""
        try:
            resp = self._http.get(f"/Items/{item_id}", params={"Fields": "Path,Overview"})
            resp.raise_for_status()
            it = resp.json()
            return JellyfinMeta(
                item_id=it["Id"],
                name=it.get("Name", ""),
                year=it.get("ProductionYear"),
                overview=it.get("Overview"),
                type=it.get("Type", ""),
            )
        except Exception as exc:
            log.debug("Jellyfin get_by_id failed for %s: %s", item_id, exc)
            return None
