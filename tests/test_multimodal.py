"""Tests for Multi-Modal Support.

Covers:
- VisionService: analyze_image, classify_image, extract_text_ocr, describe_diagram
- OCRProcessor: process_image, process_pdf, process_batch
- API endpoints: /multimodal/analyze, /ocr, /classify, /describe
- File size validation (reject >50 MB)
- Format validation (supported vs unsupported types)
- Error handling paths
"""

from __future__ import annotations

import io
import json
import struct
import uuid
import zlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.multimodal.vision import (
    ImageFormatError,
    ImageSensitivity,
    VisionService,
    _detect_mime_type,
    _encode_image,
)
from src.multimodal.ocr import OCRProcessor

# ---------------------------------------------------------------------------
# Test image fixtures
# ---------------------------------------------------------------------------

# Minimal valid 1x1 PNG (89 bytes)
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR = (
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01"  # width = 1
    b"\x00\x00\x00\x01"  # height = 1
    b"\x08\x02"           # bit depth=8, color type=2 (RGB)
    b"\x00\x00\x00"       # compression, filter, interlace
)
_PNG_IHDR_CRC = struct.pack(">I", zlib.crc32(b"IHDR" + _PNG_IHDR[8:]) & 0xFFFFFFFF)
_PNG_IDAT = b"\x00\x00\x00\nIDAT\x08\xd7c\xf8\x0f\x00\x00\x11\x00\x01"
_PNG_IDAT_CRC = struct.pack(">I", zlib.crc32(b"IDAT\x08\xd7c\xf8\x0f\x00") & 0xFFFFFFFF)
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"

MINIMAL_PNG = _PNG_HEADER + _PNG_IHDR + _PNG_IHDR_CRC + _PNG_IDAT + _PNG_IDAT_CRC + _PNG_IEND

# Minimal JPEG (SOI + EOI markers)
MINIMAL_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"

# Minimal BMP header (14-byte file header + 40-byte DIB header)
MINIMAL_BMP = (
    b"BM"                            # signature
    + struct.pack("<I", 54)          # file size
    + b"\x00\x00\x00\x00"           # reserved
    + struct.pack("<I", 54)          # pixel data offset
    + struct.pack("<I", 40)          # DIB header size
    + struct.pack("<i", 1)           # width
    + struct.pack("<i", 1)           # height
    + struct.pack("<H", 1)           # planes
    + struct.pack("<H", 24)          # bits per pixel
    + b"\x00" * 24                  # rest of DIB header
)


def _make_mock_litellm_response(content: str) -> MagicMock:
    """Create a mock LiteLLM completion response."""
    mock_message = MagicMock()
    mock_message.content = content
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 100
    mock_usage.completion_tokens = 50
    mock_usage.total_tokens = 150
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage
    return mock_response


# ---------------------------------------------------------------------------
# VisionService unit tests
# ---------------------------------------------------------------------------


class TestImageFormatDetection:
    def test_detect_png(self) -> None:
        assert _detect_mime_type(MINIMAL_PNG) == "image/png"

    def test_detect_jpeg(self) -> None:
        assert _detect_mime_type(MINIMAL_JPEG) == "image/jpeg"

    def test_detect_bmp(self) -> None:
        assert _detect_mime_type(MINIMAL_BMP) == "image/bmp"

    def test_detect_tiff_little_endian(self) -> None:
        tiff_le = b"II*\x00" + b"\x00" * 100
        assert _detect_mime_type(tiff_le) == "image/tiff"

    def test_detect_tiff_big_endian(self) -> None:
        tiff_be = b"MM\x00*" + b"\x00" * 100
        assert _detect_mime_type(tiff_be) == "image/tiff"

    def test_unsupported_format_raises(self) -> None:
        with pytest.raises(ImageFormatError, match="Unsupported image format"):
            _detect_mime_type(b"GIF89a...")

    def test_empty_bytes_raises(self) -> None:
        with pytest.raises(ImageFormatError):
            _detect_mime_type(b"")


