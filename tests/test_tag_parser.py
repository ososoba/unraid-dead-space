"""Sonarr/Radarr requester tag parsing — must not crash on bad input."""

from dms.tag_parser import ParsedTag


def test_well_formed_tag() -> None:
    p = ParsedTag.parse("2 - moyin")
    assert p.requester_id == 2
    assert p.requester_name == "moyin"
    assert not p.is_unparseable


def test_extra_whitespace() -> None:
    p = ParsedTag.parse("  10  -  alex p.  ")
    assert p.requester_id == 10
    assert p.requester_name == "alex p."
    assert not p.is_unparseable


def test_missing_dash() -> None:
    p = ParsedTag.parse("2 moyin")
    assert p.is_unparseable
    assert p.raw == "2 moyin"


def test_no_id() -> None:
    p = ParsedTag.parse("moyin")
    assert p.is_unparseable


def test_empty_string() -> None:
    p = ParsedTag.parse("")
    assert p.is_unparseable


def test_none_handled_safely() -> None:
    p = ParsedTag.parse(None)  # type: ignore[arg-type]
    assert p.is_unparseable
    assert p.raw == ""


def test_multi_word_name() -> None:
    p = ParsedTag.parse("3 - alex p. (admin)")
    assert p.requester_id == 3
    assert p.requester_name == "alex p. (admin)"
