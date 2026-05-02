"""Identity resolution: Plex GUIDs → external IDs (tmdb / tvdb / imdb).

Plex stores GUIDs in several formats depending on agent:
  - New agent: array of `tmdb://12345`, `tvdb://67890`, `imdb://tt0001`
  - Legacy: a single `guid` like `com.plexapp.agents.themoviedb://12345?lang=en`
  - Hama / others: `com.plexapp.agents.hama://anidb-1234?...`

We parse all known forms into an `ExternalIds` triple. Anything we can't
parse becomes a None and is reported as unresolved.

Also handles requester ↔ Tautulli user matching (PLAN.md §5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from dms.models import RequesterUser, TautulliLibraryItem, TautulliUser

_GUID_RX = re.compile(r"(?P<scheme>tmdb|tvdb|imdb)://(?P<value>[^?#]+)", re.IGNORECASE)
_LEGACY_AGENT_RX = re.compile(
    r"com\.plexapp\.agents\.(?P<agent>themoviedb|thetvdb|imdb|hama)://(?P<value>[^?#]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExternalIds:
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None

    @property
    def empty(self) -> bool:
        return self.tmdb_id is None and self.tvdb_id is None and self.imdb_id is None


def _to_int(raw: str) -> int | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def parse_guid(guid: str) -> ExternalIds:
    """Parse a single Plex guid string into external IDs."""
    if not guid:
        return ExternalIds()
    m = _GUID_RX.search(guid)
    if m:
        scheme = m.group("scheme").lower()
        value = m.group("value").strip()
        if scheme == "tmdb":
            return ExternalIds(tmdb_id=_to_int(value))
        if scheme == "tvdb":
            return ExternalIds(tvdb_id=_to_int(value))
        if scheme == "imdb":
            return ExternalIds(imdb_id=value if value.startswith("tt") else f"tt{value}")

    m = _LEGACY_AGENT_RX.search(guid)
    if m:
        agent = m.group("agent").lower()
        value = m.group("value").strip()
        if agent == "themoviedb":
            return ExternalIds(tmdb_id=_to_int(value))
        if agent == "thetvdb":
            return ExternalIds(tvdb_id=_to_int(value))
        if agent == "imdb":
            return ExternalIds(imdb_id=value if value.startswith("tt") else f"tt{value}")
        # hama and other anime agents — out of scope for spike
    return ExternalIds()


def parse_guids(guids: Iterable[str]) -> ExternalIds:
    """Merge multiple GUID strings into a single ExternalIds, first wins per field."""
    tmdb: int | None = None
    tvdb: int | None = None
    imdb: str | None = None
    for g in guids:
        ids = parse_guid(g)
        tmdb = tmdb or ids.tmdb_id
        tvdb = tvdb or ids.tvdb_id
        imdb = imdb or ids.imdb_id
        if tmdb and tvdb and imdb:
            break
    return ExternalIds(tmdb_id=tmdb, tvdb_id=tvdb, imdb_id=imdb)


def resolve_plex_item(item: TautulliLibraryItem) -> ExternalIds:
    """Resolve a Tautulli library item to external IDs."""
    sources: list[str] = []
    if item.guid:
        sources.append(item.guid)
    if item.guids:
        sources.extend(item.guids)
    return parse_guids(sources)


# ---------- Requester ↔ Tautulli user mapping ----------

MatchMethod = Literal["api", "name", "manual", "self", "unresolved"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class UserMapping:
    requester_id: int | None
    requester_name: str | None
    tautulli_user_id: int | None
    tautulli_user_name: str | None
    method: MatchMethod
    confidence: Confidence


def map_requesters_to_tautulli(
    requesters: list[RequesterUser],
    tautulli_users: list[TautulliUser],
) -> list[UserMapping]:
    """Match request-service users to Tautulli users.

    Strategy:
      1. plex_username (if present) == tautulli username/friendly_name → high (api)
      2. case-insensitive name match → medium (name)
      3. unresolved
    """
    by_username = {(u.username or "").lower(): u for u in tautulli_users if u.username}
    by_friendly = {(u.friendly_name or "").lower(): u for u in tautulli_users if u.friendly_name}

    mappings: list[UserMapping] = []
    for req in requesters:
        plex_name = (req.plex_username or "").lower()
        if plex_name and plex_name in by_username:
            taut = by_username[plex_name]
            mappings.append(_mapping(req, taut, "api", "high"))
            continue

        req_name = (req.display_name or req.username or "").lower()
        match = by_username.get(req_name) or by_friendly.get(req_name)
        if match:
            mappings.append(_mapping(req, match, "name", "medium"))
            continue

        mappings.append(
            UserMapping(
                requester_id=req.id,
                requester_name=req.display_name or req.username,
                tautulli_user_id=None,
                tautulli_user_name=None,
                method="unresolved",
                confidence="low",
            )
        )
    return mappings


def _mapping(
    req: RequesterUser, taut: TautulliUser, method: MatchMethod, confidence: Confidence
) -> UserMapping:
    return UserMapping(
        requester_id=req.id,
        requester_name=req.display_name or req.username,
        tautulli_user_id=taut.user_id,
        tautulli_user_name=taut.username or taut.friendly_name,
        method=method,
        confidence=confidence,
    )
