"""Eruku runner. Executed with the Eruku environment's Python, never imported by the app.

Protocol: one JSON request on stdin; JSON-lines events on stdout; last line {"event": "result", ...}.
Uses only the call documented on the Hugging Face model card:
    AutoModel.from_pretrained(model_id, trust_remote_code=True).generate_handwriting(...)
"""

import json
import os
import sys
import time


def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def probe(req):
    out = {"ok": True, "python": sys.version.split()[0]}
    try:
        import torch

        out.update(torch=True, torch_version=torch.__version__, cuda=bool(torch.cuda.is_available()))
        if out["cuda"]:
            props = torch.cuda.get_device_properties(0)
            out.update(gpu=props.name, vram_mb=round(props.total_memory / 2**20))
    except ImportError:
        out.update(torch=False, cuda=False)
    try:
        import transformers

        out.update(transformers=True, transformers_version=transformers.__version__)
    except ImportError:
        out.update(transformers=False)
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(req["model_id"], "config.json")
        out["weights_cached"] = isinstance(cached, str)
    except ImportError:
        out["weights_cached"] = False
    return out


def download(req):
    from huggingface_hub import snapshot_download

    emit("progress", fraction=0.0, message=f"downloading {req['model_id']}")
    path = snapshot_download(req["model_id"], allow_patterns=["*.json", "*.py", "pytorch_model.bin", "*.md"])
    emit("progress", fraction=0.5, message="loading once to fetch T5 and VAE dependencies")
    from transformers import AutoModel

    AutoModel.from_pretrained(req["model_id"], trust_remote_code=True)
    return {"path": path}


def load(req):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never download implicitly during inference
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(req["model_id"], trust_remote_code=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    return torch, model, device


def preflight(req):
    from PIL import Image

    torch, model, device = load(req)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    style = Image.new("RGB", (256, 64), "white")
    started = time.time()
    with torch.no_grad():
        model.generate_handwriting(style_image=style, gen_text="test", style_text="", cfg_scale=1.25)
    elapsed = time.time() - started
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return {"device": device, "seconds_per_word": round(elapsed, 2), "peak_memory_mb": round(peak) if peak else None,
            "note": "tiny workload on a blank style image; output discarded"}


def infer(req):
    from PIL import Image

    torch, model, device = load(req)
    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    style = Image.open(req["style_image"]).convert("RGB")
    outputs = []
    total = len(req["texts"]) * len(req["seeds"])
    done = 0
    for ti, text in enumerate(req["texts"]):
        for seed in req["seeds"]:
            torch.manual_seed(seed)
            started = time.time()
            with torch.no_grad():
                img = model.generate_handwriting(style_image=style, gen_text=text, style_text=req.get("style_text", ""),
                                                 cfg_scale=req.get("cfg_scale", 1.25))
            path = os.path.join(req["out_dir"], f"t{ti:03d}-s{seed}.png")
            img.save(path)
            done += 1
            outputs.append({"text": text, "seed": seed, "path": path, "width": img.width, "height": img.height,
                            "seconds": round(time.time() - started, 2)})
            emit("progress", fraction=done / total, message=f"{done}/{total}")
    peak = torch.cuda.max_memory_allocated() / 2**20 if device == "cuda" else None
    return {"outputs": outputs, "device": device, "peak_memory_mb": round(peak) if peak else None}


def main():
    req = json.loads(sys.stdin.read())
    actions = {"probe": probe, "download": download, "preflight": preflight, "infer": infer}
    try:
        result = actions[req["action"]](req)
    except Exception as exc:  # report, never hide
        emit("result", ok=False, error=f"{exc.__class__.__name__}: {exc}")
        return 1
    emit("result", ok=True, **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
