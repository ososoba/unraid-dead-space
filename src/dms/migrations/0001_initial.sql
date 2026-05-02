-- Initial schema for Dead Movies & Shows.
-- All tables and indexes use IF NOT EXISTS so the migration is idempotent.
-- See PLAN.md §5 for the canonical data model and rationale.

-- ============================================================
-- Instances (Sonarr / Radarr endpoints)
-- ============================================================
CREATE TABLE IF NOT EXISTS instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('sonarr','radarr')),
    slug            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    api_key         TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_seen_ok_at TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Arr items (movies + series)
-- ============================================================
CREATE TABLE IF NOT EXISTS arr_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id           INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL CHECK (kind IN ('movie','series')),
    arr_id                INTEGER NOT NULL,
    title                 TEXT NOT NULL,
    year                  INTEGER,
    tmdb_id               INTEGER,
    tvdb_id               INTEGER,
    imdb_id               TEXT,
    monitored             INTEGER NOT NULL DEFAULT 1,
    added_at              TEXT,
    path                  TEXT,
    root_folder           TEXT,
    quality_profile       TEXT,
    ignored_local         INTEGER NOT NULL DEFAULT 0,
    last_seen_sync_run_id INTEGER,
    deleted_at            TEXT,
    UNIQUE (instance_id, arr_id)
);
CREATE INDEX IF NOT EXISTS idx_arr_items_tmdb     ON arr_items(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_arr_items_tvdb     ON arr_items(tvdb_id);
CREATE INDEX IF NOT EXISTS idx_arr_items_imdb     ON arr_items(imdb_id);
CREATE INDEX IF NOT EXISTS idx_arr_items_kind     ON arr_items(kind);
CREATE INDEX IF NOT EXISTS idx_arr_items_deleted  ON arr_items(deleted_at);
CREATE INDEX IF NOT EXISTS idx_arr_items_instance ON arr_items(instance_id);

-- ============================================================
-- Episodes (Sonarr only — each row tied to a series arr_item)
-- ============================================================
CREATE TABLE IF NOT EXISTS arr_episodes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id              INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    arr_item_id              INTEGER NOT NULL REFERENCES arr_items(id) ON DELETE CASCADE,
    arr_episode_id           INTEGER NOT NULL,
    season_number            INTEGER NOT NULL,
    episode_number           INTEGER NOT NULL,
    absolute_episode_number  INTEGER,
    title                    TEXT,
    air_date                 TEXT,
    monitored                INTEGER NOT NULL DEFAULT 1,
    has_file                 INTEGER NOT NULL DEFAULT 0,
    arr_episode_file_id      INTEGER,
    is_special               INTEGER NOT NULL DEFAULT 0,
    last_seen_sync_run_id    INTEGER,
    deleted_at               TEXT,
    UNIQUE (instance_id, arr_episode_id)
);
CREATE INDEX IF NOT EXISTS idx_arr_episodes_item ON arr_episodes(arr_item_id);
CREATE INDEX IF NOT EXISTS idx_arr_episodes_se   ON arr_episodes(season_number, episode_number);

-- ============================================================
-- Files (movies + episodes share one table; size accounting lives here)
-- ============================================================
CREATE TABLE IF NOT EXISTS arr_files (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id           INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    arr_item_id           INTEGER NOT NULL REFERENCES arr_items(id) ON DELETE CASCADE,
    kind                  TEXT NOT NULL CHECK (kind IN ('movie','episode')),
    arr_file_id           INTEGER NOT NULL,
    arr_episode_id        INTEGER REFERENCES arr_episodes(id) ON DELETE SET NULL,
    season_number         INTEGER,
    episode_number        INTEGER,
    path                  TEXT,
    size_bytes            INTEGER NOT NULL DEFAULT 0,
    date_added            TEXT,
    quality               TEXT,
    last_seen_sync_run_id INTEGER,
    deleted_at            TEXT,
    UNIQUE (instance_id, kind, arr_file_id)
);
CREATE INDEX IF NOT EXISTS idx_arr_files_item ON arr_files(arr_item_id);
CREATE INDEX IF NOT EXISTS idx_arr_files_path ON arr_files(path);

