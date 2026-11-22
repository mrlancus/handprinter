# Dependencies

Each direct dependency, with the reason it is here. The web app environment never contains PyTorch or other ML packages.

## App (Python ≥ 3.11, installed with `pip install -e .`)

| Package | Why |
| --- | --- |
| `fastapi` | Typed HTTP API with Pydantic request models and simple static file serving. |
| `uvicorn` | ASGI server that runs FastAPI on loopback. |
| `python-multipart` | Streaming multipart uploads (FastAPI needs it for `UploadFile`). |
| `pydantic` | Typed request and composition models, with validation at the API boundary. |
| `pillow` | Decoding, EXIF orientation, perspective warps, crops, previews and Studio rendering. |
| `numpy` | Projection profiles, adaptive thresholds, homography solving and quality signals. This avoids needing OpenCV. |
| `jsonschema` | Validates recogniser results against the same `result.schema.json` that Claude Code receives. |

## Optional extras

| Extra | Package | Why |
| --- | --- | --- |
| `heic` | `pillow-heif` | HEIC/HEIF decoding for iPad and iPhone photos. |
| `pdf` | `pypdfium2` | Renders PDF pages to images. |
| `dev` | `pytest`, `httpx` | Test runner, and the HTTP client used by FastAPI's TestClient. |

## Frontend

The frontend is browser-native ES modules and hand-written CSS, with no framework or build step. One vendored runtime file:

| File | Why |
| --- | --- |
| `handprinter/static/vendor/mathjax/tex-svg-full.js` (MathJax 3.2.2, Apache-2.0, licence alongside) | Renders LaTeX maths previews in Review and typesets maths for Studio. Vendored so nothing loads from a CDN (the page CSP allows only `'self'`). Loaded lazily the first time maths is shown. Under the strict CSP its injected inline stylesheet is refused (a console message); the SVG output renders correctly without it. |

`typescript` (dev only, in `package.json`) runs `tsc --checkJs` over the JSDoc-typed modules. It is never needed to launch the app.

## Not dependencies (deliberately)

- **OpenCV.** Detection uses numpy. It can be added if real pages show a concrete gap.
- **Redis, Celery and Docker.** Jobs live in SQLite and a single worker process.
- **The Anthropic SDK and API keys.** Recognition goes only through Claude Code.

## Model environments (separate, user-created)

See [model-experiments.md](model-experiments.md).
- Eruku: torch, torchvision, transformers==4.53.0, diffusers==0.34.0, einops, huggingface_hub.
- DiffusionPen: torch, torchvision, diffusers, transformers, wandb (upstream pins no versions).
