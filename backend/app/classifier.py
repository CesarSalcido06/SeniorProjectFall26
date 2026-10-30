"""Decide whether a photo is a prop, character, or creature. The category picks the Meshy settings."""
import base64
import re

import httpx

from .config import CATEGORIES

# First draft written by MegaMan.EXE (BenchAI research model); tightened so the answer is one parseable word
# and humanoid robots count as characters, since Meshy's auto-rig handles any bipedal body.
SYSTEM_PROMPT = """You classify a photo of one real-world object for a 3D game-asset pipeline.
Answer with ONLY one lowercase word: prop, character, or creature.

- character: a humanoid, two-legged figure with arms and a head (person, action figure, doll, humanoid robot, statue of a person). It will get a humanoid skeleton.
- creature: a non-humanoid animal or monster (dog, cat, dragon, spider, fish, plush animal on four legs).
- prop: anything else, including vehicles, furniture, tools, food, mechanical parts, and non-humanoid robots.

Tie-breakers: classify the main object in the center of the photo. A plush animal standing upright on two legs with arms is a character. If unsure, answer prop."""


class ClassificationResult:
    def __init__(self, category: str, source: str, note: str = "") -> None:
        self.category = category
        self.source = source  # "user", "classifier", or "default"
        self.note = note


def parse_category(text: str) -> str | None:
    words = re.findall(r"[a-z]+", text.lower())
    for word in words:
        if word in CATEGORIES:
            return word
    return None


async def classify(photo_jpeg: bytes, requested: str, settings) -> ClassificationResult:
    if requested in CATEGORIES:
        return ClassificationResult(requested, "user")
    if settings.classifier != "openai":
        return ClassificationResult("prop", "default", "Classifier is off (CLASSIFIER=manual); defaulted to prop.")

    image_uri = "data:image/jpeg;base64," + base64.b64encode(photo_jpeg).decode()
    headers = {}
    if settings.classifier_api_key:
        headers["Authorization"] = f"Bearer {settings.classifier_api_key}"
        headers["X-API-Key"] = settings.classifier_api_key  # BenchAI's header
    body = {
        "model": settings.classifier_model,
        "temperature": 0,
        "max_tokens": 10,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Classify this object. One word: prop, character, or creature."},
                    {"type": "image_url", "image_url": {"url": image_uri}},
                ],
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=settings.classifier_timeout) as client:
            resp = await client.post(f"{settings.classifier_url.rstrip('/')}/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # the pipeline keeps going with a safe default
        return ClassificationResult("prop", "default", f"Classifier failed ({type(exc).__name__}: {exc}); defaulted to prop.")

    category = parse_category(answer)
    if category is None:
        return ClassificationResult("prop", "default", f"Classifier answer {answer[:80]!r} wasn't a category; defaulted to prop.")
    return ClassificationResult(category, "classifier", answer.strip()[:200])
