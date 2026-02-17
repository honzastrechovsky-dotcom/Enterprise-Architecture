# =============================================================================
# Stage 1: Builder - Install dependencies and build artifacts
# =============================================================================
FROM python:3.12-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy dependency specification
WORKDIR /build
COPY pyproject.toml ./

# Install Python dependencies
# Use --no-cache-dir to minimize image size
# Install only production dependencies (not dev extras)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.12-slim AS runtime

# OCI Image Specification Labels
LABEL org.opencontainers.image.title="Enterprise Agent Platform"
LABEL org.opencontainers.image.description="Multi-tenant enterprise agent platform with RAG, audit logging, and OIDC auth"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.vendor="Enterprise Agent Platform"
LABEL org.opencontainers.image.licenses="LicenseRef-Proprietary"
LABEL org.opencontainers.image.source="https://github.com/enterprise-agent-platform/enterprise-agent-platform"

# Install runtime dependencies only (PostgreSQL client library)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and group
RUN groupadd -r appuser && \
    useradd -r -g appuser -u 1000 -d /app -s /sbin/nologin appuser

# Set working directory
WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
COPY --chown=appuser:appuser src/ /app/src/

# Switch to non-root user
USER appuser

# Expose application port
EXPOSE 8000

# Health check
# Check every 30s, timeout after 3s, start checking after 5s, allow 3 retries
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=2.0)" || exit 1

# Set Python to run in unbuffered mode (better for logs)
ENV PYTHONUNBUFFERED=1

# Run uvicorn
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
