"""Vision service - image analysis via LiteLLM vision models.

Supports PNG, JPEG, TIFF, BMP formats up to 20 MB. All image payloads are
base64-encoded before being sent to the vision model so they can be embedded
directly in the JSON request body.

Responsibilities:
- General-purpose image analysis with a caller-supplied prompt
- Sensitivity classification (RESTRICTED / CONFIDENTIAL / INTERNAL / PUBLIC)
- OCR-based text extraction
- Technical diagram description
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Any

import litellm
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

SUPPORTED_MIME_TYPES: dict[bytes, str] = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"II*\x00": "image/tiff",     # little-endian TIFF
    b"MM\x00*": "image/tiff",     # big-endian TIFF
    b"BM": "image/bmp",
}


class ImageSensitivity(StrEnum):
    RESTRICTED = "RESTRICTED"
    CONFIDENTIAL = "CONFIDENTIAL"
    INTERNAL = "INTERNAL"
    PUBLIC = "PUBLIC"


# Model tier -> LiteLLM model name mapping.  Callers pass a tier string so
# that the service can be wired to different model capabilities without
# leaking model names into application code.
_MODEL_FOR_TIER: dict[str, str] = {
    "standard": "gpt-4o",
    "advanced": "gpt-4o",
    "economy": "gpt-4o-mini",
}
_DEFAULT_MODEL = "gpt-4o"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ImageFormatError(ValueError):
    """Raised when an image cannot be identified or exceeds size limits."""


def _detect_mime_type(image_bytes: bytes) -> str:
    """Return the MIME type by inspecting magic bytes."""
    for magic, mime in SUPPORTED_MIME_TYPES.items():
        if image_bytes[: len(magic)] == magic:
            return mime
    raise ImageFormatError(
        "Unsupported image format. Accepted: PNG, JPEG, TIFF, BMP."
    )


def _encode_image(image_bytes: bytes) -> tuple[str, str]:
    """Return (base64_data, mime_type) for the given image bytes."""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImageFormatError(
            f"Image exceeds maximum size of {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
        )
    mime_type = _detect_mime_type(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return b64, mime_type


def _build_vision_message(
    prompt: str,
    b64_data: str,
    mime_type: str,
) -> list[dict[str, Any]]:
    """Construct the messages list for a vision model call."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{b64_data}",
                        "detail": "high",
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class VisionService:
    """Async wrapper around LiteLLM vision model calls.

    All public methods accept raw image bytes and return structured results.
    Callers are responsible for catching VisionServiceError for recoverable
    conditions.
    """

    def __init__(self, model_override: str | None = None) -> None:
        self._model_override = model_override

    def _resolve_model(self, model_tier: str) -> str:
        if self._model_override:
            return self._model_override
        return _MODEL_FOR_TIER.get(model_tier, _DEFAULT_MODEL)

    async def analyze_image(
        self,
        image_bytes: bytes,
        prompt: str,
        model_tier: str = "standard",
    ) -> dict[str, Any]:
        """Send an image to the vision model with an arbitrary prompt.

        Args:
            image_bytes: Raw image data (PNG / JPEG / TIFF / BMP, max 20 MB).
            prompt: Instruction for the vision model.
            model_tier: One of "standard", "advanced", "economy".

        Returns:
            Dict with keys:
                - ``analysis``: The model's text response.
                - ``model``: The model identifier used.
                - ``usage``: Token usage dict (prompt_tokens, completion_tokens, total_tokens).
        """
        b64_data, mime_type = _encode_image(image_bytes)
        model = self._resolve_model(model_tier)
        messages = _build_vision_message(prompt, b64_data, mime_type)

        log.info(
            "vision.analyze_image",
            model=model,
            mime_type=mime_type,
            image_size=len(image_bytes),
        )

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            max_tokens=2048,
        )

        content: str = response.choices[0].message.content or ""
        usage: dict[str, int] = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }

        return {"analysis": content, "model": model, "usage": usage}

    async def classify_image(self, image_bytes: bytes) -> dict[str, Any]:
        """Classify image sensitivity level.

        Returns:
            Dict with keys:
                - ``classification``: One of RESTRICTED / CONFIDENTIAL / INTERNAL / PUBLIC.
                - ``rationale``: Short explanation from the model.
                - ``confidence``: "high" | "medium" | "low".
        """
        classification_prompt = (
            "Analyze this image and classify its information sensitivity.\n\n"
            "Choose EXACTLY ONE of the following labels based on the content you observe:\n"
            "  RESTRICTED  - Contains credentials, private keys, SSN, passwords, or highly "
            "personal biometric data.\n"
            "  CONFIDENTIAL - Contains proprietary business data, internal financial figures, "
            "or personally identifiable information (PII).\n"
            "  INTERNAL    - Contains internal business information not suitable for public "
            "release but not critically sensitive.\n"
            "  PUBLIC      - Contains only publicly available or non-sensitive information.\n\n"
            "Respond in exactly this JSON format (no markdown fences):\n"
            '{"classification": "<LABEL>", "rationale": "<one sentence>", '
            '"confidence": "<high|medium|low>"}'
        )

        b64_data, mime_type = _encode_image(image_bytes)
        model = self._resolve_model("standard")
        messages = _build_vision_message(classification_prompt, b64_data, mime_type)

        log.info(
            "vision.classify_image",
            model=model,
            mime_type=mime_type,
            image_size=len(image_bytes),
        )

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            max_tokens=256,
            response_format={"type": "json_object"},
        )

        import json

        raw_text: str = response.choices[0].message.content or "{}"
        try:
            result: dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            log.warning("vision.classify_image.parse_error", raw=raw_text)
            result = {
                "classification": ImageSensitivity.INTERNAL,
                "rationale": "Could not parse model response; defaulting to INTERNAL.",
                "confidence": "low",
            }

        # Normalise and validate the classification label
        label = str(result.get("classification", "INTERNAL")).upper()
        valid_labels = {s.value for s in ImageSensitivity}
        if label not in valid_labels:
            label = ImageSensitivity.INTERNAL

        result["classification"] = label
        return result

    async def extract_text_ocr(self, image_bytes: bytes) -> dict[str, Any]:
        """Extract all visible text from an image using OCR via vision model.

        Returns:
            Dict with keys:
                - ``text``: Extracted text content.
                - ``word_count``: Approximate word count.
                - ``language_hint``: Detected language (best-effort).
        """
        ocr_prompt = (
            "Extract ALL text visible in this image exactly as it appears, "
            "preserving line breaks and spacing as faithfully as possible. "
            "If no text is present, respond with an empty string. "
            "Also identify the primary language of the text.\n\n"
            "Respond in exactly this JSON format (no markdown fences):\n"
            '{"text": "<extracted text>", "language_hint": "<ISO 639-1 code or \'none\'>"}'
        )

        b64_data, mime_type = _encode_image(image_bytes)
        model = self._resolve_model("standard")
        messages = _build_vision_message(ocr_prompt, b64_data, mime_type)

        log.info(
            "vision.extract_text_ocr",
            model=model,
            image_size=len(image_bytes),
        )

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )

        import json

        raw_text: str = response.choices[0].message.content or "{}"
        try:
            result: dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            log.warning("vision.extract_text_ocr.parse_error", raw=raw_text)
            result = {"text": raw_text, "language_hint": "none"}

        extracted: str = result.get("text", "")
        result["word_count"] = len(extracted.split()) if extracted.strip() else 0
        return result

    async def describe_diagram(self, image_bytes: bytes) -> dict[str, Any]:
        """Produce a structured description of a technical diagram.

        Handles flowcharts, architecture diagrams, UML, network topology, ERDs,
        sequence diagrams, and similar technical visuals.

        Returns:
            Dict with keys:
                - ``diagram_type``: Category of diagram detected.
                - ``summary``: High-level plain-English summary.
                - ``components``: List of identified components/nodes.
                - ``relationships``: List of relationships/edges between components.
                - ``notes``: Additional observations.
        """
        diagram_prompt = (
            "Analyze this technical diagram and provide a structured description.\n\n"
            "Identify:\n"
            "1. The type of diagram (flowchart, architecture, UML class, sequence, "
            "ERD, network topology, etc.).\n"
            "2. A plain-English summary of what the diagram shows.\n"
            "3. The key components, nodes, or entities.\n"
            "4. The relationships, flows, or edges between them.\n"
            "5. Any additional notes about design patterns or conventions used.\n\n"
            "Respond in exactly this JSON format (no markdown fences):\n"
            '{"diagram_type": "<type>", "summary": "<summary>", '
            '"components": ["<comp1>", ...], '
            '"relationships": ["<rel1>", ...], '
            '"notes": "<optional observations>"}'
        )

        b64_data, mime_type = _encode_image(image_bytes)
        model = self._resolve_model("advanced")
        messages = _build_vision_message(diagram_prompt, b64_data, mime_type)

        log.info(
            "vision.describe_diagram",
            model=model,
            image_size=len(image_bytes),
        )

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )

        import json

        raw_text: str = response.choices[0].message.content or "{}"
        try:
            result: dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError:
            log.warning("vision.describe_diagram.parse_error", raw=raw_text)
            result = {
                "diagram_type": "unknown",
                "summary": raw_text,
                "components": [],
                "relationships": [],
                "notes": "",
            }

        return result
