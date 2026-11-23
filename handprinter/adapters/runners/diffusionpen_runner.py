"""DiffusionPen runner. Executed with the DiffusionPen environment's Python, never imported by the app.

Protocol: one JSON request on stdin; JSON-lines events on stdout; last line {"event": "result", ...}.

Written against koninik/DiffusionPen at commit 059e2f2bcb7372231038719e955597917c1a8783 (read, not guessed):
- the model is built exactly as `train.py: main()` builds it (UNetModel + CANINE-c text encoder, latent 4x8x32);
- style comes only from 5 reference word images through the MobileNetV2 style encoder: `unet.py: forward`
  averages their features and adds `style_lin(features)` to the time embedding; the writer label is unused;
- style and target images are prepared like `train.py: Diffusion.sampling` and `utils/iam_dataset.py`
  (height 64, white-padded or shrunk into 256x64, normalised to [-1, 1]);
- sampling is 50 DDIM steps from the Stable Diffusion v1.5 scheduler, decoded with its VAE (scale 0.18215);
- fine-tuning repeats `train.py: train()` (noise-prediction MSE, AdamW) on your own words, with the text
  encoder frozen so it fits a 6 GB card.
We import only `unet.py` and `feature_extractor.py` from the repository (no wandb, no IAM loaders).
"""

import json
import os
import random
import sys
import time

H, W = 64, 256
CHARSET = '!"#&\'()*+,-./0123456789:;?ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz '
STYLE_CLASSES = 339  # IAM writer count; the label embedding exists in the checkpoint but sampling ignores it


