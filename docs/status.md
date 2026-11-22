# Status (hand-off)

Last updated: 2026-11-22, end of the first build session (branch `claude/blissful-shannon-mu38kt`).

Legend:
- **implemented**: the code exists and runs.
- **fixtures**: verified with synthetic, non-personal fixtures (tests or a browser).
- **live**: verified with a real external service.
- **blocked**: cannot be completed here; the reason and next step are given.

Nothing has been verified with your real handwriting.

## Capabilities

| Area | State | Evidence / notes |
| --- | --- | --- |
| One-command launch (`python -m handprinter run`) | implemented, fixtures | Starts the web server and the worker. |
| Windows launchers (`scripts/run.bat`, `scripts/run.ps1`) | implemented, **untested on Windows** | Please confirm they work on your PC. |
| `doctor` (OS, Python, GPU/VRAM, disk, decoders, claude CLI, adapters) | implemented, fixtures | GPU reporting is untested because the container has no GPU. |
| Import: JPEG/PNG/WebP/ZIP with per-entry results | fixtures | `tests/test_archive_safety.py` covers traversal, symlinks, nested archives, bounds, a lying ZIP header and corrupt entries. |
| HEIC and PDF (optional extras) | implemented; PDF fixtures | The PDF test builds a 3-page PDF. HEIC was checked only for decoder availability, not with a real HEIC file. |
| Byte-for-byte originals, SHA-256 duplicates, near-duplicate warning | fixtures | Near-duplicates use a 256-bit dHash plus an ink-overlap signature; the thresholds still need tuning on real photos (see the open questions below). |
| EXIF orientation, rotation, perspective with manual corners, lighting for display only | fixtures | The geometry round-trip tests compare against Pillow for all 8 EXIF orientations. |
| Quality signals (blur, glare, shadow, clipped) | implemented | These are heuristics and have not been calibrated. |
| Local line, word and ruling detection | fixtures | Projection profiles; expected to struggle on messy real worksheets. |
| Coordinate frames, invertible transforms, staleness on reprocessing | fixtures | `tests/test_geometry.py` and `handprinter/pages.py: apply_processing`. |
| Review UI: zoom, pan, draw, move, resize, split, merge, classify, marks, other author, undo, shortcuts | implemented, fixtures in a browser | The keyboard checks are in `docs/screenshots/coverage-report.json`. |
| State model, human edits protected from re-recognition | fixtures | `tests/test_review.py` and `tests/test_recognition.py`. |
| Dataset export (PNG crops, JSONL, immutable snapshots, canonical grouped split, unseen words) | fixtures | `tests/test_dataset.py`. |
| Recognition package → `/recognise` → validate → import | fixtures and **live** | Fixture results cover the malformed, partial, hash-mismatch, tampered and stale cases. The live run is described in the next section. |
| Headless `claude -p` automation | implemented, not run | Off by default. The flags were checked against `claude --help`; no headless chunk was executed here. |
| Durable jobs, cancel, retry, restart recovery | fixtures | `tests/test_jobs.py`. |
| Security: loopback, Host/Origin checks, request token, LAN sign-in | fixtures | `tests/test_api_e2e.py`. |
| Studio: assets, canvas layout in mm, flow, undo/redo, save/open, PNG export at a set dpi | fixtures | The export size check is 100×50 mm at 254 dpi = 1000×500 px. |
| Eruku adapter | **blocked** | Needs a separate PyTorch environment and about 3 GB of weights. The runner uses only the model-card API. Next step: create the environment and set `HANDPRINTER_ERUKU_PYTHON`, then download and preflight from Experiments. |
| DiffusionPen adapter | **blocked** | Upstream has no documented entry point for sampling custom words with your own style images. Next step: clone the repo locally and write and verify a sampling driver against the real code. |
| Fine-tuning either model | **blocked** | Neither upstream project documents an own-writer fine-tune command. Details are in `docs/model-experiments.md`. |
| Blind A/B, run log, style profiles | implemented, fixtures for the API | Needs real generations from two adapters before it can be used. |
| Performance budgets | measured in the container | See `docs/performance.md` (10k regions). Re-measure on your desktop. |

## Live recognition run

On 2026-11-22 a package built from one synthetic fixture page was processed by a Haiku agent that had the recogniser instructions. The package had 4 items: 1 overview and 3 line crops.

- The `.claude/agents/handwriting-recogniser.md` file was created during that session, so it was not loaded as an agent type. Following the brief's fallback, a general agent was spawned with `model: haiku` and the same instructions.
- The agent reported its model as `claude-haiku-5-5`.
- `python -m handprinter recognition validate` reported 4 valid, 0 invalid.
- The import stored 4 proposals, all in `needs_review`.
- Haiku correctly classified the fixture text as typeset rather than handwriting (`printed_text`, `author_ambiguous: true`), and it preserved the misspelling `becuase`.
- **This does not validate recognition quality on real handwriting.**
- In a fresh Claude Code session in this repository, `/recognise` should use the real subagent file. That has not been tried yet.

## Independent reviews

All reviews were done by Sonnet subagents.

- **Backend review: done.** It reported 4 high and 11 medium findings, and the material ones are fixed. The fixes are listed in the commit "Fix findings from independent backend review". The remaining low items are listed under known limitations below.
- **Visual review:**
  - Round 1 failed 4 of the screens. Those issues were fixed.
  - Round 2 passed all 8 screens.
  - Details are in `docs/screenshots/README.md`.

## Known limitations and open questions

- Near-duplicate thresholds (`ingest.py`): synthetic pages that share a template can still link. Linking errs on the safe side for leakage, and snapshots warn when one group dominates. This needs tuning on real photos.
- Detection quality on real, messy pages is unknown. It is the first thing to try with your own pages.
- Undo is per region and steps back one revision at a time. It cannot revive regions that were split or merged.
- A failed import leaves its staged upload in `<data>/staging/`, and a re-run snapshot export can leave an orphan dataset folder. Both are safe to delete.
- Cancel is honoured between progress points. A short job such as detection or processing may still finish, and it records that the cancel arrived late.
- LAN mode is opt-in. Loopback clients skip sign-in, so do not put a reverse proxy in front of it.

## Next concrete steps

1. On your PC, run `python -m handprinter doctor` and share the GPU and VRAM line.
2. Import 5–10 real pages, check that detection is usable, and tune `NEAR_DUPLICATE_*` if needed.
3. Run `/recognise` in a fresh Claude Code session on a small package, then review the results.
4. Set up the Eruku environment, then download, preflight and run the baseline.
5. For DiffusionPen, write a sampling driver against the cloned repo.

## Pull request

Opened as https://github.com/mrlancus/handprinter/pull/1 (not merged). Short summary:

> **Handprinter: local handwriting dataset and generation studio (first build)**
>
> - The full local app: import, review, recognition hand-off, datasets, experiments, Studio.
> - Tests: 64 spec-based tests on synthetic fixtures. `tsc --checkJs` is clean.
> - Recognition: one live run with Haiku on a fixture page.
> - Model adapters: blocked on a GPU environment and weights, and reported honestly as blocked.
