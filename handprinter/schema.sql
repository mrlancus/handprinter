-- Handprinter SQLite schema. Version tracked in PRAGMA user_version.
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  note TEXT,
  source_type TEXT NOT NULL,
  session TEXT,
  results TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  batch_id TEXT REFERENCES batches(id),
  group_id TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  container_filename TEXT,
  page_index INTEGER,
  source_type TEXT NOT NULL CHECK (source_type IN ('paper', 'ipad')),
  session TEXT,
  captured_date TEXT,
  style_era TEXT NOT NULL DEFAULT 'current' CHECK (style_era IN ('current', 'older')),
  mime TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  dhash TEXT NOT NULL,
  ink_sig TEXT NOT NULL DEFAULT '',
  original_ext TEXT NOT NULL,
  original_path TEXT NOT NULL,
  original_w INTEGER NOT NULL,
  original_h INTEGER NOT NULL,
  exif_orientation INTEGER NOT NULL DEFAULT 1,
  working_w INTEGER NOT NULL,
  working_h INTEGER NOT NULL,
  processing TEXT NOT NULL DEFAULT '{}',
  processing_rev INTEGER NOT NULL DEFAULT 0,
  processed_w INTEGER NOT NULL,
  processed_h INTEGER NOT NULL,
  quality_flags TEXT NOT NULL DEFAULT '[]',
  near_duplicates TEXT NOT NULL DEFAULT '[]',
  detection_status TEXT NOT NULL DEFAULT 'none',
  created_at TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sources_sha ON sources(sha256);
CREATE INDEX IF NOT EXISTS idx_sources_created ON sources(created_at);
CREATE INDEX IF NOT EXISTS idx_sources_session ON sources(session);

CREATE TABLE IF NOT EXISTS regions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  parent_id TEXT REFERENCES regions(id),
  kind TEXT NOT NULL CHECK (kind IN ('line', 'word', 'block', 'mark')),
  cls TEXT NOT NULL DEFAULT 'handwriting',
  frame TEXT NOT NULL DEFAULT 'processed',
  frame_w INTEGER NOT NULL,
  frame_h INTEGER NOT NULL,
  processing_rev INTEGER NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'unprocessed'
    CHECK (status IN ('unprocessed', 'proposed', 'needs_review', 'approved', 'excluded')),
  deferred INTEGER NOT NULL DEFAULT 0,
  text TEXT,
  text_origin TEXT CHECK (text_origin IN ('machine', 'human')),
  flags TEXT NOT NULL DEFAULT '[]',
  resolved_flags TEXT NOT NULL DEFAULT '[]',
  review_reason TEXT,
  other_author INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  approved_revision INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_regions_source ON regions(source_id, deleted, y);
CREATE INDEX IF NOT EXISTS idx_regions_status ON regions(status, deleted);
CREATE INDEX IF NOT EXISTS idx_regions_parent ON regions(parent_id);

CREATE TABLE IF NOT EXISTS region_revisions (
  region_id TEXT NOT NULL REFERENCES regions(id),
  revision INTEGER NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('human', 'machine', 'detector', 'system')),
  action TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (region_id, revision)
);

CREATE TABLE IF NOT EXISTS links (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('mark_target', 'before_after')),
  a_id TEXT NOT NULL REFERENCES regions(id),
  b_id TEXT REFERENCES regions(id),
  span_start INTEGER,
  span_end INTEGER,
  unknown_target INTEGER NOT NULL DEFAULT 0,
  unknown_original INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_a ON links(a_id);
CREATE INDEX IF NOT EXISTS idx_links_b ON links(b_id);

CREATE TABLE IF NOT EXISTS recognition_packages (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  model_configured TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  item_count INTEGER NOT NULL DEFAULT 0,
  total_pixels INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recognition_items (
  package_id TEXT NOT NULL REFERENCES recognition_packages(id),
  item_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  region_id TEXT,
  context_type TEXT NOT NULL,
  image_sha256 TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed')),
  error TEXT,
  proposal_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (package_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_recitems_cache ON recognition_items(cache_key, status);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  region_id TEXT,
  source_id TEXT NOT NULL,
  package_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  model_reported TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  image_sha256 TEXT NOT NULL,
  region_revision INTEGER,
  payload TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 0,
  stale INTEGER NOT NULL DEFAULT 0,
  cached INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_region ON proposals(region_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  lane TEXT NOT NULL DEFAULT 'general' CHECK (lane IN ('general', 'gpu')),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
  params TEXT NOT NULL DEFAULT '{}',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT,
  result TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  worker_pid INTEGER,
  heartbeat_at REAL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, lane, created_at);

CREATE TABLE IF NOT EXISTS snapshots (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  policy TEXT NOT NULL,
  filters TEXT NOT NULL,
  split_config TEXT NOT NULL,
  counts TEXT NOT NULL,
  warnings TEXT NOT NULL DEFAULT '[]',
  manifest_sha256 TEXT NOT NULL,
  path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
  region_id TEXT NOT NULL,
  region_revision INTEGER NOT NULL,
  kind TEXT NOT NULL,
  split TEXT NOT NULL,
  unseen INTEGER NOT NULL DEFAULT 0,
  crop_sha256 TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, region_id)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  adapter TEXT NOT NULL,
  kind TEXT NOT NULL,
  job_id TEXT,
  snapshot_id TEXT,
  seed INTEGER,
  checkpoint TEXT,
  code_version TEXT,
  config TEXT NOT NULL DEFAULT '{}',
  environment TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  duration_s REAL,
  peak_memory_mb REAL,
  error TEXT,
  outputs_dir TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  adapter TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  level TEXT NOT NULL,
  variation INTEGER NOT NULL DEFAULT 0,
  seed INTEGER,
  image_path TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generations_prompt ON generations(prompt_text, level);

CREATE TABLE IF NOT EXISTS ab_trials (
  id TEXT PRIMARY KEY,
  prompt_text TEXT NOT NULL,
  level TEXT NOT NULL,
  left_id TEXT NOT NULL REFERENCES generations(id),
  right_id TEXT NOT NULL REFERENCES generations(id),
  choice TEXT CHECK (choice IN ('left', 'right', 'tie', 'neither')),
  revealed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  rated_at TEXT
);

CREATE TABLE IF NOT EXISTS studio_assets (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('upload', 'region', 'generation')),
  ref TEXT,
  label TEXT,
  path TEXT NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compositions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  data TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS style_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
  region_ids TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Canonical split: once a page is assigned, it keeps its split in every later snapshot.
CREATE TABLE IF NOT EXISTS source_splits (
  source_id TEXT PRIMARY KEY REFERENCES sources(id),
  split TEXT NOT NULL CHECK (split IN ('train', 'val', 'test')),
  assigned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regions_source_status ON regions(source_id, status);

-- Writing sheets: text chosen by the app and written out by hand (labels known, no recognition).
CREATE TABLE IF NOT EXISTS sheets (
  id TEXT PRIMARY KEY,
  number INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  focus TEXT NOT NULL,
  seed INTEGER NOT NULL,
  lines TEXT NOT NULL            -- JSON list of the lines to write, in order
);

CREATE TABLE IF NOT EXISTS sheet_pages (
  source_id TEXT PRIMARY KEY REFERENCES sources(id),
  sheet_id TEXT NOT NULL REFERENCES sheets(id),
  applied_at TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '{}'
);
