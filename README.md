# Handprinter

A local website that turns photographs of your own handwriting into a reviewed training dataset. It compares two handwriting-generation models (DiffusionPen and Eruku) and composes handwriting onto pages.

Everything runs and is stored on your computer. The only exception is images you explicitly send for recognition through Claude Code.

- **Status:** see [docs/status.md](docs/status.md).
- **Requirements:** see [docs/build-brief.md](docs/build-brief.md).
- **Architecture:** see [docs/architecture.md](docs/architecture.md).

## Use it from an iPad or phone

Run `scripts\online.bat` instead of `run.bat`: it publishes the app through a free Cloudflare Tunnel behind a password. Step by step: [docs/online.md](docs/online.md).

## Quick start

You need Python 3.11 or newer. A GPU, Claude Code and model weights are all optional.

```bash
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[heic,pdf]"      # or just: pip install -e .
python -m handprinter doctor      # what this machine has
python -m handprinter run         # web server + worker → http://127.0.0.1:8765
```

On Windows you can double-click `scripts\run.bat` or run `scripts\run.ps1` instead. Both are **untested on Windows**, so please confirm they work. On Linux or macOS, use `scripts/run.sh`. Each launcher creates `.venv` on its first run.

`run` starts the web server and the background worker together. You can also start them separately with `python -m handprinter serve` and `python -m handprinter worker`.

## Configuration

Copy `.env.example` to `.env`. Every setting is optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `HANDPRINTER_DATA_DIR` | `~/handprinter-data` | Images, crops, packages, snapshots, runs and the SQLite DB. Keep it outside Git. |
| `HANDPRINTER_HOST` / `_PORT` | `127.0.0.1` / `8765` | Loopback only by default. |
| `HANDPRINTER_ACCESS_TOKEN` | — | Required (16+ characters) when the host is not loopback (LAN mode). |
| `HANDPRINTER_RECOGNITION_MODEL` | `haiku` | Must match `model:` in `.claude/agents/handwriting-recogniser.md`; `doctor` checks that it does. |
| `HANDPRINTER_HEADLESS_RECOGNITION` | `0` | `1` lets the worker run the recogniser with `claude -p`. |
| `HANDPRINTER_ERUKU_PYTHON`, `HANDPRINTER_DIFFUSIONPEN_*` | — | Separate model environments (see below). |

## Workflow

1. **Sources.** Drop JPEG, PNG, WebP or ZIP files. HEIC and PDF need the optional extras.
   - Each file and each ZIP entry gets its own result.
   - Originals are kept byte-for-byte, exact duplicates are detected, and near-duplicates are flagged but never deleted.
   - Lines and words are detected locally as editable proposals.
2. **Correct the photo** (optional). Rotate or perspective-correct a page, with manual corner adjustment available. Regions already drawn on the page are moved to the new geometry and flagged as stale.
3. **Recognise** (optional). Select pages and choose **Prepare recognition…**, which shows exactly which images will leave the machine. Then run `/recognise` in Claude Code inside this repository and click **Import results**.
4. **Review.**
   - Correct transcriptions exactly as written.
   - Adjust, split and merge boxes, classify regions and link marks.
   - Mark someone else's handwriting.
   - Approve, exclude or defer each region.
   - Press `?` for keyboard shortcuts.
5. **Dataset.** Export an immutable snapshot of PNG crops plus `words.jsonl` and `lines.jsonl`, split by page group, with an unseen-word test subset.
6. **Experiments.** Check adapter status, run baselines on a fixed prompt set, rate outputs blind A/B, and generate.
7. **Studio.** Place generated or imported images on a page in millimetres and export a PNG at an explicit dpi. No model is needed for this.

## Recognition through Claude Code

No API key or SDK is involved. The app writes a package to `<data>/recognition/jobs/<id>/` containing `manifest.json`, the images, `prompt.md`, `result.schema.json` and an empty `results/` folder. The repository ships:

- `.claude/agents/handwriting-recogniser.md`: `model: haiku`, tools `Read, Write` only.
- `.claude/skills/recognise/SKILL.md`: the `/recognise` skill. It lists pending packages, dispatches the subagent in chunks of 5, then runs validation.

Helper commands:

```bash
python -m handprinter recognition pending
python -m handprinter recognition validate <package-id>
python -m handprinter recognition import <package-id>
```

Results are **proposals**. Re-running recognition never overwrites your corrections unless you click **Apply this proposal**.

## Optional models

Both adapters run in **their own** Python environments and talk to the app through a subprocess that emits JSON lines. See [docs/model-experiments.md](docs/model-experiments.md) for the sources, licences and exact blockers.

**Eruku.** Create a venv with a CUDA build of PyTorch, then:

```bash
pip install transformers==4.53.0 diffusers==0.34.0 einops huggingface_hub pillow
```

Set `HANDPRINTER_ERUKU_PYTHON=/path/to/that/python`. Then, in Experiments:

1. Choose **Download weights…** (about 3 GB plus dependencies). This is an explicit action.
2. Run **memory preflight**.
3. Create a style profile and run the baseline.

**DiffusionPen.** The adapter reports a blocker: upstream has no documented way to sample your own words with your own style images. The next step is written out in the docs.

The app never trains or generates without a working adapter. It also never shows fabricated output or metrics.

## Backup and restore

```bash
python -m handprinter backup /path/to/backup-folder   # SQLite online backup + all data files
```

- **Restore:** stop the app, then either point `HANDPRINTER_DATA_DIR` at the backup folder or copy its contents back.
- **Manual backup:** copying the whole data directory while the app is stopped is also a valid backup.

## Development

```bash
pip install -e ".[dev,pdf]"
pytest -q                 # spec-based tests on synthetic, non-personal fixtures
npm install && npx tsc -p .   # JSDoc type check (dev only; not needed to run the app)
```

Further documentation:
- Dependencies, each with its reason: [docs/dependencies.md](docs/dependencies.md).
- Design system: [docs/design-system.md](docs/design-system.md).
- Screenshots: [docs/screenshots/](docs/screenshots/).
