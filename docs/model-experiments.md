# Model experiments: what remains, what was verified, what comes next

## Research and review (2026-11-23)

Question: once enough handwriting is labelled, what is left to turn it into a generator that writes like you, on an RTX 2060 (6 GB)?

Method: cloned the upstream code and read it line by line (not the READMEs alone), downloaded the real weights, ran them on CPU in the build container, and searched for newer methods. Your own page crops were used locally as style input and were never committed.

### Findings

| Finding | Evidence | Consequence |
| --- | --- | --- |
| DiffusionPen's style comes only from 5 reference word images. A MobileNetV2 encoder averages their features into the time embedding; the writer label is ignored when style features are given. | `unet.py: forward` at commit 059e2f2 | Custom style needs no IAM loaders, only a small driver. The earlier "blocked: no entry point" was too pessimistic. |
| A driver that rebuilds the model exactly as `train.py: main()` does generates real words from your 5 crops. | Ran with the real weights on CPU: 8 words, about 2.6 s per word. | **DiffusionPen generation works.** |
| Fine-tuning is the upstream loop (noise-prediction MSE, AdamW) on your own words. The UNet has 38M trainable parameters; the 132M-parameter CANINE text encoder can be frozen. | Ran 30 steps on CPU; loss reported; checkpoint saved and reloaded for generation. | **Personalisation is possible on a 6 GB card** (about 600 MB of optimiser state). Speed on the 2060 is not measured yet. |
| Zero-shot output is legible and picks up some of your print-like spacing, but strokes are heavier than your pen. | Looked at the outputs. | Fine-tuning is the main lever for looking like you. |
| Eruku's `generate()` already removes the style image from its output. Passing the style crop's transcription (`style_text`) is supported and helps. | `modeling_eruku.py` on Hugging Face, revision 13f4f75 | The runner was right to use the documented call; the app already passes the transcription. |
| Eruku is T5-Large (about 770M parameters, 2.9 GB of weights) with no documented own-writer fine-tune. | Model card and `train_eruku_continous.py` | Use it zero-shot as the line-level baseline. A LoRA fine-tune is possible in principle but unverified, so it is not built. |
| Eruku was **not run here**: loading it executes code fetched from Hugging Face (`trust_remote_code`), and that was refused in the build environment. | Permission refusal | The first real Eruku run will be on your PC. |
| Both runners would have crashed on `probe` (a duplicate `ok` key in the result event). | Found by running | Fixed. |
| A `.env` saved by older Notepad or PowerShell 5 starts with a byte-order mark, which silently broke the first setting. | Read the loader | Fixed: `.env` is read as `utf-8-sig`, and the setup script writes without a BOM. |
| DiffBrush (ICCV 2025, MIT) generates whole lines, so it could capture joins between words. Its weights are only on Google Drive or Baidu, and its script needs multi-GPU NCCL (Linux). | Read `generate.py` at da9addc | Candidate for a third adapter later. Not built now. |
| No public model writes maths with a personal style. DiffMath (2026 preprint) generates maths expressions but has no style conditioning that we could confirm. DiffusionPen's character set has no maths symbols. | Searches; `CHARSET` | Maths stays as recognition data. Generating your maths would need composing it from your own symbol crops, which is future work. |

### Review of the findings

- The verification is of the **mechanics** (it loads, generates, trains, saves and reloads), not of quality on a GPU or after a real fine-tune. Thirty CPU steps prove only that the path works.
- The style crops were cut from a screenshot of a page, which is lower quality than your real photos. Zero-shot quality on real crops may be better.
- DiffusionPen is word-level and its character set is the IAM set. Lines are made by placing words. Generated lines should use your own measured word gaps, which the dataset already records; that is not built yet.
- The time estimate (10–30 minutes for 2000 steps on a 2060) is an extrapolation and is labelled as such in the UI.

### Plan, in order

1. **Done (this change):**
   - DiffusionPen driver: generation, pinned weight download (about 1.5 GB) and fine-tuning.
   - App wiring: Experiments has a Download button, a "Fine-tune on your handwriting" panel, and a Weights choice (Pretrained or one of your fine-tunes) when generating.
   - Training uses training-split words only, so validation and test words stay unseen.
   - `SetupModels.bat` creates one CUDA environment for both models.
2. **On your PC:**
   - Run `SetupModels.bat`.
   - In Experiments, press **Download weights**, then **Run memory preflight**.
   - Approve a few hundred words and export a snapshot.
   - Create a style profile and run the baseline.
   - Fine-tune, generate again with your fine-tune, and compare blind.
3. **Next to build:**
   - Line composition from your own spacing statistics.
   - Measured GPU timings fed back into the UI.
   - Eruku zero-shot on lines once it has run on your PC.
   - Then consider DiffBrush, or a LoRA fine-tune of Eruku.

---

# Background notes (2026-11-22)

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
| Training in app | **Fine-tune on your words** (driver, see above). | Not offered: no upstream own-writer fine-tune. |
| Character check | Hard-coded set above; others reported per prompt. | Bytes accepted; non-ASCII warned as untested. |

The app ships the Eruku runner script (`handprinter/adapters/runners/eruku_runner.py`). It uses only the documented model-card call, so it needs its own environment with torch and the pinned packages.

*(Superseded 2026-11-23.)* This said DiffusionPen sampling was tied to the IAM loaders. Reading the code showed that it is not. The driver `handprinter/adapters/runners/diffusionpen_runner.py` imports only `unet.py` and `feature_extractor.py`.

## Experiment protocol (implemented in the app)

1. Freeze a dataset snapshot. Every run records the snapshot ID, the split manifest hash, seed, checkpoint, code commit, configuration, environment, duration, peak memory, outputs and errors.
2. Run a baseline on the fixed prompt set (`handprinter/evaluation.py: DEFAULT_PROMPTS`), separately for words and short lines, with several seeds.
3. Personalise: fine-tune DiffusionPen on the snapshot's training words (Experiments → Fine-tune). Each fine-tune is a run of kind `train`, and its weights stay in `<data>/models/diffusionpen/personal/<run>/`.
4. Run blind A/B ratings with a reveal, compared only on prompts both models support. Excluded prompts are listed.
5. OCR exact-text checks are a fallible proxy. No metric is ever synthesised.

## Status

- DiffusionPen: generation and fine-tuning were verified on CPU with the real weights (2026-11-23). GPU runs are not yet measured.
- Eruku: not run (see above).
