"""FoodBrain icon generation server — runs on Windows PC with RTX GPU.

Accepts POST /generate {"prompt": str, "size": int} → PNG bytes.
FoodBrain's /api/icon/ endpoint calls this when an icon isn't cached.

Uses FLUX.1-schnell (Apache 2.0, ~24 GB, no gated access required).
First run downloads weights from HuggingFace automatically.
"""

import io
import logging

import torch
from diffusers import FluxPipeline
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

log.info("Loading FLUX.1-schnell (BF16) — first run downloads ~24 GB …")
pipe = FluxPipeline.from_pretrained(
    "black-forest-labs/FLUX.1-schnell",
    torch_dtype=torch.bfloat16,
).to("cuda")
log.info("Model ready.")


class GenerateRequest(BaseModel):
    prompt: str
    size: int = 128


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    log.info("Generating icon: %r at %dpx", req.prompt[:60], req.size)
    # FLUX minimum useful resolution is 256; generate at 512 and resize down.
    gen_size = max(req.size, 512)
    result = pipe(
        req.prompt,
        height=gen_size,
        width=gen_size,
        num_inference_steps=4,
        guidance_scale=0.0,  # schnell is a distilled model, no CFG needed
    )
    image: Image.Image = result.images[0]
    if req.size < gen_size:
        image = image.resize((req.size, req.size), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    log.info("Done.")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/health")
def health() -> dict:
    device = str(next(pipe.transformer.parameters()).device)
    return {"ok": True, "device": device}
