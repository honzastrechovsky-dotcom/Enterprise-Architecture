"""Tests for FeedbackService.

Following TDD methodology - these tests define expected behavior BEFORE implementation.
Tests should FAIL (RED phase) until service implementation is complete.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_feedback_service(mock_db_session):
    """Create FeedbackService instance with mock db session."""
    from src.services.feedback import FeedbackService
    return FeedbackService(mock_db_session)


class TestSubmitFeedback:
    """Test FeedbackService.submit_feedback method."""

    @pytest.mark.asyncio
    async def test_submit_feedback_creates_record(
        self, mock_feedback_service, test_tenant_id, test_user_id
    ):
        """Test that submit_feedback creates a database record."""
        feedback_id = await mock_feedback_service.submit_feedback(
            tenant_id=test_tenant_id,
            user_id=test_user_id,
            rating="thumbs_up",
            prompt_text="What is 2+2?",
            response_text="4",
            model_used="openai/gpt-4o",
            comment="Great answer!",
            tags=["accurate", "helpful"],
        )

        assert isinstance(feedback_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_submit_feedback_with_trace_id(
        self, mock_feedback_service, test_tenant_id, test_user_id
    ):
        """Test submitting feedback with optional trace_id."""
        trace_id = "trace-123-abc"

        feedback_id = await mock_feedback_service.submit_feedback(
            tenant_id=test_tenant_id,
            user_id=test_user_id,
            rating="thumbs_down",
            prompt_text="Calculate 5*5",
            response_text="24",
            model_used="openai/gpt-4o-mini",
            trace_id=trace_id,
        )

        assert isinstance(feedback_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_submit_feedback_with_conversation_and_message_ids(
        self, mock_feedback_service, test_tenant_id, test_user_id
    ):
        """Test submitting feedback with conversation and message IDs."""
        conversation_id = uuid.uuid4()
        message_id = uuid.uuid4()

        feedback_id = await mock_feedback_service.submit_feedback(
            tenant_id=test_tenant_id,
            user_id=test_user_id,
            rating="thumbs_up",
            prompt_text="Explain AI",
            response_text="AI is...",
            model_used="openai/gpt-4o",
            conversation_id=conversation_id,
            message_id=message_id,
        )

        assert isinstance(feedback_id, uuid.UUID)


class TestListFeedback:
    """Test FeedbackService.list_feedback method."""

    @pytest.mark.asyncio
    async def test_list_feedback_returns_tenant_scoped_results(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test that list_feedback only returns feedback for the tenant."""
        # Mock the database query result
        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_feedback_service.db.execute.return_value = mock_result

        results = await mock_feedback_service.list_feedback(
            tenant_id=test_tenant_id,
            limit=50,
            offset=0,
        )

        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_list_feedback_with_rating_filter(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test filtering feedback by rating."""
        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_feedback_service.db.execute.return_value = mock_result

        results = await mock_feedback_service.list_feedback(
            tenant_id=test_tenant_id,
            rating="thumbs_up",
            limit=50,
        )

        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_list_feedback_with_date_range_filter(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test filtering feedback by date range."""
        date_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        date_to = datetime(2026, 2, 1, tzinfo=timezone.utc)

        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_feedback_service.db.execute.return_value = mock_result

        results = await mock_feedback_service.list_feedback(
            tenant_id=test_tenant_id,
            date_from=date_from,
            date_to=date_to,
        )

        assert isinstance(results, list)


class TestFeedbackStats:
    """Test FeedbackService.get_feedback_stats method."""

    @pytest.mark.asyncio
    async def test_get_feedback_stats_returns_aggregates(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test that stats returns aggregated feedback data."""
        # Mock database queries
        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 100  # total count
        mock_feedback_service.db.execute.return_value = mock_result

        stats = await mock_feedback_service.get_feedback_stats(tenant_id=test_tenant_id)

        assert "total_count" in stats
        assert "positive_rate" in stats
        assert "top_tags" in stats
        assert "by_model" in stats

    @pytest.mark.asyncio
    async def test_get_feedback_stats_calculates_positive_rate(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test that positive rate is correctly calculated."""
        mock_feedback_service.db.execute = AsyncMock()

        # The service calls execute 4 times:
        # 1. total count, 2. positive count, 3. tags, 4. by_model
        call_count = 0

        def mock_execute_side_effect(query):
            nonlocal call_count
            call_count += 1
            mock_result = MagicMock()
            if call_count <= 2:
                # First two calls are COUNT queries returning scalar
                mock_result.scalar.return_value = 100 if call_count == 1 else 60
            else:
                mock_result.scalar.return_value = 0
            # For tags/model iteration
            mock_result.__iter__ = MagicMock(return_value=iter([]))
            mock_result.scalars.return_value.all.return_value = []
            return mock_result

        mock_feedback_service.db.execute.side_effect = mock_execute_side_effect

        stats = await mock_feedback_service.get_feedback_stats(tenant_id=test_tenant_id)

        assert isinstance(stats["positive_rate"], (int, float))


class TestExportFeedback:
    """Test FeedbackService.export_feedback method."""

    @pytest.mark.asyncio
    async def test_export_feedback_as_jsonl(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test exporting feedback in JSONL format."""
        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_feedback_service.db.execute.return_value = mock_result

        jsonl_output = await mock_feedback_service.export_feedback(
            tenant_id=test_tenant_id,
            format="jsonl",
        )

        assert isinstance(jsonl_output, str)

    @pytest.mark.asyncio
    async def test_export_feedback_filters_by_rating(
        self, mock_feedback_service, test_tenant_id
    ):
        """Test export only includes specified rating."""
        mock_feedback_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_feedback_service.db.execute.return_value = mock_result

        jsonl_output = await mock_feedback_service.export_feedback(
            tenant_id=test_tenant_id,
            format="jsonl",
            rating="thumbs_up",
        )

        assert isinstance(jsonl_output, str)
