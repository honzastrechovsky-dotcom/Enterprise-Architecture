"""Tests for persistent fine-tuning job queue.

Covers:
- FineTuningJobRecord ORM model
- PersistentFineTuningManager job lifecycle
- Real token count calculation
- Dataset-size-driven metrics (not hardcoded)
- Job cancellation
- Backward compat: FineTuningPipeline (in-memory) still works
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.fine_tuning import FineTuningJobRecord
from src.scale.fine_tuning import (
    EvaluationMetrics,
    FineTuningConfig,
    FineTuningPipeline,
    PersistentFineTuningManager,
    TrainingStatus,
    _calculate_token_counts,
    _generate_metrics_from_dataset,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jsonl(tmp_path: Path, examples: list[dict]) -> Path:
    p = tmp_path / "dataset.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in examples))
    return p


def _make_config(tmp_path: Path, dataset_path: Path) -> FineTuningConfig:
    return FineTuningConfig(
        base_model="ollama/qwen2.5:7b",
        dataset_path=dataset_path,
        output_path=tmp_path / "model_out",
    )


# ---------------------------------------------------------------------------
# Unit tests: pure functions
# ---------------------------------------------------------------------------


class TestCalculateTokenCounts:
    """_calculate_token_counts derives real values from examples."""

    def test_short_examples(self) -> None:
        examples = [
            {"input": "hello world", "output": "hi there friend"},
            {"input": "what is AI?", "output": "artificial intelligence"},
        ]
        avg_in, avg_out = _calculate_token_counts(examples)
        assert avg_in > 0
        assert avg_out > 0

    def test_empty_list_returns_zeros(self) -> None:
        avg_in, avg_out = _calculate_token_counts([])
        assert avg_in == 0.0
        assert avg_out == 0.0

    def test_longer_output_gives_higher_avg_output(self) -> None:
        examples = [
            {"input": "x", "output": "a " * 200},
        ]
        avg_in, avg_out = _calculate_token_counts(examples)
        assert avg_out > avg_in

    def test_missing_keys_handled(self) -> None:
        examples = [{"input": "hello"}]
        avg_in, avg_out = _calculate_token_counts(examples)
        assert avg_in >= 0
        assert avg_out == 0.0


class TestGenerateMetricsFromDataset:
    """_generate_metrics_from_dataset produces realistic, size-driven results."""

    def test_small_dataset_lower_accuracy(self) -> None:
        small = _generate_metrics_from_dataset(dataset_size=10)
        large = _generate_metrics_from_dataset(dataset_size=1000)
        assert large.accuracy >= small.accuracy

    def test_accuracy_within_valid_range(self) -> None:
        m = _generate_metrics_from_dataset(dataset_size=100)
        assert 0.0 <= m.accuracy <= 1.0

    def test_perplexity_positive(self) -> None:
        m = _generate_metrics_from_dataset(dataset_size=100)
        assert m.perplexity > 0

    def test_bleu_within_range(self) -> None:
        m = _generate_metrics_from_dataset(dataset_size=100)
        assert m.bleu_score is not None
        assert 0.0 <= m.bleu_score <= 1.0

    def test_not_hardcoded(self) -> None:
        m = _generate_metrics_from_dataset(dataset_size=100)
        # Must NOT be the old hardcoded values simultaneously
        assert m.accuracy != 0.85 or m.perplexity != 12.5


# ---------------------------------------------------------------------------
# Unit tests: FineTuningJobRecord ORM model
# ---------------------------------------------------------------------------


class TestFineTuningJobRecord:
    """ORM model field defaults and repr."""

    def test_defaults(self) -> None:
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="ollama/qwen2.5:7b",
            dataset_path="/data/ds.jsonl",
            output_path="/data/out",
        )
        assert record.status == TrainingStatus.PENDING.value
        assert record.progress == 0.0
        assert record.metrics == {}
        assert record.error_message is None

    def test_repr_contains_status(self) -> None:
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="ollama/qwen2.5:7b",
            dataset_path="/data/ds.jsonl",
            output_path="/data/out",
        )
        record.id = uuid.uuid4()
        r = repr(record)
        assert "FineTuningJobRecord" in r
        assert "pending" in r


# ---------------------------------------------------------------------------
# Integration-style tests: PersistentFineTuningManager (mocked DB)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db() -> AsyncMock:
    """Minimal mock of AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture()
def manager(mock_db: AsyncMock) -> PersistentFineTuningManager:
    return PersistentFineTuningManager(db=mock_db)


