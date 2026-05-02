"""Environment-driven configuration for the spike.

Loads numbered Sonarr/Radarr instances dynamically (SONARR_1_*, SONARR_2_*, ...).
Validation happens here; downstream code can assume well-formed config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

load_dotenv()


ArrKind = Literal["sonarr", "radarr"]
RequesterSource = Literal["overseerr", "seerr", "jellyseerr", "none"]


@dataclass(frozen=True)
class ArrInstance:
    kind: ArrKind
    index: int
    name: str
    url: str
    api_key: str

    @property
    def slug(self) -> str:
        return f"{self.kind}-{self.index}"


@dataclass(frozen=True)
class TautulliConfig:
    url: str
    api_key: str


@dataclass(frozen=True)
class RequesterConfig:
    """A single Overseerr / Seerr / Jellyseerr instance.

    Multiple instances are supported (e.g. one for 1080p, one for 4K). They are
    keyed by `index` and merged at the candidate engine level — earliest request
    per media wins for attribution regardless of instance.
    """

    index: int
    source: RequesterSource
    name: str
    url: str
    api_key: str

    @property
    def slug(self) -> str:
        return f"{self.source}-{self.index}"


@dataclass(frozen=True)
class WatchConfig:
    scope: Literal["anyone", "requester"] = "anyone"
    threshold_movies_pct: int = 80
    threshold_episodes_pct: int = 80
    specials_mode: Literal["ignore", "include"] = "ignore"
    never_watched_days: int = 90
    stale_days: int = 180


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: int = 30
    max_concurrency: int = 4
    backfill_page_size: int = 500


@dataclass
class AppConfig:
    arr_instances: list[ArrInstance] = field(default_factory=list)
    tautulli: TautulliConfig | None = None
    requester_instances: list[RequesterConfig] = field(default_factory=list)
    watch: WatchConfig = field(default_factory=WatchConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    history_retention_years: int = 10


def _env(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    return int(raw) if raw else default


def _load_arr(kind: ArrKind) -> list[ArrInstance]:
    """Load all SONARR_<n>_* / RADARR_<n>_* instances from env."""
    prefix = kind.upper()
    instances: list[ArrInstance] = []
    for index in range(1, 11):  # support up to 10 instances
        name = _env(f"{prefix}_{index}_NAME")
        url = _env(f"{prefix}_{index}_URL")
        api_key = _env(f"{prefix}_{index}_API_KEY")
        if not (url and api_key):
            continue
        instances.append(
            ArrInstance(
                kind=kind,
                index=index,
                name=name or f"{prefix} {index}",
                url=url.rstrip("/"),
                api_key=api_key,
            )
        )
    return instances


def _load_tautulli() -> TautulliConfig | None:
    url = _env("TAUTULLI_URL")
    key = _env("TAUTULLI_API_KEY")
    if not (url and key):
        return None
    return TautulliConfig(url=url.rstrip("/"), api_key=key)


_VALID_REQUESTER_SOURCES = {"overseerr", "seerr", "jellyseerr", "none"}


def _load_requesters() -> list[RequesterConfig]:
    """Load REQUESTER_<n>_* instances. Each can be a different flavor."""
    instances: list[RequesterConfig] = []
    for index in range(1, 11):
        source_raw = (_env(f"REQUESTER_{index}_SOURCE") or "").lower()
        url = _env(f"REQUESTER_{index}_URL")
        key = _env(f"REQUESTER_{index}_API_KEY")
        name = _env(f"REQUESTER_{index}_NAME")
        if not source_raw and not url and not key:
            continue  # slot empty
        if source_raw not in _VALID_REQUESTER_SOURCES:
            raise ValueError(f"Invalid REQUESTER_{index}_SOURCE: {source_raw!r}")
        if source_raw == "none":
            continue
        if not (url and key):
            continue  # partial config — skip silently like Arr
        instances.append(
            RequesterConfig(
                index=index,
                source=source_raw,  # type: ignore[arg-type]
                name=name or f"{source_raw.capitalize()} {index}",
                url=url.rstrip("/"),
                api_key=key,
            )
        )
    return instances


def _load_watch() -> WatchConfig:
    scope = (_env("WATCH_SCOPE") or "anyone").lower()
    if scope not in {"anyone", "requester"}:
        raise ValueError(f"Invalid WATCH_SCOPE: {scope!r}")
    specials = (_env("SERIES_SPECIALS_MODE") or "ignore").lower()
    if specials not in {"ignore", "include"}:
        raise ValueError(f"Invalid SERIES_SPECIALS_MODE: {specials!r}")
    return WatchConfig(
        scope=scope,  # type: ignore[arg-type]
        threshold_movies_pct=_env_int("WATCH_THRESHOLD_MOVIES_PCT", 80),
        threshold_episodes_pct=_env_int("WATCH_THRESHOLD_EPISODES_PCT", 80),
        specials_mode=specials,  # type: ignore[arg-type]
        never_watched_days=_env_int("NEVER_WATCHED_DAYS", 90),
        stale_days=_env_int("STALE_DAYS", 180),
    )


def _load_http() -> HttpConfig:
    return HttpConfig(
        timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", 30),
        max_concurrency=_env_int("SYNC_MAX_CONCURRENCY", 4),
        backfill_page_size=_env_int("BACKFILL_PAGE_SIZE", 500),
    )


def load_config() -> AppConfig:
    return AppConfig(
        arr_instances=[*_load_arr("sonarr"), *_load_arr("radarr")],
        tautulli=_load_tautulli(),
        requester_instances=_load_requesters(),
        watch=_load_watch(),
        http=_load_http(),
        history_retention_years=_env_int("HISTORY_RETENTION_YEARS", 10),
    )
