-- Phish setlist predictor schema. Portable SQL (SQLite now, Postgres later).

CREATE TABLE IF NOT EXISTS venues (
  venueid    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  city       TEXT,
  state      TEXT,
  country    TEXT,
  alias      INTEGER DEFAULT 0        -- canonical venueid for renamed venues; 0 = self
);

CREATE TABLE IF NOT EXISTS shows (
  showid     INTEGER PRIMARY KEY,
  showdate   TEXT NOT NULL,            -- ISO yyyy-mm-dd
  venueid    INTEGER REFERENCES venues(venueid),
  tourid     INTEGER,
  tour_name  TEXT,
  artistid   INTEGER,
  exclude    INTEGER NOT NULL DEFAULT 0,  -- 1 for cancelled/soundcheck/anomalous
  show_index INTEGER                    -- chronological ordinal over non-excluded past Phish shows
);
CREATE INDEX IF NOT EXISTS idx_shows_date ON shows(showdate);
CREATE INDEX IF NOT EXISTS idx_shows_venue ON shows(venueid);

CREATE TABLE IF NOT EXISTS songs (
  songid       INTEGER PRIMARY KEY,
  slug         TEXT UNIQUE NOT NULL,   -- canonical identity
  name         TEXT NOT NULL,
  is_original  INTEGER,                -- 1 original, 0 cover, NULL unknown
  debut_date   TEXT,
  times_played INTEGER                 -- phish.net lifetime count, sanity checks only
);

CREATE TABLE IF NOT EXISTS performances (
  showid     INTEGER NOT NULL REFERENCES shows(showid),
  songid     INTEGER NOT NULL REFERENCES songs(songid),
  set_label  TEXT,                     -- '1','2','3','e','e2', ...
  position   INTEGER NOT NULL,         -- ordinal within show
  gap        INTEGER,                  -- from API, as-of-now; cross-check only, never a feature
  trans_mark TEXT,
  PRIMARY KEY (showid, songid, position)
);
CREATE INDEX IF NOT EXISTS idx_perf_song ON performances(songid);
CREATE INDEX IF NOT EXISTS idx_perf_show ON performances(showid);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
