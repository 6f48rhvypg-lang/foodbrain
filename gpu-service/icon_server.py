"""FoodBrain icon generation server — runs on Windows PC with RTX GPU.

Accepts POST /generate {"prompt": str, "size": int} → PNG bytes.
FoodBrain's /api/icon/ endpoint calls this when an icon isn't cached.

First run downloads ~34 GB of HiDream-I1-Full weights from HuggingFace.
"""

import io
import logging

import torch
from diffusers import HiDreamImagePipeline
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

log.info("Loading HiDream-I1-Full (BF16) — this takes ~1 min on first run …")
pipe = HiDreamImagePipeline.from_pretrained(
    "HiDream-ai/HiDream-I1-Full",
    torch_dtype=torch.bfloat16,
).to("cuda")
log.info("Model ready.")


class GenerateRequest(BaseModel):
    prompt: str
    size: int = 128


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    log.info("Generating icon: %r at %dpx", req.prompt[:60], req.size)
    result = pipe(
        req.prompt,
        height=req.size,
        width=req.size,
        num_inference_steps=28,
    )
    image = result.images[0]
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    log.info("Done.")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "device": str(next(pipe.unet.parameters()).device)}
