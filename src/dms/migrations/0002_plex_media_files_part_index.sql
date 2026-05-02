-- Multi-part media support: a single Plex item can have several files
-- (split parts, multi-disc rips). The 0001 schema's UNIQUE(plex_item_id,
-- rating_key) collapsed parts into one row, weakening size accounting.
-- Add a `part_index` column and include it in the unique key.
--
-- SQLite can't ALTER a UNIQUE constraint in place, so we use the standard
-- table-recreate pattern: build _new with the desired schema, copy rows,
-- drop old, rename new. Foreign keys are disabled during the swap so the
-- candidates.plex_media_file_id reference doesn't transiently violate; it
-- re-attaches to the renamed table because we preserve `id` values.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS plex_media_files_new;

CREATE TABLE plex_media_files_new (
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
    part_index            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (plex_item_id, rating_key, part_index)
);

INSERT INTO plex_media_files_new
  (id, plex_item_id, rating_key, file_path, size_bytes, container,
   video_resolution, video_codec, last_seen_sync_run_id, deleted_at, part_index)
SELECT id, plex_item_id, rating_key, file_path, size_bytes, container,
       video_resolution, video_codec, last_seen_sync_run_id, deleted_at, 0
FROM plex_media_files;

DROP TABLE plex_media_files;
ALTER TABLE plex_media_files_new RENAME TO plex_media_files;

CREATE INDEX IF NOT EXISTS idx_plex_media_files_item ON plex_media_files(plex_item_id);
CREATE INDEX IF NOT EXISTS idx_plex_media_files_path ON plex_media_files(file_path);

PRAGMA foreign_keys = ON;
