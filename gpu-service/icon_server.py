"""FoodBrain icon generation server — runs on Windows PC with RTX GPU.

Accepts POST /generate {"prompt": str, "size": int} → PNG bytes.
FoodBrain's /api/icon/ endpoint calls this when an icon isn't cached.

Uses SDXL-Turbo (Apache 2.0, ~6.5 GB, 4-step generation, no CFG).
First run downloads weights from HuggingFace automatically.
"""

import io
import logging
import threading

import torch
from diffusers import AutoPipelineForText2Image
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

log.info("Loading SDXL-Turbo (FP16) — first run downloads ~6.5 GB …")
pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/sdxl-turbo",
    torch_dtype=torch.float16,
    variant="fp16",
).to("cuda")
log.info("Model ready.")

_lock = threading.Lock()


class GenerateRequest(BaseModel):
    prompt: str
    size: int = 128


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    log.info("Generating icon: %r at %dpx", req.prompt[:60], req.size)
    with _lock:
        result = pipe(
            prompt=req.prompt,
            num_inference_steps=4,
            guidance_scale=0.0,  # turbo is distilled — no CFG needed
            height=512,
            width=512,
        )
    image: Image.Image = result.images[0]
    if req.size < 512:
        image = image.resize((req.size, req.size), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    log.info("Done.")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/health")
def health() -> dict:
    device = str(next(pipe.unet.parameters()).device)
    return {"ok": True, "device": device, "model": "sdxl-turbo"}
