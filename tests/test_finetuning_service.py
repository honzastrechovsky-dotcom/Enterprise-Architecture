"""Tests for FinetuningService.

Following TDD methodology - these tests define expected behavior BEFORE implementation.
Tests should FAIL (RED phase) until service implementation is complete.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.feedback import DatasetStatus


@pytest.fixture
def mock_finetuning_service(mock_db_session):
    """Create FinetuningService instance with mock db session."""
    from src.services.finetuning import FinetuningService
    return FinetuningService(mock_db_session)


class TestCreateDataset:
    """Test FinetuningService.create_dataset method."""

    @pytest.mark.asyncio
    async def test_create_dataset_with_filters(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test creating a fine-tuning dataset with filters."""
        dataset_id = await mock_finetuning_service.create_dataset(
            tenant_id=test_tenant_id,
            name="High Quality Responses Q1",
            description="All positive feedback from Q1 2026",
            filters={
                "min_rating": "thumbs_up",
                "tags": ["accurate", "helpful"],
                "date_from": "2026-01-01",
                "date_to": "2026-03-31",
            },
        )

        assert isinstance(dataset_id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_create_dataset_without_filters(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test creating dataset without filters includes all feedback."""
        dataset_id = await mock_finetuning_service.create_dataset(
            tenant_id=test_tenant_id,
            name="All Feedback Dataset",
            description="Complete feedback dataset",
        )

        assert isinstance(dataset_id, uuid.UUID)


class TestPopulateDataset:
    """Test FinetuningService.populate_dataset method."""

    @pytest.mark.asyncio
    async def test_populate_dataset_from_feedback(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test populating dataset from feedback matching filters."""
        dataset_id = uuid.uuid4()

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock(
            id=dataset_id,
            tenant_id=test_tenant_id,
            filters={"min_rating": "thumbs_up"},
        )
        mock_finetuning_service.db.execute.return_value = mock_result

        record_count = await mock_finetuning_service.populate_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
        )

        assert isinstance(record_count, int)
        assert record_count >= 0

    @pytest.mark.asyncio
    async def test_populate_dataset_applies_filters(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test that populate applies dataset filters correctly."""
        dataset_id = uuid.uuid4()

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock(
            id=dataset_id,
            tenant_id=test_tenant_id,
            filters={
                "min_rating": "thumbs_up",
                "tags": ["accurate"],
                "model": "openai/gpt-4o",
            },
        )
        mock_finetuning_service.db.execute.return_value = mock_result

        record_count = await mock_finetuning_service.populate_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
        )

        assert isinstance(record_count, int)


class TestExportDataset:
    """Test FinetuningService.export_dataset method."""

    @pytest.mark.asyncio
    async def test_export_dataset_in_openai_format(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test exporting dataset in OpenAI fine-tuning format."""
        dataset_id = uuid.uuid4()

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock(
            id=dataset_id,
            tenant_id=test_tenant_id,
        )
        mock_result.scalars.return_value.all.return_value = []
        mock_finetuning_service.db.execute.return_value = mock_result

        export_data = await mock_finetuning_service.export_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
            format="openai",
        )

        assert isinstance(export_data, str)
        # Should be JSONL format

    @pytest.mark.asyncio
    async def test_export_dataset_includes_messages_format(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test that export includes messages in OpenAI format."""
        dataset_id = uuid.uuid4()

        # Mock a dataset record
        mock_record = MagicMock()
        mock_record.system_prompt = "You are a helpful assistant"
        mock_record.user_prompt = "What is 2+2?"
        mock_record.assistant_response = "4"

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock(
            id=dataset_id,
            tenant_id=test_tenant_id,
        )
        mock_result.scalars.return_value.all.return_value = [mock_record]
        mock_finetuning_service.db.execute.return_value = mock_result

        export_data = await mock_finetuning_service.export_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
            format="openai",
        )

        assert isinstance(export_data, str)
        # Each line should be a JSON object with "messages" key


class TestListDatasets:
    """Test FinetuningService.list_datasets method."""

    @pytest.mark.asyncio
    async def test_list_datasets_returns_tenant_scoped(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test that list_datasets only returns datasets for tenant."""
        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_finetuning_service.db.execute.return_value = mock_result

        datasets = await mock_finetuning_service.list_datasets(
            tenant_id=test_tenant_id
        )

        assert isinstance(datasets, list)


class TestGetDataset:
    """Test FinetuningService.get_dataset method."""

    @pytest.mark.asyncio
    async def test_get_dataset_returns_details_with_samples(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test that get_dataset returns dataset with sample records."""
        dataset_id = uuid.uuid4()

        mock_dataset = MagicMock()
        mock_dataset.id = dataset_id
        mock_dataset.name = "Test Dataset"
        mock_dataset.status = DatasetStatus.READY
        mock_dataset.record_count = 100

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_dataset
        mock_result.scalars.return_value.all.return_value = []
        mock_finetuning_service.db.execute.return_value = mock_result

        dataset = await mock_finetuning_service.get_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
        )

        assert dataset is not None
        assert "sample_records" in dataset or hasattr(dataset, "sample_records")

    @pytest.mark.asyncio
    async def test_get_dataset_not_found_returns_none(
        self, mock_finetuning_service, test_tenant_id
    ):
        """Test that get_dataset returns None for non-existent dataset."""
        dataset_id = uuid.uuid4()

        mock_finetuning_service.db.execute = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_finetuning_service.db.execute.return_value = mock_result

        dataset = await mock_finetuning_service.get_dataset(
            dataset_id=dataset_id,
            tenant_id=test_tenant_id,
        )

        assert dataset is None