class TestPersistentFineTuningManagerCreate:
    """create_job creates a record, persists it, and returns it."""

    @pytest.mark.asyncio()
    async def test_create_job_returns_record(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        examples = [{"input": f"q{i}", "output": f"a{i}"} for i in range(15)]
        ds = _make_jsonl(tmp_path, examples)
        config = _make_config(tmp_path, ds)

        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Stub out background pipeline so it doesn't actually run
        manager._run_training_pipeline = AsyncMock()  # type: ignore[method-assign]

        record = await manager.create_job(
            tenant_id=tenant_id,
            user_id=user_id,
            config=config,
        )

        assert record.tenant_id == tenant_id
        assert record.user_id == user_id
        assert record.status == TrainingStatus.PENDING.value
        assert record.base_model == config.base_model
        manager.db.add.assert_called_once()
        manager.db.flush.assert_called()

    @pytest.mark.asyncio()
    async def test_create_job_invalid_dataset_raises(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        examples = [{"input": "q", "output": "a"} for _ in range(5)]
        ds = _make_jsonl(tmp_path, examples)
        config = _make_config(tmp_path, ds)

        with pytest.raises(ValueError, match="validation failed"):
            await manager.create_job(
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                config=config,
            )


class TestPersistentFineTuningManagerGetStatus:
    """get_job_status fetches from DB by id."""

    @pytest.mark.asyncio()
    async def test_get_existing_job(
        self, manager: PersistentFineTuningManager
    ) -> None:
        job_id = uuid.uuid4()
        expected = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path="/d",
            output_path="/o",
        )
        expected.id = job_id

        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=expected)
        manager.db.execute = AsyncMock(return_value=result_mock)

        fetched = await manager.get_job_status(job_id)
        assert fetched.id == job_id

    @pytest.mark.asyncio()
    async def test_get_missing_job_raises(
        self, manager: PersistentFineTuningManager
    ) -> None:
        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        manager.db.execute = AsyncMock(return_value=result_mock)

        with pytest.raises(ValueError, match="not found"):
            await manager.get_job_status(uuid.uuid4())


class TestPersistentFineTuningManagerCancel:
    """cancel_job sets status to CANCELLED."""

    @pytest.mark.asyncio()
    async def test_cancel_pending_job(
        self, manager: PersistentFineTuningManager
    ) -> None:
        job_id = uuid.uuid4()
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path="/d",
            output_path="/o",
        )
        record.id = job_id
        record.status = TrainingStatus.PENDING.value

        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=record)
        manager.db.execute = AsyncMock(return_value=result_mock)

        cancelled = await manager.cancel_job(job_id)
        assert cancelled.status == TrainingStatus.CANCELLED.value

    @pytest.mark.asyncio()
    async def test_cancel_completed_job_raises(
        self, manager: PersistentFineTuningManager
    ) -> None:
        job_id = uuid.uuid4()
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path="/d",
            output_path="/o",
        )
        record.id = job_id
        record.status = TrainingStatus.COMPLETED.value

        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=record)
        manager.db.execute = AsyncMock(return_value=result_mock)

        with pytest.raises(ValueError, match="cannot cancel"):
            await manager.cancel_job(job_id)


class TestTrainingPipelineStages:
    """_process_training_job progresses through all stages."""

    @pytest.mark.asyncio()
    async def test_pipeline_completes_all_stages(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        examples = [{"input": f"q{i}", "output": f"a{i}"} for i in range(20)]
        ds = _make_jsonl(tmp_path, examples)

        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path=str(ds),
            output_path=str(tmp_path / "out"),
        )
        record.id = uuid.uuid4()

        stages_seen: list[str] = []

        async def _flush() -> None:
            stages_seen.append(record.status)

        manager.db.flush = _flush  # type: ignore[method-assign]

        await manager._process_training_job(record, step_delay=0.0)

        assert TrainingStatus.PREPARING.value in stages_seen
        assert TrainingStatus.TRAINING.value in stages_seen
        assert TrainingStatus.EVALUATING.value in stages_seen
        assert record.status == TrainingStatus.COMPLETED.value
        assert record.progress == 100.0
        assert record.completed_at is not None

    @pytest.mark.asyncio()
    async def test_pipeline_respects_cancellation(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        examples = [{"input": f"q{i}", "output": f"a{i}"} for i in range(20)]
        ds = _make_jsonl(tmp_path, examples)

        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path=str(ds),
            output_path=str(tmp_path / "out"),
        )
        record.id = uuid.uuid4()
        record.status = TrainingStatus.CANCELLED.value  # pre-cancelled

        await manager._process_training_job(record, step_delay=0.0)

        assert record.status == TrainingStatus.CANCELLED.value

    @pytest.mark.asyncio()
    async def test_pipeline_calculates_real_token_counts(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        long_input = "what is the meaning of life? " * 20
        long_output = "The answer is forty-two according to Douglas Adams. " * 30
        examples = [{"input": long_input, "output": long_output}] * 15
        ds = _make_jsonl(tmp_path, examples)

        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path=str(ds),
            output_path=str(tmp_path / "out"),
        )
        record.id = uuid.uuid4()

        await manager._process_training_job(record, step_delay=0.0)

        assert "avg_input_tokens" in record.metrics
        assert "avg_output_tokens" in record.metrics
        assert record.metrics["avg_input_tokens"] != 50.0
        assert record.metrics["avg_output_tokens"] != 150.0
        assert record.metrics["avg_output_tokens"] > record.metrics["avg_input_tokens"]


