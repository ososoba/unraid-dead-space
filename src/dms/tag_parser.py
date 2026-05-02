"""Parse Sonarr/Radarr requester tags (format `"<id> - <name>"`).

Designed to never crash on malformed tags — returns `is_unparseable=True`
with the raw value preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Format: "<id> - <name>", e.g. "2 - moyin"
_TAG_RX = re.compile(r"^\s*(?P<id>\d+)\s*-\s*(?P<name>.+?)\s*$")


@dataclass(frozen=True)
class ParsedTag:
    raw: str
    requester_id: int | None
    requester_name: str | None
    is_unparseable: bool

    @classmethod
    def parse(cls, raw: str) -> ParsedTag:
        if raw is None:
            return cls(raw="", requester_id=None, requester_name=None, is_unparseable=True)
        m = _TAG_RX.match(raw)
        if not m:
            return cls(raw=raw, requester_id=None, requester_name=None, is_unparseable=True)
        return cls(
            raw=raw,
            requester_id=int(m.group("id")),
            requester_name=m.group("name"),
            is_unparseable=False,
        )
