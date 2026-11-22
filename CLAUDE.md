# Handprinter — notes for Claude sessions

Local handwriting dataset and generation studio for one person on one desktop PC.
Full requirements: [docs/build-brief.md](docs/build-brief.md). Hand-off state: [docs/status.md](docs/status.md).

## Hard rules

- Everything is local. No Anthropic API, SDK or API keys. Recognition only via Claude Code (Haiku subagent, file hand-off).
- Recognition output is a **proposal**. Never overwrite a human edit without an explicit user action.
- Preserve originals byte-for-byte. Cut training crops from the full-resolution working image (geometry only), PNG.
- Never beautify handwriting: perspective correction only, no slant/spacing/baseline fixes.
- Every stored box records its coordinate frame and frame dimensions; transforms live only in `handprinter/geometry.py`.
- Model adapters never fake training, generation, downloads or metrics. Blocked = exact blocker + next step.
- Heavy work runs in the worker process, never in the web process.
- Personal data, DBs, weights, packages, outputs and logs stay out of Git (data dir is outside the repo).
- The app must work without GPU, Claude Code, real handwriting or model weights.
- Online mode (Cloudflare Tunnel) trusts no client by address: every request needs the password session. Never add a loopback bypass there.

## Architecture (see [docs/architecture.md](docs/architecture.md))

FastAPI web process + separate worker process, both started by `python -m handprinter run`.
SQLite (WAL) holds metadata, annotations and durable jobs; files live in the data directory.
Frontend: browser-native ES modules + JSDoc, checked by `tsc --checkJs` (dev only, no build step).

## Folder map

| Path | Contents |
| --- | --- |
| `handprinter/` | Python package: `config`, `db`, `geometry`, `ingest`, `preprocess`, `detect`, `review`, `recognition/`, `dataset`, `jobs`, `worker`, `adapters/`, `evaluation`, `studio`, `doctor`, `web/` |
| `handprinter/static/` | Frontend: `index.html`, `css/tokens.css` (design tokens), `css/*.css`, `js/*.js`, `vendor/mathjax/` (vendored, local LaTeX rendering) |
| `prompts/recognition/` | Versioned recognition prompts (`v1.md`, current `v2.md` adds LaTeX maths) |
| `.claude/agents/`, `.claude/skills/` | Haiku recogniser subagent and `/recognise` skill |
| `tests/` | pytest suite + fixture generators (non-personal, synthetic) |
| `scripts/` | Launchers, screenshot harness |
| `docs/` | Architecture, status, design system, model research, screenshots |

## Data layout

Data dir (default `~/handprinter-data`, set `HANDPRINTER_DATA_DIR`): `handprinter.sqlite3`, `originals/`, `working/`, `processed/`, `previews/`, `crops/`, `staging/`, `recognition/jobs/<id>/`, `datasets/<snapshot>/`, `runs/`, `studio/`, `logs/`.

## Design system

Tokens: `handprinter/static/css/tokens.css`. Rules: [docs/design-system.md](docs/design-system.md). Accent forest green for primary actions, amber = review needed, red = error, everything else neutral.

## Performance budgets

Warm UI usable 2 s; review action acknowledged 100 ms; pan/zoom 60 fps; 10k examples stay responsive (paginated queries, cached thumbnails/crops).

## Testing policy

Spec-based, few tests on risky logic (archive safety, transforms, state transitions, human-edit preservation, split leakage, export integrity, recognition import, job recovery). Expected values come from the spec, not current output. Run `pytest -q` and `npx tsc -p .` after changes.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # optional extras: ".[heic,pdf]"
python -m handprinter doctor
python -m handprinter run          # http://127.0.0.1:8765
python -m handprinter online       # same + Cloudflare Tunnel + password (docs/online.md)
```

## Current status

Core release implemented and fixture-verified; live Haiku recognition verified once on a fixture page; model adapters blocked on GPU environment/weights. Details and next steps: [docs/status.md](docs/status.md). Performance: [docs/performance.md](docs/performance.md).
