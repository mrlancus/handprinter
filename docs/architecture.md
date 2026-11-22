# Architecture

One person, one PC. Two OS processes, one SQLite file, one data directory.

```
browser (ES modules) ──HTTP/JSON──▶ web process (FastAPI, uvicorn, loopback)
                                        │  reads/writes metadata, enqueues jobs
                                        ▼
                                  SQLite (WAL)  ◀──── worker process (polls jobs, heartbeats)
                                        ▲                 │ heavy work: decode, detect, crop,
                                        │                 │ package, export, adapter subprocesses
                              data directory (files) ◀────┘
                                                          └──▶ adapter env subprocess (JSON lines on stdout)
```

`python -m handprinter run` starts the web server and spawns the worker as a child process; `serve` and `worker` run them individually.

## Online mode

`python -m handprinter online` (or `scripts/online.bat`) runs the same two processes with `HANDPRINTER_ONLINE=1` and starts `cloudflared` (`handprinter/tunnel.py`): a quick tunnel (random `trycloudflare.com` address) or a named tunnel when `HANDPRINTER_TUNNEL_TOKEN` is set (passed via the environment, never argv). The tunnel connects from loopback, so in online mode **no client is trusted by address**: every page and API call needs the session cookie (HMAC of the password, `HttpOnly`, `Secure` over https, `SameSite=Lax`, 30 days); `/login`, `/api/login` and `/static/*` are public. Sign-in attempts are limited per visitor (`CF-Connecting-IP`, 5 per 15 min) and globally (20 per 24 h). API responses carry `cache-control: no-store` and `cdn-cache-control: no-store`. The per-start request token still guards every state change. Headless recognition defaults on, so recognition can be started from the iPad. See [online.md](online.md).

## Data model

| Table | Purpose |
| --- | --- |
| `batches` | One import action (time, note, per-file results). |
| `sources` | One page image. Stable UUID; original filename, sha256, dHash, MIME, source type (`paper`/`ipad`), session, date, style era, source group, PDF page index, dimensions for each frame, `processing_rev`, quality flags. |
| `regions` | Lines, words, blocks and marks. Box in a named frame + frame dimensions + `processing_rev`; class, status, text, text origin, flags, revision, parent (word → line), other-author flag. |
| `region_revisions` | Full snapshot of a region after every change, with actor (`human`/`machine`/`detector`) and action. Undo restores a previous snapshot as a new revision. |
| `links` | Mark → target region/span (with `unknown_target`, `unknown_original`) and before/after correction captures. |
| `proposals` | Machine recognition results: model as reported, prompt version, schema version, image hash, region revision, payload, `applied`, `stale`. |
| `recognition_packages`, `recognition_items` | Package manifest mirror; per-item status (`pending`/`succeeded`/`failed`), cache key. |
| `jobs` | Durable job queue (see below). |
| `snapshots`, `snapshot_items` | Immutable dataset exports and their exact region revisions and splits. |
| `runs`, `generations`, `ab_trials` | Model runs, generated images, blind comparison ratings. |
| `compositions`, `studio_assets` | Studio pages (physical units) and imported images. |

## Image lineage and coordinate frames

| Frame | Definition | Stored as |
| --- | --- | --- |
| `original` | Pixel grid of the decoded original file, before EXIF orientation. | `originals/<id>.<ext>` (bytes untouched) |
| `working` | Original with EXIF orientation applied. Full resolution, lossless PNG. | `working/<id>.png` |
| `processed` | Working image after rotation + perspective (homography `H_wp`). Lighting normalisation is a display/detection layer only. | `processed/<id>-r<rev>.png` |
| `recognition` | A resized region or page image inside a recognition package (`scale`, `offset` back to `processed`). | package `images/` |
| `crop` | A region's pixels, cut from the working image by warping through `H_wp⁻¹` (geometry only, no lighting). | `crops/<region>-r<rev>.png` |
| `viewport` | Browser display: `scale` + `translate` over the processed preview. | frontend only |

All transforms are 3×3 homographies in `handprinter/geometry.py` (`Homography.compose/inverse/apply`), shared by every module; the frontend uses the same affine maths for viewport mapping (`static/js/geometry.js`). EXIF orientation is a homography too, so `original ↔ working ↔ processed ↔ recognition ↔ crop` round-trips are testable.

**Staleness.** Changing processing creates a new `processing_rev`. Regions on the old revision are remapped through the working frame (bounding box of the mapped corners), flagged `geometry_stale`, and their unapplied proposals are marked stale. Stale regions are flagged, so they block export until a human re-confirms. Snapshots copy crops, so earlier snapshots never change.

