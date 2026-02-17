"""Multi-modal API endpoints.

All endpoints require authentication and accept multipart/form-data file
uploads up to 50 MB.  File type validation is enforced by inspecting
magic bytes rather than relying solely on the declared content-type.

Prefix: /api/v1/multimodal

Routes:
    POST /analyze   - General image analysis with a custom prompt
    POST /ocr       - Text extraction from images or PDFs
    POST /classify  - Image sensitivity classification
    POST /describe  - Technical diagram description
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from src.auth.dependencies import AuthenticatedUser, get_current_user
from src.multimodal.ocr import OCRProcessor
from src.multimodal.vision import (
    ImageFormatError,
    VisionService,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/multimodal", tags=["multimodal"])

# 50 MB hard limit on all uploads
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Accepted MIME types for image endpoints (non-PDF)
_ACCEPTED_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
}

# Accepted MIME types for OCR endpoint (image + PDF)
_ACCEPTED_OCR_TYPES = _ACCEPTED_IMAGE_TYPES | {"application/pdf"}


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AnalyzeResponse(BaseModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    analysis: str
    model: str
    usage: dict[str, int]


class OCRResponse(BaseModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    # Image OCR fields
    text: str | None = None
    word_count: int | None = None
    language_hint: str | None = None
    source: str | None = None
    # PDF OCR additional fields
    page_count: int | None = None
    pages: list[dict[str, Any]] | None = None
    full_text: str | None = None


class ClassifyResponse(BaseModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    classification: str
    rationale: str
    confidence: str


class DescribeResponse(BaseModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    diagram_type: str
    summary: str
    components: list[str]
    relationships: list[str]
    notes: str


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _read_upload(
    file: UploadFile,
    accepted_types: set[str],
) -> bytes:
    """Read upload bytes with size and format validation.

    Raises HTTP 413 if the file exceeds _MAX_UPLOAD_BYTES.
    Raises HTTP 415 if the content-type is not in accepted_types.
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in accepted_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported media type '{content_type}'. "
                f"Accepted: {', '.join(sorted(accepted_types))}"
            ),
        )

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File size {len(raw) // (1024 * 1024)} MB exceeds the "
                f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            ),
        )
    return raw


def _handle_image_format_error(exc: ImageFormatError) -> None:
    """Re-raise an ImageFormatError as an HTTP 422 response."""
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=str(exc),
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_vision_service() -> VisionService:
    return VisionService()