class TestEvaluateModel:
    """evaluate_model uses real dataset-driven metrics."""

    @pytest.mark.asyncio()
    async def test_evaluate_completed_job(
        self, manager: PersistentFineTuningManager, tmp_path: Path
    ) -> None:
        job_id = uuid.uuid4()
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path="/d",
            output_path="/o",
        )
        record.id = job_id
        record.status = TrainingStatus.COMPLETED.value
        record.metrics = {"dataset_size": 50}

        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=record)
        manager.db.execute = AsyncMock(return_value=result_mock)

        examples = [{"input": f"q{i}", "output": f"a{i}"} for i in range(50)]
        test_set = _make_jsonl(tmp_path, examples)

        metrics = await manager.evaluate_model(job_id, test_set)

        assert isinstance(metrics, EvaluationMetrics)
        assert 0.0 <= metrics.accuracy <= 1.0
        assert metrics.perplexity > 0
        assert metrics.test_examples == 50
        # Evaluation results are stored on the record
        assert record.evaluation_json is not None
        assert "accuracy" in record.evaluation_json

    @pytest.mark.asyncio()
    async def test_evaluate_not_completed_raises(
        self, manager: PersistentFineTuningManager
    ) -> None:
        job_id = uuid.uuid4()
        record = FineTuningJobRecord(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            base_model="test",
            dataset_path="/d",
            output_path="/o",
        )
        record.id = job_id
        record.status = TrainingStatus.TRAINING.value

        result_mock = AsyncMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=record)
        manager.db.execute = AsyncMock(return_value=result_mock)

        with pytest.raises(ValueError, match="completed"):
            await manager.evaluate_model(job_id, Path("/fake/path.jsonl"))


# ---------------------------------------------------------------------------
# Backward compat: FineTuningPipeline (in-memory) still works
# ---------------------------------------------------------------------------


class TestFineTuningPipelineBackwardCompat:
    """FineTuningPipeline keeps the old API intact."""

    def test_class_exists(self) -> None:
        from src.scale.fine_tuning import FineTuningPipeline

        assert FineTuningPipeline is not None

    def test_instantiation(self) -> None:
        db = MagicMock()
        pipeline = FineTuningPipeline(db=db)
        assert hasattr(pipeline, "create_training_job")
        assert hasattr(pipeline, "get_job_status")
        assert hasattr(pipeline, "evaluate_model")
        assert hasattr(pipeline, "prepare_dataset")
        assert hasattr(pipeline, "validate_dataset")
        assert hasattr(pipeline, "cancel_job")

    @pytest.mark.asyncio()
    async def test_prepare_dataset_real_token_counts(
        self, tmp_path: Path
    ) -> None:
        """DatasetStats.avg_input_tokens should not be hardcoded 50."""
        db = AsyncMock()

        msg_user = MagicMock()
        msg_user.role = "user"
        msg_user.content = "What is the capital of France? " * 10

        msg_asst = MagicMock()
        msg_asst.role = "assistant"
        msg_asst.content = "Paris is the capital of France. " * 20

        conv = MagicMock()
        conv.metadata_ = {}
        conv.messages = [msg_user, msg_asst]

        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=conv)
        db.execute = AsyncMock(return_value=result_mock)

        pipeline = FineTuningPipeline(db=db)
        out = tmp_path / "out.jsonl"
        stats = await pipeline.prepare_dataset(
            tenant_id=uuid.uuid4(),
            conversation_ids=[uuid.uuid4()],
            output_path=out,
        )

        assert stats.avg_input_tokens != 50.0
        assert stats.avg_output_tokens != 150.0
        assert stats.avg_input_tokens > 0
        assert stats.avg_output_tokens > 0
        assert stats.avg_output_tokens > stats.avg_input_tokens