def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def probe(req):
    out = {"ok": True, "python": sys.version.split()[0]}
    for mod in ("torch", "diffusers", "transformers", "timm", "einops"):
        try:
            m = __import__(mod)
            out[mod] = True
            out[f"{mod}_version"] = getattr(m, "__version__", "")
        except ImportError:
            out[mod] = False
    out["cuda"] = False
    if out["torch"]:
        import torch

        out["cuda"] = bool(torch.cuda.is_available())
        if out["cuda"]:
            props = torch.cuda.get_device_properties(0)
            out.update(gpu=props.name, vram_mb=round(props.total_memory / 2**20))
    repo = req.get("repo", "")
    out["repository"] = os.path.isfile(os.path.join(repo, "unet.py")) and os.path.isfile(os.path.join(repo, "feature_extractor.py"))
    out["checkpoint"] = os.path.isfile(req.get("checkpoint", ""))
    out["style_model"] = os.path.isfile(req.get("style_model", ""))
    sd = req.get("sd_path", "")
    out["vae"] = os.path.isfile(os.path.join(sd, "vae", "config.json")) and os.path.isfile(os.path.join(sd, "scheduler", "scheduler_config.json"))
    if out["repository"]:
        try:
            import subprocess

            out["commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            out["commit"] = ""
    return out


class _Args:  # the attributes unet.UNetModel reads from train.py's argparse namespace
    interpolation = False
    mix_rate = None


def _strip(state):
    """Upstream wraps the UNet and its text encoder in nn.DataParallel, which adds 'module.' to their keys."""
    out = {}
    for k, v in state.items():
        k = k[7:] if k.startswith("module.") else k
        out[k.replace("text_encoder.module.", "text_encoder.", 1)] = v
    return out


def load(req, train=False):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never download implicitly
    sys.path.insert(0, req["repo"])
    import torch
    from diffusers import AutoencoderKL, DDIMScheduler
    from transformers import CanineModel, CanineTokenizer

    from feature_extractor import ImageEncoder  # type: ignore[import-not-found]
    from unet import UNetModel  # type: ignore[import-not-found]

    device = "cuda" if torch.cuda.is_available() and req.get("device", "auto") != "cpu" else "cpu"
    canine = req.get("canine", "google/canine-c")
    tokenizer = CanineTokenizer.from_pretrained(canine)
    text_encoder = CanineModel.from_pretrained(canine)
    unet = UNetModel(image_size=(H, W), in_channels=4, model_channels=320, out_channels=4, num_res_blocks=1,
                     attention_resolutions=(1, 1), channel_mult=(1, 1), num_heads=4, num_classes=STYLE_CLASSES,
                     context_dim=320, vocab_size=len(CHARSET), text_encoder=text_encoder, args=_Args())
    missing, unexpected = unet.load_state_dict(_strip(torch.load(req["checkpoint"], map_location="cpu")), strict=False)
    if missing or unexpected:
        raise RuntimeError(f"checkpoint does not match the model: {len(missing)} missing, {len(unexpected)} unexpected keys "
                           f"(first: {(missing or unexpected)[:3]})")
    unet = unet.to(device)
    vae = AutoencoderKL.from_pretrained(req["sd_path"], subfolder="vae").to(device).requires_grad_(False).eval()
    ddim = DDIMScheduler.from_pretrained(req["sd_path"], subfolder="scheduler")
    style = ImageEncoder(model_name="mobilenetv2_100", num_classes=0, pretrained=False, trainable=False)
    state = _strip(torch.load(req["style_model"], map_location="cpu"))
    own = style.state_dict()
    matched = {k: v for k, v in state.items() if k in own and own[k].shape == v.shape}
    if len(matched) < len(own) * 0.9:
        raise RuntimeError(f"style encoder weights do not match mobilenetv2_100 ({len(matched)}/{len(own)} tensors)")
    own.update(matched)
    style.load_state_dict(own)
    style = style.to(device).requires_grad_(False).eval()
    if not train:
        unet.eval()
    return torch, unet, vae, ddim, style, tokenizer, device


def prepare(path):
    """A word image as DiffusionPen sees it: height 64, then padded or shrunk into 256x64 on white."""
    from PIL import Image, ImageOps

    img = Image.open(path).convert("RGB")
    w, h = img.size
    img = img.resize((max(1, int(w * H / h)), H))
    if img.width < W:
        return ImageOps.pad(img, size=(W, H), color="white")
    while img.width > W:  # upstream shrinks in 20 px steps, keeping the aspect ratio
        new_w = img.width - 20
        img = img.resize((new_w, max(1, int(img.height * new_w / img.width))))
    out = Image.new("RGB", (W, H), (255, 255, 255))
    out.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return out


def to_tensor(torch, img):
    import numpy as np

    arr = np.asarray(img, dtype=np.float32) / 255.0
    return (torch.from_numpy(arr).permute(2, 0, 1) - 0.5) / 0.5


def style_features(torch, style, paths, device, n):
    if len(paths) < 5:
        paths = (list(paths) * 5)[:5]
    imgs = torch.stack([to_tensor(torch, prepare(p)) for p in paths[:5]]).to(device)
    feats = style(imgs)  # [5, 1280]
    return feats.repeat(n, 1)  # unet reshapes to [n, 5, 1280] and averages


def sample(torch, unet, vae, ddim, tokenizer, feats, texts, device, seed, steps=50):
    gen = torch.Generator(device="cpu").manual_seed(seed)
    n = len(texts)
    context = tokenizer(texts, padding="max_length", truncation=True, return_tensors="pt", max_length=40).to(device)
    x = torch.randn((n, 4, H // 8, W // 8), generator=gen).to(device)
    ddim.set_timesteps(steps)
    for t in ddim.timesteps:
        tt = torch.full((n,), int(t), dtype=torch.long, device=device)
        eps = unet(x, tt, context, None, style_extractor=feats)
        x = ddim.step(eps, t, x).prev_sample
    img = vae.decode(x / 0.18215).sample
    return ((img / 2 + 0.5).clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()


def crop_white(arr, pad=4):
    """Trim the white padding around a generated word (columns only, like upstream crop_whitespace_width)."""
    import numpy as np

    gray = arr.mean(axis=2)
    cols = np.where(gray.min(axis=0) < 200)[0]
    if cols.size == 0:
        return arr
    return arr[:, max(0, cols[0] - pad): cols[-1] + 1 + pad]


def preflight(req):
    import tempfile

    from PIL import Image

    torch, unet, vae, ddim, style, tokenizer, device = load(req)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    with tempfile.TemporaryDirectory() as tmp:
        blank = os.path.join(tmp, "blank.png")
        Image.new("RGB", (W, H), "white").save(blank)
        started = time.time()
        with torch.no_grad():
            feats = style_features(torch, style, [blank], device, 1)
            sample(torch, unet, vae, ddim, tokenizer, feats, ["test"], device, 0)
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return {"device": device, "seconds_per_word": round(time.time() - started, 2),
            "peak_memory_mb": round(peak) if peak else None, "note": "one word on a blank style image; output discarded"}


def infer(req):
    from PIL import Image

    torch, unet, vae, ddim, style, tokenizer, device = load(req)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    outputs = []
    total = len(req["texts"]) * len(req["seeds"])
    done = 0
    with torch.no_grad():
        feats1 = style_features(torch, style, req["style_images"], device, 1)
        for seed in req["seeds"]:
            batch = int(req.get("batch", 8))
            for start in range(0, len(req["texts"]), batch):
                texts = req["texts"][start:start + batch]
                started = time.time()
                arrs = sample(torch, unet, vae, ddim, tokenizer, feats1.repeat(len(texts), 1), texts, device, seed + start,
                              int(req.get("steps", 50)))
                per = (time.time() - started) / len(texts)
                for i, (text, arr) in enumerate(zip(texts, arrs)):
                    ti = start + i
                    path = os.path.join(req["out_dir"], f"t{ti:03d}-s{seed}.png")
                    img = Image.fromarray(crop_white(arr)).convert("L")
                    img.save(path)
                    outputs.append({"text": text, "seed": seed, "path": path, "width": img.width, "height": img.height,
                                    "seconds": round(per, 2)})
                    done += 1
                emit("progress", fraction=done / total, message=f"{done}/{total}")
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return {"outputs": outputs, "device": device, "peak_memory_mb": round(peak) if peak else None}


def train(req):
    """Fine-tune on your own words. Text encoder, VAE and style encoder stay frozen (as upstream freezes the latter two)."""
    import copy

    torch, unet, vae, ddim, style, tokenizer, device = load(req, train=True)
    rng = random.Random(int(req.get("seed", 7)))
    torch.manual_seed(int(req.get("seed", 7)))
    items = [it for it in req["items"] if it["text"] and all(c in CHARSET for c in it["text"])]
    if len(items) < 5:
        raise ValueError(f"need at least 5 usable training words, got {len(items)}")
    steps, bs = int(req.get("steps", 2000)), int(req.get("batch_size", 8))
    lr, ema_beta = float(req.get("lr", 5e-5)), float(req.get("ema", 0.995))
    save_every = int(req.get("save_every", 500))
    out_dir = req["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    unet.text_encoder.requires_grad_(False)
    unet.text_encoder.eval()
    params = [p for n, p in unet.named_parameters() if not n.startswith("text_encoder.")]
    opt = torch.optim.AdamW(params, lr=lr)
    ema = copy.deepcopy(unet).eval().requires_grad_(False)
    use_amp = device == "cuda" and bool(req.get("fp16", True))
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    cache = {}

    def tensor(path):
        if path not in cache:
            cache[path] = to_tensor(torch, prepare(path))
        return cache[path]

    paths = [it["image"] for it in items]
    with torch.no_grad():  # the latents of each target never change: encode them once
        latents = {}
        for i in range(0, len(paths), 16):
            chunk = paths[i:i + 16]
            z = vae.encode(torch.stack([tensor(p) for p in chunk]).to(device)).latent_dist.mean * 0.18215
            latents.update({p: z[j].cpu() for j, p in enumerate(chunk)})
    losses, started, window = [], time.time(), []
    for step in range(1, steps + 1):
        batch = [rng.choice(items) for _ in range(bs)]
        x0 = torch.stack([latents[it["image"]] for it in batch]).to(device)
        with torch.no_grad():
            refs = torch.stack([tensor(p) for it in batch for p in rng.sample(paths, 5)]).to(device)
            feats = style(refs)
            context = tokenizer([it["text"] for it in batch], padding="max_length", truncation=True, return_tensors="pt",
                                max_length=40).to(device)
        noise = torch.randn_like(x0)
        t = torch.randint(0, ddim.config.num_train_timesteps, (bs,), device=device).long()
        xt = ddim.add_noise(x0, noise, t)
        with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=use_amp):
            pred = unet(xt, timesteps=t, context=context, y=None, style_extractor=feats)
            loss = torch.nn.functional.mse_loss(pred.float(), noise)
        opt.zero_grad(set_to_none=True)
        scaler.scale(loss).backward()
        scaler.unscale_(opt)
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        scaler.step(opt)
        scaler.update()
        with torch.no_grad():
            for pe, pm in zip(ema.parameters(), unet.parameters()):
                if pm.requires_grad:
                    pe.mul_(ema_beta).add_(pm.detach(), alpha=1 - ema_beta)
        window.append(loss.item())
        if step % 25 == 0 or step == steps:
            losses.append({"step": step, "loss": round(sum(window) / len(window), 5)})
            window = []
            emit("progress", fraction=step / steps, message=f"step {step}/{steps}, loss {losses[-1]['loss']}")
        if step % save_every == 0 or step == steps:
            torch.save(ema.state_dict(), os.path.join(out_dir, "ema_ckpt.pt.tmp"))
            os.replace(os.path.join(out_dir, "ema_ckpt.pt.tmp"), os.path.join(out_dir, "ema_ckpt.pt"))
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return {"checkpoint": os.path.join(out_dir, "ema_ckpt.pt"), "steps": steps, "items_used": len(items),
            "items_skipped": len(req["items"]) - len(items), "losses": losses, "device": device,
            "seconds": round(time.time() - started, 1), "peak_memory_mb": round(peak) if peak else None}


# Exact revisions the driver was verified against (Hugging Face commit hashes).
DOWNLOADS = [
    ("konnik/DiffusionPen", "5636a68365001435ec76553114313c22c6f1ca07", ".",
     ["diffusionpen_iam_model_path/models/ema_ckpt.pt", "style_models/iam_style_diffusionpen.pth"]),
    ("stable-diffusion-v1-5/stable-diffusion-v1-5", "451f4fe16113bff5a5d2269ed5ad43b0592e9a14", "stable-diffusion-v1-5",
     ["vae/config.json", "vae/diffusion_pytorch_model.safetensors", "scheduler/scheduler_config.json"]),
    ("google/canine-c", "dc0eaffdff3fa9161613311c7096eeb3e133ee19", "canine-c",
     ["config.json", "pytorch_model.bin", "tokenizer_config.json", "special_tokens_map.json"]),
]


def download(req):
    """Weights only (no code is downloaded or run). About 1.5 GB."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    root = req["weights"]
    files = [(repo, rev, sub, f) for repo, rev, sub, names in DOWNLOADS for f in names]
    got = []
    for i, (repo, rev, sub, name) in enumerate(files):
        emit("progress", fraction=i / len(files), message=f"{repo}: {name}")
        try:
            got.append(hf_hub_download(repo, name, revision=rev, local_dir=os.path.join(root, sub)))
        except EntryNotFoundError:
            if name != "special_tokens_map.json":  # optional for CANINE
                raise
    return {"weights": root, "files": len(got)}


def main():
    req = json.loads(sys.stdin.read())
    actions = {"probe": probe, "download": download, "preflight": preflight, "infer": infer, "train": train}
    try:
        result = actions[req["action"]](req)
    except Exception as exc:  # report, never hide
        emit("result", ok=False, error=f"{exc.__class__.__name__}: {exc}")
        return 1
    emit("result", **{"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
