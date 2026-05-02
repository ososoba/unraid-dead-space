"""Config loader: numbered Arr instances + invalid value handling."""

from __future__ import annotations

import pytest

from dms import config as cfg_module


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every env var the loader inspects."""
    keys = [
        "WATCH_SCOPE",
        "SERIES_SPECIALS_MODE",
        "TAUTULLI_URL",
        "TAUTULLI_API_KEY",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for kind in ("SONARR", "RADARR"):
        for i in range(1, 11):
            for suffix in ("NAME", "URL", "API_KEY"):
                monkeypatch.delenv(f"{kind}_{i}_{suffix}", raising=False)
    for i in range(1, 11):
        for suffix in ("SOURCE", "NAME", "URL", "API_KEY"):
            monkeypatch.delenv(f"REQUESTER_{i}_{suffix}", raising=False)
    return monkeypatch


def test_loads_numbered_arr_instances(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SONARR_1_URL", "http://s1:8989")
    clean_env.setenv("SONARR_1_API_KEY", "k1")
    clean_env.setenv("SONARR_2_URL", "http://s2:8989")
    clean_env.setenv("SONARR_2_API_KEY", "k2")
    clean_env.setenv("RADARR_1_URL", "http://r1:7878/")  # trailing slash stripped
    clean_env.setenv("RADARR_1_API_KEY", "k3")

    cfg = cfg_module.load_config()
    slugs = {i.slug for i in cfg.arr_instances}
    assert slugs == {"sonarr-1", "sonarr-2", "radarr-1"}
    radarr = next(i for i in cfg.arr_instances if i.slug == "radarr-1")
    assert radarr.url == "http://r1:7878"  # no trailing slash


def test_skips_partial_instance(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SONARR_1_URL", "http://s1:8989")
    # Missing API key — should be skipped entirely
    cfg = cfg_module.load_config()
    assert cfg.arr_instances == []


def test_invalid_requester_source_raises(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REQUESTER_1_SOURCE", "garbage")
    clean_env.setenv("REQUESTER_1_URL", "http://x")
    clean_env.setenv("REQUESTER_1_API_KEY", "k")
    with pytest.raises(ValueError, match="REQUESTER_1_SOURCE"):
        cfg_module.load_config()


def test_requester_none_skipped_silently(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REQUESTER_1_SOURCE", "none")
    cfg = cfg_module.load_config()
    assert cfg.requester_instances == []


def test_loads_multiple_requester_instances(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REQUESTER_1_SOURCE", "overseerr")
    clean_env.setenv("REQUESTER_1_NAME", "Overseerr 1080p")
    clean_env.setenv("REQUESTER_1_URL", "http://o1:5055/")
    clean_env.setenv("REQUESTER_1_API_KEY", "k1")
    clean_env.setenv("REQUESTER_2_SOURCE", "seerr")
    clean_env.setenv("REQUESTER_2_NAME", "Seerr 4K")
    clean_env.setenv("REQUESTER_2_URL", "http://s1:5055")
    clean_env.setenv("REQUESTER_2_API_KEY", "k2")

    cfg = cfg_module.load_config()
    assert len(cfg.requester_instances) == 2
    slugs = {r.slug for r in cfg.requester_instances}
    assert slugs == {"overseerr-1", "seerr-2"}
    o = next(r for r in cfg.requester_instances if r.slug == "overseerr-1")
    assert o.url == "http://o1:5055"  # trailing slash stripped
    assert o.name == "Overseerr 1080p"


def test_requester_partial_config_skipped(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REQUESTER_1_SOURCE", "overseerr")
    clean_env.setenv("REQUESTER_1_URL", "http://x")
    # No API key — skip silently like Arr instances
    cfg = cfg_module.load_config()
    assert cfg.requester_instances == []


def test_invalid_watch_scope_raises(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("WATCH_SCOPE", "everyone")
    with pytest.raises(ValueError, match="WATCH_SCOPE"):
        cfg_module.load_config()
