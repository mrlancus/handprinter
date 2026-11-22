# Model experiments: DiffusionPen and Eruku

Research done 2026-11-22 by a Sonnet research subagent, reading primary sources. [V] = read in the source, [I] = inference, [E] = estimate. Commit hashes could not be read (the GitHub API was blocked from the research session). Record the hashes when you clone: the adapters log `git rev-parse HEAD` for every run.

## DiffusionPen

- Paper [V]: *DiffusionPen: Towards Controlling the Style of Handwritten Text Generation*, ECCV 2024, [arXiv:2409.06065](https://arxiv.org/abs/2409.06065).
- Code [V]: <https://github.com/koninik/DiffusionPen> (`main`), MIT licence.
- Weights [V]: <https://huggingface.co/konnik/DiffusionPen> (MIT):
  - `diffusionpen_iam_model_path/models/ema_ckpt.pt` (680 MB), plus `ckpt.pt` and `optim.pt`.
  - `style_models/iam_style_diffusionpen.pth` (10.9 MB).
- External weights [V]: the VAE and DDIM scheduler come from `stable-diffusion-v1-5` (passed as `--stable_dif_path`).
- Entry point [V]: `train.py` does both training and sampling. Sampling uses `--train_mode sampling --sampling_mode single_sampling|paragraph`.
  - Other flags: `--save_path`, `--style_path`, `--stable_dif_path`, `--epochs`, `--batch_size` (default 320), `--level`, `--dataset` (iam, gnhk), `--img_size`, `--device`, `--load_check`.
  - There is no separate inference script and no requirements file.
- Dependencies [V]: the code imports `torch`, `torchvision`, `diffusers`, `transformers` (CANINE), `wandb`, `tqdm`, `PIL` and `numpy`. No versions are pinned.
- Input [V]:
  - Word images, height 64 and width 256, resized and padded.
  - 5 style references per writer.
  - The character set is hard-coded: ``! " # & ' ( ) * + , - . / 0-9 : ; ? A-Z a-z`` and space.
- Fine-tuning [V/I]: there is no fine-tune command. The README says to "adjust the data loader" for your own data. `--load_check` resumes from a checkpoint. A new writer needs a custom dataset class. The model conditions on style images, so a zero-shot baseline from 5 of your crops is possible without training.
- RTX 2060 [E]: sampling plausibly fits in 6 GB. Training at batch size 320 does not fit, so a small batch is required. This is untested.

## Eruku (name confirmed)

- Paper [V]: *Autoregressive Styled Text Image Generation, but Make it Reliable*, WACV 2026, [arXiv:2510.23240](https://arxiv.org/abs/2510.23240).
  - Project page: <https://aimagelab.github.io/Eruku>.
  - It succeeds Emuru ([arXiv:2503.17074](https://arxiv.org/abs/2503.17074)).
- Code [V]: <https://github.com/Blowing-Up-Groundhogs/Eruku> (`main`), MIT licence.
  - Main files: `train_eruku_continous.py`, `generate_iam_eruku.py` and `requirements.txt`.
  - Pins in `requirements.txt`: diffusers 0.34.0, transformers 4.53.0, accelerate 1.8.1. torch is not pinned.
- Weights [V]: [`blowing-up-groundhogs/eruku`](https://huggingface.co/blowing-up-groundhogs/eruku).
  - The model card says Apache-2.0, which does not match the code's MIT licence.
  - `pytorch_model.bin` is 2.88 GB, and it uses custom code (`trust_remote_code=True`).
  - It loads `google-t5/t5-large`, `blowing-up-groundhogs/emuru_vae` and `google/byt5-small`.
- Inference [V, model card]:
  ```python
  model = AutoModel.from_pretrained("blowing-up-groundhogs/eruku", trust_remote_code=True)
  img = model.generate_handwriting(style_image=pil, gen_text="Hello", style_text="", cfg_scale=1.25)
  ```
  The card says to keep `cfg_scale` ≤ 2.0. `max_new_tokens` defaults to 512.
- Input [V]:
  - One style image, resized to height 64 with the aspect ratio kept.
  - Output width varies up to the token cap.
  - The tokenizer is byte-level ByT5. No supported character set is documented. [I] English glyphs are the realistic set.
- Fine-tuning [V/I]: `train_eruku_continous.py` (accelerate, webdataset, hard-coded batch 32 and accumulation to 256) has no documented own-writer fine-tune.
- RTX 2060 [E]: fp32 inference probably fits in 6 GB for short text (CFG doubles the batch). Full fine-tuning of T5-Large is unlikely to fit.

## How the adapters use this

| | DiffusionPen | Eruku |
| --- | --- | --- |
| Baseline (no training) | `train.py --train_mode sampling` with style crops from a snapshot. Custom driver required, see below. | `generate_handwriting()` with a style crop. |
| Levels | word | word, line |
| Training in app | Not offered: no upstream fine-tune command. | Not offered: no upstream own-writer fine-tune. |
| Character check | Hard-coded set above; others reported per prompt. | Bytes accepted; non-ASCII warned as untested. |

The app ships the Eruku runner script (`handprinter/adapters/runners/eruku_runner.py`). It uses only the documented model-card call, so it needs its own environment with torch and the pinned packages.

DiffusionPen sampling is tied to the IAM loaders inside `train.py`. The upstream code has no documented "sample these words with these 5 style images" entry point. The DiffusionPen adapter therefore reports **blocked: no upstream entry point for custom style references**. The next step is to write a small driver against the cloned repo once you can inspect it locally. We do not guess its internal import paths.

## Experiment protocol (implemented in the app)

1. Freeze a dataset snapshot. Every run records the snapshot ID, the split manifest hash, seed, checkpoint, code commit, configuration, environment, duration, peak memory, outputs and errors.
2. Run a baseline on the fixed prompt set (`handprinter/evaluation.py: DEFAULT_PROMPTS`), separately for words and short lines, with several seeds.
3. Personalise. This is blocked until an upstream fine-tune path exists, and the UI says so.
4. Run blind A/B ratings with a reveal, compared only on prompts both models support. Excluded prompts are listed.
5. OCR exact-text checks are a fallible proxy. No metric is ever synthesised.

## Status

Both adapters are **implemented and blocked on assets and GPU** in the cloud build environment. Neither has been run.
