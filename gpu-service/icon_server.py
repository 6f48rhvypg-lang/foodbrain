"""FoodBrain icon generation server — runs on Windows PC with RTX GPU.

Accepts POST /generate {"prompt": str, "size": int} → PNG bytes.
FoodBrain's /api/icon/ endpoint calls this when an icon isn't cached.

Uses HiDream-I1-Full. The 17B transformer and the LLaMA-3.1-8B text encoder
are loaded with 4-bit NF4 quantization (bitsandbytes) so the transformer
(~9 GB quantized vs ~34 GB in BF16) fits entirely in VRAM and stays resident
during denoising. Without this, model-cpu-offload thrashes transformer blocks
across PCIe every step (~149 s/step → 2 h per image). With it, the GPU does
real tensor math. Fits comfortably in 32 GB (RTX 5090).
"""

import io
import logging
import threading
import time

import torch
from diffusers import BitsAndBytesConfig as DiffusersBnbConfig
from diffusers import HiDreamImagePipeline, HiDreamImageTransformer2DModel
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel
from transformers import AutoTokenizer, LlamaForCausalLM
from transformers import BitsAndBytesConfig as TransformersBnbConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI()

LLAMA_MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"
HIDREAM_MODEL = "HiDream-AI/HiDream-I1-Full"

log.info("Loading LLaMA tokenizer …")
tokenizer_4 = AutoTokenizer.from_pretrained(LLAMA_MODEL, local_files_only=True)

log.info("Loading LLaMA-3.1-8B-Instruct (NF4) …")
text_encoder_4 = LlamaForCausalLM.from_pretrained(
    LLAMA_MODEL,
    output_hidden_states=True,
    quantization_config=TransformersBnbConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    ),
    torch_dtype=torch.bfloat16,
    local_files_only=True,
)

log.info("Loading HiDream transformer (NF4) …")
transformer = HiDreamImageTransformer2DModel.from_pretrained(
    HIDREAM_MODEL,
    subfolder="transformer",
    quantization_config=DiffusersBnbConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    ),
    torch_dtype=torch.bfloat16,
)

log.info("Loading HiDream-I1-Full pipeline …")
pipe = HiDreamImagePipeline.from_pretrained(
    HIDREAM_MODEL,
    tokenizer_4=tokenizer_4,
    text_encoder_4=text_encoder_4,
    transformer=transformer,
    torch_dtype=torch.bfloat16,
)
pipe.enable_model_cpu_offload()
log.info("Model ready.")

_lock = threading.Lock()


class GenerateRequest(BaseModel):
    prompt: str
    size: int = 128


@app.post("/generate")
def generate(req: GenerateRequest) -> Response:
    log.info("Generating icon: %r at %dpx", req.prompt[:80], req.size)
    t0 = time.perf_counter()
    with _lock:
        result = pipe(
            prompt=req.prompt,
            num_inference_steps=50,
            guidance_scale=5.0,
            height=1024,
            width=1024,
        )
    elapsed = time.perf_counter() - t0
    image: Image.Image = result.images[0]
    if req.size < 1024:
        image = image.resize((req.size, req.size), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    log.info("Done in %.1fs (%.2fs/step).", elapsed, elapsed / 50)
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "device": "cuda", "model": "hidream-i1-full"}