**Crops never stretch.** Exported crops keep native pixel size; manifests record width, height, aspect and the source scale. Adapter-specific resizing (e.g. DiffusionPen 64×256) is produced from canonical crops into the run directory.

## Ingestion and review state machine

Import (per file, in the worker): validate size → sniff content type → (ZIP) iterate entries safely → decode with pixel bound → EXIF orient → hash → duplicate/near-duplicate check → store original/working/preview → quality signals → `source` row. Every file and ZIP entry gets its own result (`imported`, `duplicate`, `rejected: reason`).

Region annotation status:

```
unprocessed ──machine proposal (clean)──▶ proposed ──┐
     │       ──machine proposal (flagged)─▶ needs_review
     │                                           │
     └──── human: approve / exclude / reopen ────┴──▶ approved ⇄ needs_review ⇄ excluded
```

- Allowed transitions live in `review.TRANSITIONS`; anything else is rejected.
- Machine actions only move `unprocessed → proposed|needs_review` and `proposed → needs_review`. They never touch `approved`/`excluded` regions or regions whose text origin is `human`; such proposals are stored unapplied and flagged as `disagreement` when text differs, waiting for the explicit "Apply proposal" action.
- Approving resolves active flags (moved to `resolved_flags`), records the approved revision and the reason.
- Defer keeps `needs_review` with a `deferred` marker so it sorts last.
- Job state (`jobs.status`) is independent of annotation status.

## Background jobs

`jobs(id, kind, status, params, progress, message, result, error, attempts, heartbeat_at, worker_pid, cancel_requested, …)`.

- Status: `queued → running → succeeded | failed | cancelled`; `interrupted` when recovered.
- Claim: single `UPDATE … WHERE id = (SELECT … queued ORDER BY created) RETURNING` — atomic under SQLite.
- Heartbeat every 2 s; handlers call `ctx.check()` which updates progress and raises on cancellation.
- Recovery on worker start and periodically: `running` jobs with a stale heartbeat or a dead PID become `interrupted`, then are requeued if `attempts < max_attempts` and the kind is idempotent, otherwise `failed` with an explanation. Nothing stays "running" forever.
- Concurrency: one general job at a time plus at most one GPU job (`gpu` jobs are serialised separately).
- Progress reaches the browser by polling `/api/jobs?active=1` (1 s while jobs are active, otherwise idle).

## API boundary

- JSON under `/api/*`, Pydantic models in `handprinter/web/schemas.py`. Files are served only through ID-based endpoints (`/api/sources/{id}/image?frame=…`, `/api/regions/{id}/crop`), never by path.
- Security middleware: Host must be loopback (or the configured LAN host), Origin (when present) must match, and every non-GET request needs the `X-Handprinter-Token` header matching the per-start token embedded in the page. LAN mode (`HANDPRINTER_HOST` non-loopback) refuses to start without `HANDPRINTER_ACCESS_TOKEN` and then requires a session cookie obtained with it. No CORS headers are emitted.
- Lists are paginated (`limit ≤ 200`, keyset or offset with indexed columns).

## Omitted areas

