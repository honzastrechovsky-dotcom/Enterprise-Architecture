"""OCR processor - extract text from PDFs and images.

Strategy:
1. For PDFs: use pypdf to extract the embedded text layer first.  If a page
   produces no text (i.e. it is a scanned/rasterized page), fall back to the
   vision model for OCR.
2. For images: delegate directly to VisionService.extract_text_ocr.
3. Batch mode: process a list of (filename, bytes) tuples concurrently and
   return per-file results.

Design notes:
- All I/O is async; pypdf operations are wrapped in a thread-pool executor to
  avoid blocking the event loop.
- Failed pages are recorded individually so a single bad page does not abort
  the whole document.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import structlog

from src.multimodal.vision import VisionService

log = structlog.get_logger(__name__)

# Minimum character count for a pypdf page to be considered "has text".
# Very short extractions (e.g. a lone page-number) are treated as blank and
# trigger the vision-model fallback.
_MIN_TEXT_CHARS = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _extract_pdf_page_text(pdf_bytes: bytes, page_index: int) -> str:
    """Run pypdf text extraction for a single page in a thread pool.

    Returns the extracted text, which may be an empty string for scanned pages.
    """

    def _sync_extract() -> str:
        import pypdf  # lazy import - not needed for image-only usage

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if page_index >= len(reader.pages):
            return ""
        return reader.pages[page_index].extract_text() or ""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_extract)


async def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Return the page count for a PDF without loading all content."""

    def _sync_count() -> int:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return len(reader.pages)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_count)


async def _render_pdf_page_as_png(pdf_bytes: bytes, page_index: int) -> bytes | None:
    """Render a PDF page to PNG bytes for vision-model OCR fallback.

    Returns None if the pdf2image / poppler dependency is not available, in
    which as the caller should skip OCR for this page rather than raising.
    """
    try:
        import pdf2image  # optional heavy dependency

        def _sync_render() -> bytes:
            images = pdf2image.convert_from_bytes(
                pdf_bytes,
                first_page=page_index + 1,
                last_page=page_index + 1,
                dpi=200,
                fmt="PNG",
            )
            if not images:
                return b""
            buf = io.BytesIO()
            images[0].save(buf, format="PNG")
            return buf.getvalue()

        loop = asyncio.get_running_loop()
        png_bytes = await loop.run_in_executor(None, _sync_render)
        return png_bytes if png_bytes else None

    except ImportError:
        log.warning(
            "ocr.pdf_render_unavailable",
            reason="pdf2image not installed; scanned page will be skipped",
            page_index=page_index,
        )
        return None


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class OCRProcessor:
    """Extract text from PDFs and images using hybrid extraction strategy."""

    def __init__(self, vision_service: VisionService | None = None) -> None:
        self._vision = vision_service or VisionService()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_pdf(self, pdf_bytes: bytes) -> dict[str, Any]:
        """Extract text from all pages of a PDF.

        For each page:
        - If pypdf extracts sufficient text, use that.
        - Otherwise render the page to PNG and use the vision model as OCR.

        Returns:
            Dict with keys:
                - ``page_count``: Total number of pages.
                - ``pages``: List of per-page dicts with ``page`` (1-based),
                  ``text``, ``method`` ("pdf_layer" | "vision_ocr" | "skipped"),
                  and ``char_count``.
                - ``full_text``: All page text concatenated with newlines.
                - ``word_count``: Approximate total word count.
        """
        page_count = await _count_pdf_pages(pdf_bytes)
        log.info("ocr.process_pdf.start", page_count=page_count)

        page_results: list[dict[str, Any]] = []

        for page_index in range(page_count):
            page_num = page_index + 1
            text = await _extract_pdf_page_text(pdf_bytes, page_index)

            if len(text.strip()) >= _MIN_TEXT_CHARS:
                page_results.append(
                    {
                        "page": page_num,
                        "text": text,
                        "method": "pdf_layer",
                        "char_count": len(text),
                    }
                )
                log.debug("ocr.pdf_layer_extracted", page=page_num, chars=len(text))
                continue

            # Fallback: render page as PNG then run vision OCR
            png_bytes = await _render_pdf_page_as_png(pdf_bytes, page_index)
            if png_bytes:
                try:
                    ocr_result = await self._vision.extract_text_ocr(png_bytes)
                    extracted = ocr_result.get("text", "")
                    page_results.append(
                        {
                            "page": page_num,
                            "text": extracted,
                            "method": "vision_ocr",
                            "char_count": len(extracted),
                        }
                    )
                    log.debug(
                        "ocr.vision_ocr_extracted", page=page_num, chars=len(extracted)
                    )
                except Exception as exc:
                    log.warning(
                        "ocr.vision_ocr_failed", page=page_num, error=str(exc)
                    )
                    page_results.append(
                        {
                            "page": page_num,
                            "text": "",
                            "method": "skipped",
                            "char_count": 0,
                        }
                    )
            else:
                page_results.append(
                    {
                        "page": page_num,
                        "text": text,  # whatever pypdf got (possibly empty)
                        "method": "skipped",
                        "char_count": len(text),
                    }
                )

        full_text = "\n\n".join(
            p["text"] for p in page_results if p["text"].strip()
        )
        word_count = len(full_text.split()) if full_text.strip() else 0

        return {
            "page_count": page_count,
            "pages": page_results,
            "full_text": full_text,
            "word_count": word_count,
        }

    async def process_image(self, image_bytes: bytes) -> dict[str, Any]:
        """Extract text from a single image via vision-model OCR.

        Returns:
            Dict matching VisionService.extract_text_ocr output, augmented
            with a ``source`` field set to "vision_ocr".
        """
        log.info("ocr.process_image.start", image_size=len(image_bytes))
        result = await self._vision.extract_text_ocr(image_bytes)
        result["source"] = "vision_ocr"
        return result

    async def process_batch(
        self,
        files: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """Process a batch of files concurrently.

        Args:
            files: List of (filename, file_bytes) tuples.  Each file is
                identified by its filename to infer whether to use
                process_pdf or process_image.

        Returns:
            Dict with keys:
                - ``results``: List of per-file result dicts, each with
                  ``filename``, ``status`` ("ok" | "error"), ``data``
                  (the extraction result) or ``error`` (error message).
                - ``total_files``: Count of files submitted.
                - ``succeeded``: Count of successful extractions.
                - ``failed``: Count of failed extractions.
        """
        log.info("ocr.process_batch.start", file_count=len(files))

        async def _process_one(filename: str, file_bytes: bytes) -> dict[str, Any]:
            try:
                if filename.lower().endswith(".pdf"):
                    data = await self.process_pdf(file_bytes)
                else:
                    data = await self.process_image(file_bytes)
                return {"filename": filename, "status": "ok", "data": data}
            except Exception as exc:
                log.warning("ocr.batch_file_failed", filename=filename, error=str(exc))
                return {
                    "filename": filename,
                    "status": "error",
                    "error": str(exc),
                }

        tasks = [_process_one(fname, fbytes) for fname, fbytes in files]
        results = await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if r["status"] == "ok")
        failed = len(results) - succeeded

        return {
            "results": list(results),
            "total_files": len(files),
            "succeeded": succeeded,
            "failed": failed,
        }