def get_ocr_processor() -> OCRProcessor:
    return OCRProcessor()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="Analyze image with a custom prompt",
    status_code=status.HTTP_200_OK,
)
async def analyze_image(
    file: UploadFile = File(..., description="Image file (PNG, JPEG, TIFF, BMP)"),
    prompt: str = Form(..., description="Instruction for the vision model"),
    model_tier: str = Form(
        default="standard",
        description="Model capability tier: standard | advanced | economy",
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    vision_service: VisionService = Depends(get_vision_service),
) -> AnalyzeResponse:
    """Analyze an image using a vision model with a custom prompt.

    Accepts multipart/form-data with:
    - ``file``: Image upload (PNG, JPEG, TIFF, BMP; max 50 MB)
    - ``prompt``: Text instruction describing the analysis to perform
    - ``model_tier``: Optional tier selection (default: standard)
    """
    image_bytes = await _read_upload(file, _ACCEPTED_IMAGE_TYPES)

    log.info(
        "multimodal.analyze_image",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        filename=file.filename,
        size=len(image_bytes),
        model_tier=model_tier,
    )

    try:
        result = await vision_service.analyze_image(image_bytes, prompt, model_tier)
    except ImageFormatError as exc:
        _handle_image_format_error(exc)

    return AnalyzeResponse(
        analysis=result["analysis"],
        model=result["model"],
        usage=result["usage"],
    )


@router.post(
    "/ocr",
    response_model=OCRResponse,
    summary="Extract text from an image or PDF",
    status_code=status.HTTP_200_OK,
)
async def extract_text(
    file: UploadFile = File(
        ..., description="Image (PNG, JPEG, TIFF, BMP) or PDF file"
    ),
    current_user: AuthenticatedUser = Depends(get_current_user),
    ocr_processor: OCRProcessor = Depends(get_ocr_processor),
) -> OCRResponse:
    """Extract all text from an image or PDF using OCR.

    For PDFs, text is extracted from the embedded text layer first; scanned
    pages fall back to vision-model OCR.

    Accepts multipart/form-data with:
    - ``file``: Image or PDF upload (max 50 MB)
    """
    file_bytes = await _read_upload(file, _ACCEPTED_OCR_TYPES)
    filename = file.filename or "upload"

    log.info(
        "multimodal.ocr",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        filename=filename,
        size=len(file_bytes),
    )

    try:
        if filename.lower().endswith(".pdf"):
            result = await ocr_processor.process_pdf(file_bytes)
            return OCRResponse(
                full_text=result["full_text"],
                page_count=result["page_count"],
                pages=result["pages"],
                word_count=result["word_count"],
            )
        else:
            result = await ocr_processor.process_image(file_bytes)
            return OCRResponse(
                text=result.get("text", ""),
                word_count=result.get("word_count", 0),
                language_hint=result.get("language_hint"),
                source=result.get("source"),
            )
    except ImageFormatError as exc:
        _handle_image_format_error(exc)

    # This line is unreachable but satisfies the type checker
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post(
    "/classify",
    response_model=ClassifyResponse,
    summary="Classify image sensitivity",
    status_code=status.HTTP_200_OK,
)
async def classify_image(
    file: UploadFile = File(..., description="Image file (PNG, JPEG, TIFF, BMP)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    vision_service: VisionService = Depends(get_vision_service),
) -> ClassifyResponse:
    """Classify the sensitivity level of image content.

    Returns one of: RESTRICTED, CONFIDENTIAL, INTERNAL, PUBLIC.

    Accepts multipart/form-data with:
    - ``file``: Image upload (max 50 MB)
    """
    image_bytes = await _read_upload(file, _ACCEPTED_IMAGE_TYPES)

    log.info(
        "multimodal.classify_image",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        filename=file.filename,
        size=len(image_bytes),
    )

    try:
        result = await vision_service.classify_image(image_bytes)
    except ImageFormatError as exc:
        _handle_image_format_error(exc)

    return ClassifyResponse(
        classification=result["classification"],
        rationale=result.get("rationale", ""),
        confidence=result.get("confidence", "medium"),
    )


@router.post(
    "/describe",
    response_model=DescribeResponse,
    summary="Describe a technical diagram",
    status_code=status.HTTP_200_OK,
)
async def describe_diagram(
    file: UploadFile = File(..., description="Technical diagram image (PNG, JPEG, TIFF, BMP)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    vision_service: VisionService = Depends(get_vision_service),
) -> DescribeResponse:
    """Generate a structured description of a technical diagram.

    Handles flowcharts, architecture diagrams, UML, ERDs, network topology,
    sequence diagrams, and other technical visuals.

    Accepts multipart/form-data with:
    - ``file``: Diagram image upload (max 50 MB)
    """
    image_bytes = await _read_upload(file, _ACCEPTED_IMAGE_TYPES)

    log.info(
        "multimodal.describe_diagram",
        user_id=str(current_user.id),
        tenant_id=str(current_user.tenant_id),
        filename=file.filename,
        size=len(image_bytes),
    )

    try:
        result = await vision_service.describe_diagram(image_bytes)
    except ImageFormatError as exc:
        _handle_image_format_error(exc)

    return DescribeResponse(
        diagram_type=result.get("diagram_type", "unknown"),
        summary=result.get("summary", ""),
        components=result.get("components", []),
        relationships=result.get("relationships", []),
        notes=result.get("notes", ""),
    )