class TestEncodeImage:
    def test_valid_png_returns_base64(self) -> None:
        b64, mime = _encode_image(MINIMAL_PNG)
        assert mime == "image/png"
        import base64
        decoded = base64.b64decode(b64)
        assert decoded == MINIMAL_PNG

    def test_oversized_image_raises(self) -> None:
        # 21 MB > 20 MB limit
        oversized = b"\x89PNG" + b"\x00" * (21 * 1024 * 1024)
        with pytest.raises(ImageFormatError, match="exceeds maximum size"):
            _encode_image(oversized)

    def test_exactly_20mb_allowed(self) -> None:
        # Exactly at the limit should not raise (mime detection uses first bytes)
        at_limit = b"\x89PNG" + b"\x00" * (20 * 1024 * 1024 - 4)
        b64, mime = _encode_image(at_limit)
        assert mime == "image/png"


class TestVisionServiceAnalyze:
    @pytest.mark.asyncio
    async def test_analyze_image_success(self) -> None:
        service = VisionService()
        mock_resp = _make_mock_litellm_response("The image shows a cat.")

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.analyze_image(MINIMAL_PNG, "What is in this image?")

        assert result["analysis"] == "The image shows a cat."
        assert result["model"] == "gpt-4o"
        assert result["usage"]["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_analyze_uses_model_tier(self) -> None:
        service = VisionService()
        mock_resp = _make_mock_litellm_response("analysis")

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await service.analyze_image(MINIMAL_PNG, "analyze", model_tier="economy")

        call_kwargs = mock_call.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_analyze_model_override(self) -> None:
        service = VisionService(model_override="claude-3-5-sonnet")
        mock_resp = _make_mock_litellm_response("result")

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_call:
            await service.analyze_image(MINIMAL_PNG, "analyze")

        assert mock_call.call_args.kwargs["model"] == "claude-3-5-sonnet"

    @pytest.mark.asyncio
    async def test_analyze_unsupported_format_raises(self) -> None:
        service = VisionService()
        with pytest.raises(ImageFormatError):
            await service.analyze_image(b"NOTANIMAGE", "describe")


class TestVisionServiceClassify:
    @pytest.mark.asyncio
    async def test_classify_returns_valid_label(self) -> None:
        service = VisionService()
        model_json = json.dumps({
            "classification": "CONFIDENTIAL",
            "rationale": "Contains PII data.",
            "confidence": "high",
        })
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.classify_image(MINIMAL_PNG)

        assert result["classification"] == "CONFIDENTIAL"
        assert result["confidence"] == "high"
        assert "rationale" in result

    @pytest.mark.asyncio
    async def test_classify_normalises_lowercase_label(self) -> None:
        service = VisionService()
        model_json = json.dumps({
            "classification": "restricted",  # lowercase from model
            "rationale": "Passwords visible.",
            "confidence": "high",
        })
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.classify_image(MINIMAL_PNG)

        assert result["classification"] == "RESTRICTED"

    @pytest.mark.asyncio
    async def test_classify_invalid_label_defaults_to_internal(self) -> None:
        service = VisionService()
        model_json = json.dumps({
            "classification": "UNKNOWN_LABEL",
            "rationale": "Unknown.",
            "confidence": "low",
        })
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.classify_image(MINIMAL_PNG)

        assert result["classification"] == ImageSensitivity.INTERNAL

    @pytest.mark.asyncio
    async def test_classify_malformed_json_defaults_to_internal(self) -> None:
        service = VisionService()
        mock_resp = _make_mock_litellm_response("not json at all")

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.classify_image(MINIMAL_PNG)

        assert result["classification"] == ImageSensitivity.INTERNAL
        assert result["confidence"] == "low"


class TestVisionServiceOCR:
    @pytest.mark.asyncio
    async def test_extract_text_success(self) -> None:
        service = VisionService()
        model_json = json.dumps({"text": "Hello World", "language_hint": "en"})
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.extract_text_ocr(MINIMAL_PNG)

        assert result["text"] == "Hello World"
        assert result["word_count"] == 2
        assert result["language_hint"] == "en"

    @pytest.mark.asyncio
    async def test_extract_text_empty_image(self) -> None:
        service = VisionService()
        model_json = json.dumps({"text": "", "language_hint": "none"})
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.extract_text_ocr(MINIMAL_JPEG)

        assert result["text"] == ""
        assert result["word_count"] == 0


class TestVisionServiceDescribe:
    @pytest.mark.asyncio
    async def test_describe_diagram_success(self) -> None:
        service = VisionService()
        model_json = json.dumps({
            "diagram_type": "architecture",
            "summary": "Three-tier web application architecture.",
            "components": ["Load Balancer", "App Server", "Database"],
            "relationships": ["LB routes to App", "App queries DB"],
            "notes": "Uses microservices pattern.",
        })
        mock_resp = _make_mock_litellm_response(model_json)

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.describe_diagram(MINIMAL_PNG)

        assert result["diagram_type"] == "architecture"
        assert len(result["components"]) == 3
        assert "Load Balancer" in result["components"]

    @pytest.mark.asyncio
    async def test_describe_diagram_malformed_json(self) -> None:
        service = VisionService()
        mock_resp = _make_mock_litellm_response("not valid json")

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
            result = await service.describe_diagram(MINIMAL_PNG)

        assert result["diagram_type"] == "unknown"
        assert result["components"] == []


# ---------------------------------------------------------------------------
# OCRProcessor unit tests
# ---------------------------------------------------------------------------


class TestOCRProcessorImage:
    @pytest.mark.asyncio
    async def test_process_image_delegates_to_vision(self) -> None:
        mock_vision = AsyncMock(spec=VisionService)
        mock_vision.extract_text_ocr.return_value = {
            "text": "Sample text",
            "word_count": 2,
            "language_hint": "en",
        }
        processor = OCRProcessor(vision_service=mock_vision)
        result = await processor.process_image(MINIMAL_PNG)

        assert result["text"] == "Sample text"
        assert result["source"] == "vision_ocr"
        mock_vision.extract_text_ocr.assert_awaited_once_with(MINIMAL_PNG)


class TestOCRProcessorPDF:
    @pytest.mark.asyncio
    async def test_process_pdf_text_layer(self) -> None:
        """PDF with extractable text should use pdf_layer method."""
        mock_vision = AsyncMock(spec=VisionService)
        processor = OCRProcessor(vision_service=mock_vision)

        # Patch internal helpers
        with (
            patch(
                "src.multimodal.ocr._count_pdf_pages",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "src.multimodal.ocr._extract_pdf_page_text",
                new_callable=AsyncMock,
                side_effect=["Page one content here.", "Page two content here."],
            ),
        ):
            result = await processor.process_pdf(b"%PDF-1.4 fake")

        assert result["page_count"] == 2
        assert result["pages"][0]["method"] == "pdf_layer"
        assert result["pages"][1]["method"] == "pdf_layer"
        assert "Page one" in result["full_text"]
        assert result["word_count"] > 0
        # Vision model was not called for text-layer pages
        mock_vision.extract_text_ocr.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_process_pdf_ocr_fallback(self) -> None:
        """Scanned pages (no text) should fall back to vision OCR."""
        mock_vision = AsyncMock(spec=VisionService)
        mock_vision.extract_text_ocr.return_value = {
            "text": "Scanned text extracted.",
            "word_count": 3,
            "language_hint": "en",
        }
        processor = OCRProcessor(vision_service=mock_vision)

        with (
            patch(
                "src.multimodal.ocr._count_pdf_pages",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "src.multimodal.ocr._extract_pdf_page_text",
                new_callable=AsyncMock,
                return_value="",  # no text layer
            ),
            patch(
                "src.multimodal.ocr._render_pdf_page_as_png",
                new_callable=AsyncMock,
                return_value=MINIMAL_PNG,
            ),
        ):
            result = await processor.process_pdf(b"%PDF-1.4 fake")

        assert result["pages"][0]["method"] == "vision_ocr"
        assert "Scanned text" in result["full_text"]


class TestOCRProcessorBatch:
    @pytest.mark.asyncio
    async def test_process_batch_mixed(self) -> None:
        mock_vision = AsyncMock(spec=VisionService)
        mock_vision.extract_text_ocr.return_value = {
            "text": "image text",
            "word_count": 2,
            "language_hint": "en",
        }
        processor = OCRProcessor(vision_service=mock_vision)

        files = [
            ("photo.png", MINIMAL_PNG),
            ("another.jpg", MINIMAL_JPEG),
        ]
        result = await processor.process_batch(files)

        assert result["total_files"] == 2
        assert result["succeeded"] == 2
        assert result["failed"] == 0
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_process_batch_handles_failures_gracefully(self) -> None:
        mock_vision = AsyncMock(spec=VisionService)
        mock_vision.extract_text_ocr.side_effect = [
            {"text": "ok", "word_count": 1, "language_hint": "en"},
            Exception("Model unavailable"),
        ]
        processor = OCRProcessor(vision_service=mock_vision)

        files = [("a.png", MINIMAL_PNG), ("b.jpg", MINIMAL_JPEG)]
        result = await processor.process_batch(files)

        assert result["total_files"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        statuses = {r["filename"]: r["status"] for r in result["results"]}
        assert statuses["a.png"] == "ok"
        assert statuses["b.jpg"] == "error"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_multimodal():
    """Create a minimal FastAPI app with the multimodal router and mocked auth."""
    from fastapi import FastAPI
    from src.api.multimodal import router as mm_router
    from src.auth.dependencies import get_current_user
    from src.models.user import User, UserRole

    app = FastAPI()

    # Stub user
    stub_user_id = uuid.uuid4()
    stub_tenant_id = uuid.uuid4()
    stub_user = MagicMock()
    stub_user.id = stub_user_id
    stub_user.tenant_id = stub_tenant_id

    from src.auth.dependencies import AuthenticatedUser

    stub_auth_user = AuthenticatedUser(user=stub_user, claims={})

    app.dependency_overrides[get_current_user] = lambda: stub_auth_user
    app.include_router(mm_router, prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_analyze_endpoint_success(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    mock_resp = _make_mock_litellm_response("Analysis result")

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/multimodal/analyze",
                files={"file": ("test.png", MINIMAL_PNG, "image/png")},
                data={"prompt": "What is this?", "model_tier": "standard"},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["analysis"] == "Analysis result"
    assert "model" in data
    assert "usage" in data


@pytest.mark.asyncio
async def test_analyze_endpoint_rejects_oversized_file(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    # 51 MB file
    oversized = b"\x89PNG" + b"\x00" * (51 * 1024 * 1024)

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/multimodal/analyze",
            files={"file": ("big.png", oversized, "image/png")},
            data={"prompt": "analyze"},
        )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_analyze_endpoint_rejects_unsupported_format(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/multimodal/analyze",
            files={"file": ("doc.txt", b"hello world", "text/plain")},
            data={"prompt": "analyze"},
        )

    assert response.status_code == 415


@pytest.mark.asyncio
async def test_classify_endpoint_success(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    model_json = json.dumps({
        "classification": "PUBLIC",
        "rationale": "No sensitive data.",
        "confidence": "high",
    })
    mock_resp = _make_mock_litellm_response(model_json)

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/multimodal/classify",
                files={"file": ("img.png", MINIMAL_PNG, "image/png")},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["classification"] == "PUBLIC"
    assert data["confidence"] == "high"


@pytest.mark.asyncio
async def test_ocr_endpoint_image(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    model_json = json.dumps({"text": "Hello OCR", "language_hint": "en"})
    mock_resp = _make_mock_litellm_response(model_json)

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/multimodal/ocr",
                files={"file": ("scan.jpg", MINIMAL_JPEG, "image/jpeg")},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Hello OCR"


@pytest.mark.asyncio
async def test_describe_endpoint_success(app_with_multimodal):
    import httpx
    from httpx import AsyncClient

    model_json = json.dumps({
        "diagram_type": "flowchart",
        "summary": "Decision flow.",
        "components": ["Start", "Decision", "End"],
        "relationships": ["Start->Decision", "Decision->End"],
        "notes": "",
    })
    mock_resp = _make_mock_litellm_response(model_json)

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app_with_multimodal), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/multimodal/describe",
                files={"file": ("diagram.png", MINIMAL_PNG, "image/png")},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["diagram_type"] == "flowchart"
    assert "Start" in data["components"]
