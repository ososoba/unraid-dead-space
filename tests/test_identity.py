"""GUID parsing + requester→Tautulli mapping tests. No network."""

from __future__ import annotations

from dms.identity import (
    ExternalIds,
    map_requesters_to_tautulli,
    parse_guid,
    parse_guids,
)
from dms.models import RequesterUser, TautulliUser


class TestParseGuid:
    def test_modern_tmdb(self) -> None:
        assert parse_guid("tmdb://12345") == ExternalIds(tmdb_id=12345)

    def test_modern_tvdb(self) -> None:
        assert parse_guid("tvdb://67890") == ExternalIds(tvdb_id=67890)

    def test_modern_imdb_normalizes_tt_prefix(self) -> None:
        assert parse_guid("imdb://0133093") == ExternalIds(imdb_id="tt0133093")

    def test_modern_imdb_keeps_tt_prefix(self) -> None:
        assert parse_guid("imdb://tt0133093") == ExternalIds(imdb_id="tt0133093")

    def test_legacy_themoviedb_agent(self) -> None:
        guid = "com.plexapp.agents.themoviedb://603?lang=en"
        assert parse_guid(guid) == ExternalIds(tmdb_id=603)

    def test_legacy_thetvdb_agent(self) -> None:
        guid = "com.plexapp.agents.thetvdb://78901?lang=en"
        assert parse_guid(guid) == ExternalIds(tvdb_id=78901)

    def test_legacy_imdb_agent(self) -> None:
        guid = "com.plexapp.agents.imdb://tt0133093?lang=en"
        assert parse_guid(guid) == ExternalIds(imdb_id="tt0133093")

    def test_hama_agent_unhandled(self) -> None:
        # Anime agent — out of scope, returns empty
        assert parse_guid("com.plexapp.agents.hama://anidb-1234").empty

    def test_garbage_returns_empty(self) -> None:
        assert parse_guid("not-a-guid").empty
        assert parse_guid("").empty

    def test_uppercase_scheme(self) -> None:
        assert parse_guid("TMDB://12345") == ExternalIds(tmdb_id=12345)


class TestParseGuids:
    def test_merges_first_per_field(self) -> None:
        ids = parse_guids(["tmdb://1", "tvdb://2", "imdb://tt3"])
        assert ids == ExternalIds(tmdb_id=1, tvdb_id=2, imdb_id="tt3")

    def test_first_value_wins_per_scheme(self) -> None:
        ids = parse_guids(["tmdb://1", "tmdb://999"])
        assert ids.tmdb_id == 1

    def test_short_circuits_when_complete(self) -> None:
        ids = parse_guids(["tmdb://1", "tvdb://2", "imdb://tt3", "tmdb://will-not-overwrite"])
        assert ids.tmdb_id == 1


class TestUserMapping:
    def test_high_confidence_via_plex_username(self) -> None:
        req = RequesterUser(id=1, plexUsername="alex_p", displayName="Alex")
        taut = TautulliUser(user_id=42, username="alex_p", friendly_name="Alex P.")
        mappings = map_requesters_to_tautulli([req], [taut])
        assert len(mappings) == 1
        assert mappings[0].method == "api"
        assert mappings[0].confidence == "high"
        assert mappings[0].tautulli_user_id == 42

    def test_medium_confidence_via_name(self) -> None:
        req = RequesterUser(id=1, displayName="Moyin")
        taut = TautulliUser(user_id=7, username="moyin", friendly_name="Moyin")
        mappings = map_requesters_to_tautulli([req], [taut])
        assert mappings[0].method == "name"
        assert mappings[0].confidence == "medium"
        assert mappings[0].tautulli_user_id == 7

    def test_unresolved_when_no_match(self) -> None:
        req = RequesterUser(id=1, displayName="Stranger")
        taut = TautulliUser(user_id=7, username="moyin")
        mappings = map_requesters_to_tautulli([req], [taut])
        assert mappings[0].method == "unresolved"
        assert mappings[0].confidence == "low"
        assert mappings[0].tautulli_user_id is None

    def test_case_insensitive_name_match(self) -> None:
        req = RequesterUser(id=1, displayName="MOYIN")
        taut = TautulliUser(user_id=7, username="moyin")
        mappings = map_requesters_to_tautulli([req], [taut])
        assert mappings[0].method == "name"
