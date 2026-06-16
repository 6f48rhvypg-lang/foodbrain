"""FoodBrain icon generation server — runs on Windows PC with RTX GPU.

Accepts POST /generate {"prompt": str, "size": int} → PNG bytes.
FoodBrain's /api/icon/ endpoint calls this when an icon isn't cached.

Uses HiDream-I1-Full (BF16). LLaMA-3.1-8B-Instruct is loaded separately
and passed as text_encoder_4 (required by diffusers HiDream pipeline).
Requires 32 GB VRAM (RTX 5090). First run downloads weights automatically.
"""

import io
import logging
import threading

import torch
from diffusers import HiDreamImagePipeline
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel
from transformers import AutoTokenizer, LlamaForCausalLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

LLAMA_MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"

log.info("Loading LLaMA tokenizer …")
tokenizer_4 = AutoTokenizer.from_pretrained(LLAMA_MODEL)

log.info("Loading LLaMA-3.1-8B-Instruct (BF16) …")
text_encoder_4 = LlamaForCausalLM.from_pretrained(
    LLAMA_MODEL,
    output_hidden_states=True,
    output_attentions=True,
    torch_dtype=torch.bfloat16,
)

log.info("Loading HiDream-I1-Full (BF16) …")
pipe = HiDreamImagePipeline.from_pretrained(
    "HiDream-AI/HiDream-I1-Full",
    tokenizer_4=tokenizer_4,
    text_encoder_4=text_encoder_4,
    torch_dtype=torch.bfloat16,
).to("cuda")
log.info("Model ready.")

_lock = threading.Lock()


class GenerateRequest(BaseModel):
    prompt: str
    size: int = 128


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    log.info("Generating icon: %r at %dpx", req.prompt[:80], req.size)
    with _lock:
        result = pipe(
            prompt=req.prompt,
            num_inference_steps=50,
            guidance_scale=5.0,
            height=1024,
            width=1024,
        )
    image: Image.Image = result.images[0]
    if req.size < 1024:
        image = image.resize((req.size, req.size), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    log.info("Done.")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/health")
def health() -> dict:
    device = str(next(pipe.transformer.parameters()).device)
    return {"ok": True, "device": device, "model": "hidream-i1-full"}
