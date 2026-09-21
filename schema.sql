-- Sieve schema. Single-file SQLite, WAL mode. Designed to stay small enough to
-- sit on a Raspberry Pi: the only unbounded tables are `videos` and
-- `impressions`, both of which are pruned by `sieve prune`.

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    author_id     TEXT NOT NULL DEFAULT '',
    published     INTEGER NOT NULL DEFAULT 0,
    duration      INTEGER NOT NULL DEFAULT 0,
    views         INTEGER NOT NULL DEFAULT 0,
    likes         INTEGER NOT NULL DEFAULT 0,
    description   TEXT NOT NULL DEFAULT '',
    keywords      TEXT NOT NULL DEFAULT '[]',
    genre         TEXT NOT NULL DEFAULT '',
    is_live       INTEGER NOT NULL DEFAULT 0,
    is_upcoming   INTEGER NOT NULL DEFAULT 0,
    family_safe   INTEGER NOT NULL DEFAULT 1,
    sub_count     INTEGER NOT NULL DEFAULT 0,
    caption_langs TEXT NOT NULL DEFAULT '[]',
    transcript    TEXT,
    -- set only by the optional vision module; -1 means never scored
    nsfw_vision   REAL NOT NULL DEFAULT -1,
    fetched_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_author ON videos(author_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published DESC);

-- One row per video per scorer version. Recomputed lazily when SCORER_VERSION
-- changes so an upgrade never requires a full re-scan.
CREATE TABLE IF NOT EXISTS scores (
    video_id    TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    education        REAL NOT NULL DEFAULT 50,
    entertainment    REAL NOT NULL DEFAULT 50,
    stimulation      REAL NOT NULL DEFAULT 50,
    brainrot         REAL NOT NULL DEFAULT 50,
    clickbait        REAL NOT NULL DEFAULT 50,
    info_density     REAL NOT NULL DEFAULT 50,
    technical_depth  REAL NOT NULL DEFAULT 50,
    production       REAL NOT NULL DEFAULT 50,
    ai_generated     REAL NOT NULL DEFAULT 50,
    nsfw             REAL NOT NULL DEFAULT 0,
    music            REAL NOT NULL DEFAULT 0,
    profanity        REAL NOT NULL DEFAULT 0,
    topics      TEXT NOT NULL DEFAULT '[]',
    vector      TEXT NOT NULL DEFAULT '{}',
    signals     TEXT NOT NULL DEFAULT '{}',
    computed_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    subs         INTEGER NOT NULL DEFAULT 0,
    video_count  INTEGER NOT NULL DEFAULT 0,
    avg_duration REAL NOT NULL DEFAULT 0,
    consistency  REAL NOT NULL DEFAULT 0,
    quality      REAL NOT NULL DEFAULT 50,
    education    REAL NOT NULL DEFAULT 50,
    clickbait    REAL NOT NULL DEFAULT 50,
    -- derived 0..1 affinity recomputed from watch time, completion and recency
    affinity     REAL NOT NULL DEFAULT 0,
    watch_seconds INTEGER NOT NULL DEFAULT 0,
    watch_count   INTEGER NOT NULL DEFAULT 0,
    completion   REAL NOT NULL DEFAULT 0,
    last_watched INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0
);

-- Explicit, user-owned channel policy. Separate from `channels` so that
-- recomputing derived stats can never clobber a manual decision.
CREATE TABLE IF NOT EXISTS channel_prefs (
    channel_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    -- -5..+5. Positive boosts, negative buries, 0 leaves it to the algorithm.
    priority   INTEGER NOT NULL DEFAULT 0,
    -- neutral | allow (whitelist) | block (blacklist)
    listing    TEXT NOT NULL DEFAULT 'neutral',
    -- when listing='allow', optionally exempt this channel from soft filters
    exempt_filters INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channel_prefs_listing ON channel_prefs(listing);

CREATE TABLE IF NOT EXISTS subscriptions (
    channel_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    weight     REAL NOT NULL DEFAULT 1.0,
    added_at   INTEGER NOT NULL DEFAULT 0
);

-- Watch events. Multiple rows per video are expected (rewatches are signal).
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL,
    watched_at INTEGER NOT NULL,
    progress   REAL NOT NULL DEFAULT 0,      -- 0..1 fraction of duration reached
    dwell      INTEGER NOT NULL DEFAULT 0,   -- seconds actually in the player
    origin     TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_history_video ON history(video_id);
CREATE INDEX IF NOT EXISTS idx_history_time ON history(watched_at DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,   -- more | less | shorter | longer | deeper | lighter | higher_quality | block_channel
    note       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_video ON feedback(video_id);

-- The editable interest graph shown in the debugger.
CREATE TABLE IF NOT EXISTS interests (
    tag        TEXT PRIMARY KEY,
    weight     REAL NOT NULL DEFAULT 0,   -- -1..1, negative = suppress
    confidence REAL NOT NULL DEFAULT 0,   -- 0..1
    origin     TEXT NOT NULL DEFAULT 'derived',  -- derived | manual | llm | imported
    pinned     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_profiles (
    name       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    video_ids  TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL DEFAULT 0
);

-- Every slot we ever rendered, with its explanation. Powers the debugger,
-- daily caps and the "you have seen this 4 times and never clicked" signal.
CREATE TABLE IF NOT EXISTS impressions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id  TEXT NOT NULL,
    shown_at  INTEGER NOT NULL,
    slot      INTEGER NOT NULL DEFAULT 0,
    score     REAL NOT NULL DEFAULT 0,
    bucket    TEXT NOT NULL DEFAULT '',
    reason    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_impressions_time ON impressions(shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_impressions_video ON impressions(video_id);

CREATE TABLE IF NOT EXISTS blocklist (
    kind       TEXT NOT NULL,   -- channel | video | term
    value      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS model (
    name       TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0
);

-- Crowd-sourced titles and thumbnails from DeArrow (sponsor.ajay.app).
-- Fetched by 4-character sha256 prefix, so the upstream server never learns
-- which video the user is actually looking at.
CREATE TABLE IF NOT EXISTS dearrow (
    video_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    title_votes INTEGER NOT NULL DEFAULT 0,
    title_locked INTEGER NOT NULL DEFAULT 0,
    thumb_time  REAL,
    thumb_original INTEGER NOT NULL DEFAULT 0,
    fetched_at  INTEGER NOT NULL DEFAULT 0
);

-- SponsorBlock segments, likewise fetched by hash prefix. Used both for
-- playback skipping and as a ranking signal (sponsor load, filler ratio).
CREATE TABLE IF NOT EXISTS sponsor_segments (
    video_id   TEXT PRIMARY KEY,
    segments   TEXT NOT NULL DEFAULT '[]',
    sponsor_ratio   REAL NOT NULL DEFAULT 0,
    filler_ratio    REAL NOT NULL DEFAULT 0,
    selfpromo_ratio REAL NOT NULL DEFAULT 0,
    has_exclusive_access INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL DEFAULT 0
);

-- Hash prefixes we have already pulled, so a cold homepage costs ~24 requests
-- instead of one per video.
CREATE TABLE IF NOT EXISTS hash_prefix_log (
    service    TEXT NOT NULL,
    prefix     TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (service, prefix)
);

CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expiry ON http_cache(expires_at);