-- ============================================================
-- Plex items (Tautulli library inventory; movies/shows/seasons/episodes)
-- ============================================================
CREATE TABLE IF NOT EXISTS plex_items (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    rating_key               INTEGER NOT NULL UNIQUE,
    parent_rating_key        INTEGER,
    grandparent_rating_key   INTEGER,
    kind                     TEXT NOT NULL CHECK (kind IN ('movie','show','season','episode')),
    title                    TEXT,
    year                     INTEGER,
    section_id               INTEGER,
    section_name             TEXT,
    tmdb_id                  INTEGER,
    tvdb_id                  INTEGER,
    imdb_id                  TEXT,
    guid                     TEXT,
    guids_json               TEXT,
    season_number            INTEGER,
    episode_number           INTEGER,
    absolute_episode_number  INTEGER,
    parent_title             TEXT,
    grandparent_title        TEXT,
    originally_available_at  TEXT,
    last_seen_sync_run_id    INTEGER,
    deleted_at               TEXT
);
CREATE INDEX IF NOT EXISTS idx_plex_items_tmdb        ON plex_items(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_plex_items_tvdb        ON plex_items(tvdb_id);
CREATE INDEX IF NOT EXISTS idx_plex_items_imdb        ON plex_items(imdb_id);
CREATE INDEX IF NOT EXISTS idx_plex_items_grandparent ON plex_items(grandparent_rating_key);
CREATE INDEX IF NOT EXISTS idx_plex_items_se          ON plex_items(season_number, episode_number);
CREATE INDEX IF NOT EXISTS idx_plex_items_kind        ON plex_items(kind);

-- ============================================================
-- Plex media files (file-level inventory for orphan detection)
-- ============================================================
CREATE TABLE IF NOT EXISTS plex_media_files (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_item_id          INTEGER NOT NULL REFERENCES plex_items(id) ON DELETE CASCADE,
    rating_key            INTEGER NOT NULL,
    file_path             TEXT,
    size_bytes            INTEGER NOT NULL DEFAULT 0,
    container             TEXT,
    video_resolution      TEXT,
    video_codec           TEXT,
    last_seen_sync_run_id INTEGER,
    deleted_at            TEXT,
    UNIQUE (plex_item_id, rating_key)
);
CREATE INDEX IF NOT EXISTS idx_plex_media_files_item ON plex_media_files(plex_item_id);
CREATE INDEX IF NOT EXISTS idx_plex_media_files_path ON plex_media_files(file_path);

-- ============================================================
-- Watch events (Tautulli history; row_id is the stable cursor key)
-- ============================================================
CREATE TABLE IF NOT EXISTS watch_events (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_row_id          INTEGER NOT NULL UNIQUE,
    rating_key             INTEGER,
    parent_rating_key      INTEGER,
    grandparent_rating_key INTEGER,
    kind                   TEXT,
    user_id                INTEGER,
    user_name              TEXT,
    season_number          INTEGER,
    episode_number         INTEGER,
    started_at             TEXT,
    stopped_at             TEXT,
    percent_complete       INTEGER,
    watched_status         REAL,
    play_duration_sec      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_watch_events_rk          ON watch_events(rating_key);
CREATE INDEX IF NOT EXISTS idx_watch_events_grandparent ON watch_events(grandparent_rating_key);
CREATE INDEX IF NOT EXISTS idx_watch_events_user        ON watch_events(user_id);
CREATE INDEX IF NOT EXISTS idx_watch_events_started     ON watch_events(started_at);

-- ============================================================
-- Tags from Sonarr / Radarr ("<id> - <name>" parsed)
-- ============================================================
CREATE TABLE IF NOT EXISTS tags (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id           INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    arr_item_id           INTEGER NOT NULL REFERENCES arr_items(id) ON DELETE CASCADE,
    raw_tag               TEXT NOT NULL,
    parsed_requester_id   INTEGER,
    parsed_requester_name TEXT,
    is_unparseable        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tags_item ON tags(arr_item_id);

-- ============================================================
-- Requests (Overseerr / Seerr / Jellyseerr / tag / manual)
-- ============================================================
CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL CHECK (source IN ('overseerr','seerr','jellyseerr','tag','manual')),
    source_request_id TEXT,
    media_kind        TEXT,
    tmdb_id           INTEGER,
    tvdb_id           INTEGER,
    requester_id      INTEGER,
    requester_name    TEXT,
    requested_at      TEXT,
    status            TEXT,
    UNIQUE (source, source_request_id)
);
CREATE INDEX IF NOT EXISTS idx_requests_tmdb ON requests(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_requests_tvdb ON requests(tvdb_id);

-- ============================================================
-- Request attribution per arr_item (denormalized; recomputed each sync)
-- ============================================================
CREATE TABLE IF NOT EXISTS request_attribution (
    arr_item_id    INTEGER PRIMARY KEY REFERENCES arr_items(id) ON DELETE CASCADE,
    requester_id   INTEGER,
    requester_name TEXT,
    source         TEXT
);

-- ============================================================
-- User identity map (request-service user ↔ Tautulli user)
-- ============================================================
CREATE TABLE IF NOT EXISTS user_identity_map (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_source    TEXT NOT NULL,
    requester_id        INTEGER,
    requester_name      TEXT,
    tautulli_user_id    INTEGER,
    tautulli_user_name  TEXT,
    plex_username       TEXT,
    match_method        TEXT NOT NULL CHECK (match_method IN ('api','name','manual','self','unresolved')),
    confidence          TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (requester_source, requester_id)
);

-- ============================================================
-- Watch state (derived per arr_item; recomputed each sync)
-- ============================================================
CREATE TABLE IF NOT EXISTS watch_state (
    arr_item_id                       INTEGER PRIMARY KEY REFERENCES arr_items(id) ON DELETE CASCADE,
    has_any_play                      INTEGER NOT NULL DEFAULT 0,
    has_requester_play                INTEGER NOT NULL DEFAULT 0,
    total_episodes                    INTEGER,
    episodes_watched_count_anyone     INTEGER,
    episodes_watched_count_requester  INTEGER,
    episode_coverage_pct_anyone       REAL,
    episode_coverage_pct_requester    REAL,
    last_played_at_anyone             TEXT,
    last_played_at_requester          TEXT,
    is_finished_anyone                INTEGER NOT NULL DEFAULT 0,
    is_finished_requester             INTEGER NOT NULL DEFAULT 0,
    requester_mapping_confidence      TEXT
);

-- ============================================================
-- Candidates (rebuilt per sync; UI reads latest computed_at_sync_run_id)
-- ============================================================
CREATE TABLE IF NOT EXISTS candidates (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    arr_item_id              INTEGER REFERENCES arr_items(id) ON DELETE CASCADE,
    plex_item_id             INTEGER REFERENCES plex_items(id) ON DELETE CASCADE,
    plex_media_file_id       INTEGER REFERENCES plex_media_files(id) ON DELETE SET NULL,
    reason                   TEXT NOT NULL,
    scope                    TEXT NOT NULL CHECK (scope IN ('anyone','requester')),
    size_bytes               INTEGER NOT NULL DEFAULT 0,
    age_days                 INTEGER,
    last_played_at           TEXT,
    confidence               TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    computed_at_sync_run_id  INTEGER NOT NULL,
    CHECK (arr_item_id IS NOT NULL OR plex_item_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_candidates_run    ON candidates(computed_at_sync_run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_reason ON candidates(reason);
CREATE INDEX IF NOT EXISTS idx_candidates_size   ON candidates(size_bytes);
CREATE INDEX IF NOT EXISTS idx_candidates_arr    ON candidates(arr_item_id);

-- ============================================================
-- Ignore rules (app-local; never modifies Sonarr/Radarr state)
-- ============================================================
CREATE TABLE IF NOT EXISTS ignore_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL CHECK (scope IN ('arr_item','tmdb','tvdb')),
    arr_item_id INTEGER REFERENCES arr_items(id) ON DELETE CASCADE,
    tmdb_id     INTEGER,
    tvdb_id     INTEGER,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT
);

-- ============================================================
-- Sync orchestration
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('full','incremental','manual')),
    status       TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','partial')),
    requested_by TEXT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    cursor_json  TEXT,
    error_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_status  ON sync_jobs(status);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_started ON sync_jobs(started_at);

CREATE TABLE IF NOT EXISTS sync_run_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    step_name     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','skipped')),
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    items_seen    INTEGER NOT NULL DEFAULT 0,
    items_changed INTEGER NOT NULL DEFAULT 0,
    error_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_run_steps_run ON sync_run_steps(run_id);

-- Sync locks: heartbeat refreshed during run; stealable when stale.
CREATE TABLE IF NOT EXISTS sync_locks (
    name         TEXT PRIMARY KEY,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    owner        TEXT NOT NULL
);

-- ============================================================
-- Audit log (ignore/unignore/purge/save_config/sync_now/etc.)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL DEFAULT (datetime('now')),
    actor        TEXT,
    action       TEXT NOT NULL,
    target       TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

-- ============================================================
-- Config (key/value; UI-editable settings override env defaults)
-- ============================================================
CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