A person can draw a box over anything that is not their handwriting (diagram, printed worksheet text, someone else's notes, scribbles). It is stored as a region: `kind = block`, `cls = omitted`, `status = excluded`, the reason in `review_reason`. Effects (`handprinter/pages.py`):

- Detection zeroes the ink mask inside omitted areas, so no boxes are proposed there.
- Regions at least 60% inside are excluded with `review_reason = omitted:<area id>` (approved regions are kept and reported). Restoring an area deletes it and returns each of those regions to its exact pre-omit status from `region_revisions`.
- Recognition packages paint omitted areas flat grey in every image (overview and line context) before anything is written, and skip excluded word boxes.
- `GET /api/sources/{id}/analysis` returns a plain-language account of the page (original, orientation, correction, duplicates, quality, detection, omitted areas, suggestions, training readiness). The page view ("What it found") draws it over the corrected image.

## Maths (LaTeX)

Maths is stored as LaTeX source in `regions.text`: a region with `cls = equation` holds bare LaTeX; handwriting text can contain inline `$...$` maths. Prompt v2 asks the recogniser for LaTeX (`$...$`, `region_type: equation` for maths-only targets); import flags any maths `contains_math` so a person always checks it, keeps `$...$` spans as single alignment tokens, and does not spread a maths-only line over word boxes. Approving `equation` needs LaTeX. Exports put maths in `maths.jsonl` + `maths/` (`format: latex | text+inline_latex`), sharing the page-grouped split but never entering the word vocabulary or unseen-word subset. The browser renders LaTeX with the vendored MathJax (`static/js/math.js`); Studio can add typeset maths as an image asset (typeset, not handwriting).

## Recognition package format

`<data>/recognition/jobs/<job-id>/`

```
manifest.json          job_id, created_at, prompt_version, schema_version, model_configured, items[]
prompt.md              copy of prompts/recognition/<version>.md
result.schema.json     JSON Schema each result must satisfy
images/<item-id>.png   ≤ 1568 px long edge, ≤ 1.15 MP (Claude vision guidance)
results/               one <item-id>.json per item, written by the subagent
```

Manifest item: `item_id`, `image` (relative), `image_sha256`, `width`, `height`, `frame` (`recognition`), `source_frame` (`processed`, w, h, `processing_rev`), `to_source` (`scale_x`, `scale_y`, `offset_x`, `offset_y`), `context_type` (`page_overview`|`region`|`line`), `target_box` (target within the image, the rest is context), `local_boxes` (detected word boxes in image coords), `source_id`, `region_id`.

Import: schema validation (`jsonschema`), item ID and sha256 must match the manifest, boxes are mapped back through `to_source`, word text is aligned to local boxes (`recognition/align.py`, DP with merge/split moves and a width cost; ambiguity is flagged, never zipped). When the alignment is ambiguous (or the line has no word boxes) and every word box is an untouched machine box, `recognition/refit.py` re-cuts the line's ink at its N−1 widest gaps for the N recognised words and replaces those boxes, but only if the new cut fits the text better; an untouched detector line grows to hold its words. Any box a person drew, moved, labelled or decided on blocks this, and cuts that are not clearly wider than the gaps inside words stay flagged `alignment_ambiguous`. Valid results become proposals; invalid/missing items stay `pending` with the reason and can be repackaged alone. Cache key = sha256(image) + model + prompt version + settings; cached items are skipped unless forced.

Claude Code side: `.claude/agents/handwriting-recogniser.md` (`model: haiku`, `tools: Read, Write`) and `.claude/skills/recognise/SKILL.md`. Optional headless mode runs `claude -p` per chunk with the agent passed inline (`--agents`, `--agent handwriting-recogniser`, `--model haiku`, `--tools Read,Write`, `--disallowedTools Bash,Edit,WebFetch,WebSearch`, `--permission-mode acceptEdits`, `--output-format json`) and **cwd set to the package directory**, so writes cannot reach the repository. It only runs when `doctor` confirms `claude auth status` reports `loggedIn`. Import also refuses manifest entries that differ from what the app recorded when packaging.

## Model adapters

`handprinter/adapters/base.py` defines `ModelAdapter`: `status()` (environment, repo, checkpoint, blockers, next step), `capabilities()` (levels, charset, max length, needs style refs, can train), `convert_dataset(snapshot)`, `preflight()`, `infer(request)`, `train(request)`. Execution goes through `adapters/runner.py`, which starts the adapter's own Python (`HANDPRINTER_<NAME>_PYTHON`) with a runner script and reads JSON-lines progress; cancellation terminates the process group. Adapters: `diffusionpen`, `eruku` — see [model-experiments.md](model-experiments.md).

## Trade-offs and open questions

- **No OpenCV.** Detection (projection profiles, adaptive threshold) and page-corner finding are numpy + Pillow. Good enough for proposals on worksheets; documented as fallible. Horizontal printed ruling (rows mostly ink) and long vertical rules such as margin lines (one unbroken stroke over 15% of the page height) are removed before lines and words are found; word boxes then grow vertically, up to halfway to the neighbouring line, while their own columns still hold ink, so tails and loops are not cut off. Add OpenCV only if real pages show a concrete gap.
- **Axis-aligned boxes in the processed frame.** Simple and robust once perspective is corrected; strongly curved lines would need polygons (LATER).
- **Crops skip lighting normalisation** to keep faint ink faithful; this means training crops may show uneven paper. Open: offer a per-snapshot "normalised" variant.
- **Polling vs SSE.** Polling at 1 s only while jobs run is simpler and survives server restarts; SSE can replace it without API changes.
- **Region revisions store full snapshots** (small rows) rather than diffs: simple undo and audit at the cost of some space.
- **Canonical split.** `source_splits` persists each page's split; later snapshots extend it to new pages only, so experiments share one split. Group keys are the smallest member source ID.
- Open: near-duplicates require a 256-bit dHash within 24 bits AND ink-layout IoU ≥ 0.6. Same-template synthetic pages can still link (safe for leakage, coarser splits; snapshots warn when one group dominates). Needs tuning on real photos.
- Open: whether Eruku fine-tuning is feasible on an RTX 2060 (likely not without LoRA/frozen backbone).
