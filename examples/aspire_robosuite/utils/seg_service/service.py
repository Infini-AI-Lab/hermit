"""SAM3-wire-compatible segmentation service backed by ungated models.

Text prompts: Grounding-DINO (open-vocabulary boxes) -> SAM box prompts -> masks.
Point prompts: SAM single positive point, multimask output.
Implements the cap_harness SAM3 HTTP contract (GET /openapi.json, POST /segment,
POST /segment_point) so ASPIRE uses it via CAP_HARNESS_SAM3_URL unchanged.
"""
import argparse, asyncio, base64, io, os

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel
from transformers import (AutoModelForZeroShotObjectDetection, AutoProcessor,
                          SamModel, SamProcessor)

DINO_ID = os.environ.get("SEG_DINO_MODEL", "IDEA-Research/grounding-dino-base")
SAM_ID = os.environ.get("SEG_SAM_MODEL", "facebook/sam-vit-huge")
BOX_THRESHOLD = float(os.environ.get("SEG_BOX_THRESHOLD", "0.25"))
TEXT_THRESHOLD = float(os.environ.get("SEG_TEXT_THRESHOLD", "0.2"))
MAX_SEGMENTATIONS = 5
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

app = FastAPI(title="grounded-sam (sam3-compatible)")
_LOCK = asyncio.Lock()
M = {}


class SegmentRequest(BaseModel):
    image_base64: str
    text_prompt: str


class PointPromptRequest(BaseModel):
    image_base64: str
    point_coords: list[float]


def _load():
    M["dino_proc"] = AutoProcessor.from_pretrained(DINO_ID)
    M["dino"] = AutoModelForZeroShotObjectDetection.from_pretrained(DINO_ID).to(DEVICE).eval()
    M["sam_proc"] = SamProcessor.from_pretrained(SAM_ID)
    M["sam"] = SamModel.from_pretrained(SAM_ID).to(DEVICE).eval()


def _image(payload: str) -> Image.Image:
    try:
        return Image.open(io.BytesIO(base64.b64decode(payload, validate=True))).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image data: {exc}") from exc


def _raw(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


@torch.inference_mode()
def _sam(image: Image.Image, **prompt):
    inputs = M["sam_proc"](image, return_tensors="pt", **prompt).to(DEVICE)
    out = M["sam"](**inputs, multimask_output=True)
    masks = M["sam_proc"].image_processor.post_process_masks(
        out.pred_masks.cpu(), inputs["original_sizes"].cpu(), inputs["reshaped_input_sizes"].cpu())[0]
    return masks.numpy(), out.iou_scores.cpu().numpy()[0]  # (P,3,H,W), (P,3)


@torch.inference_mode()
def _segment_text(image: Image.Image, prompt: str) -> dict:
    text = prompt.strip().lower()
    text = text if text.endswith(".") else text + "."
    inputs = M["dino_proc"](images=image, text=text, return_tensors="pt").to(DEVICE)
    det = M["dino"](**inputs)
    res = M["dino_proc"].post_process_grounded_object_detection(
        det, inputs.input_ids, threshold=BOX_THRESHOLD, text_threshold=TEXT_THRESHOLD,
        target_sizes=[image.size[::-1]])[0]
    boxes, scores = res["boxes"].cpu().numpy(), res["scores"].cpu().numpy()
    if len(scores) == 0:
        return {"results": []}
    order = np.argsort(scores)[::-1][:MAX_SEGMENTATIONS]
    boxes, scores = boxes[order], scores[order]
    masks, ious = _sam(image, input_boxes=[[b.tolist() for b in boxes]])
    results = []
    for i in range(len(scores)):
        best = int(np.argmax(ious[i]))
        m = masks[i, best].astype(np.uint8)
        results.append({"mask_base64": _raw(m), "shape": list(m.shape), "box": boxes[i].astype(float).tolist(),
                        "score": float(scores[i]), "label": prompt})
    return {"results": results}


def _segment_point(image: Image.Image, point) -> dict:
    masks, ious = _sam(image, input_points=[[[list(point)]]])
    m, s = masks[0].astype(np.float32), ious[0].astype(float)
    order = np.argsort(s)[::-1][:MAX_SEGMENTATIONS]
    m, s = m[order], s[order]
    return {"scores": s.tolist(), "masks_base64": _raw(m), "masks_shape": list(m.shape), "masks_dtype": "float32"}


@app.post("/segment")
async def segment(req: SegmentRequest):
    image = _image(req.image_base64)
    async with _LOCK:
        return await asyncio.to_thread(_segment_text, image, req.text_prompt)


@app.post("/segment_point")
async def segment_point(req: PointPromptRequest):
    if len(req.point_coords) != 2:
        raise HTTPException(status_code=400, detail="point_coords must contain [x, y]")
    image = _image(req.image_base64)
    async with _LOCK:
        return await asyncio.to_thread(_segment_point, image, tuple(map(float, req.point_coords)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8114)
    a = ap.parse_args()
    _load()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
