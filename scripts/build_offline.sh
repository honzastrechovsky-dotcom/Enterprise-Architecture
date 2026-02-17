#!/usr/bin/env bash
# =============================================================================
# build_offline.sh — Offline Docker image build for air-gapped environments
# =============================================================================
#
# PURPOSE
# -------
# Builds the Enterprise Agent Platform Docker image without requiring internet
# access at build time. Python dependencies are pre-downloaded ("vendored") into
# ./vendor/wheels/ and baked into the image via --find-links.
#
# USAGE
# -----
#   # Step 1 (on a machine WITH internet): download deps
#   ./scripts/build_offline.sh vendor
#
#   # Step 2: transfer the project directory (including ./vendor/) to the
#   #          air-gapped host via USB drive, SCP over bastion, etc.
#
#   # Step 3 (on the air-gapped host): build the image
#   ./scripts/build_offline.sh build
#
#   # Or do both in one shot (on a machine with internet):
#   ./scripts/build_offline.sh all
#
# PRIVATE PYPI MIRROR ALTERNATIVE
# --------------------------------
# If your organisation runs a private PyPI mirror (Artifactory, Nexus,
# devpi, etc.), you can skip vendoring entirely and instead set:
#
#   export PIP_INDEX_URL=https://pypi.your-company.com/simple/
#   export PIP_TRUSTED_HOST=pypi.your-company.com
#
# Then run a normal `docker build` — pip will pull from the mirror.
# In that case this script is not needed. See:
#   https://pip.pypa.io/en/stable/topics/configuration/
#   https://www.jfrog.com/confluence/display/JFROG/PyPI+Repositories
#
# REQUIREMENTS
# ------------
#   - Docker 24+ with BuildKit enabled  (export DOCKER_BUILDKIT=1)
#   - Python 3.12 installed on the vendoring machine
#   - requirements.txt present in the project root
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENDOR_DIR="${PROJECT_ROOT}/vendor/wheels"
REQUIREMENTS_FILE="${PROJECT_ROOT}/requirements.txt"
IMAGE_NAME="${IMAGE_NAME:-enterprise-agent-platform}"
IMAGE_TAG="${IMAGE_TAG:-offline}"
DOCKERFILE="${PROJECT_ROOT}/Dockerfile"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

log() { echo "[build_offline] $*"; }
die() { echo "[build_offline] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# vendor — download all wheels into ./vendor/wheels/
# ---------------------------------------------------------------------------
cmd_vendor() {
    log "Vendoring Python dependencies from ${REQUIREMENTS_FILE}"

    [[ -f "${REQUIREMENTS_FILE}" ]] \
        || die "requirements.txt not found at ${REQUIREMENTS_FILE}"

    mkdir -p "${VENDOR_DIR}"

    # Download source distributions and binary wheels.
    # --platform / --python-version flags ensure we download the correct wheels
    # for the Docker base image (linux, amd64, cp312).
    pip download \
        --requirement "${REQUIREMENTS_FILE}" \
        --dest "${VENDOR_DIR}" \
        --platform linux_x86_64 \
        --python-version 3.12 \
        --only-binary :all: \
        --no-deps \
        2>/dev/null \
    || {
        log "Binary-only download failed for some packages — retrying with source allowed"
        # Some packages don't have binary wheels; allow source distributions too
        pip download \
            --requirement "${REQUIREMENTS_FILE}" \
            --dest "${VENDOR_DIR}"
    }

    local count
    count=$(find "${VENDOR_DIR}" -name "*.whl" -o -name "*.tar.gz" | wc -l)
    log "Vendored ${count} packages into ${VENDOR_DIR}"
    log ""
    log "Next steps:"
    log "  1. Transfer the project directory (including vendor/) to the air-gapped host"
    log "  2. Run: ./scripts/build_offline.sh build"
}

# ---------------------------------------------------------------------------
# build — build the Docker image using vendored wheels
# ---------------------------------------------------------------------------
cmd_build() {
    log "Building Docker image '${IMAGE_NAME}:${IMAGE_TAG}' using vendored wheels"

    [[ -f "${DOCKERFILE}" ]] \
        || die "Dockerfile not found at ${DOCKERFILE}"

    [[ -d "${VENDOR_DIR}" ]] \
        || die "Vendor directory not found at ${VENDOR_DIR}. Run: ./scripts/build_offline.sh vendor"

    # DOCKER_BUILDKIT=1 is required for --build-arg to be passed to RUN instructions.
    export DOCKER_BUILDKIT=1

    docker build \
        --file "${DOCKERFILE}" \
        --tag "${IMAGE_NAME}:${IMAGE_TAG}" \
        --build-arg "PIP_FIND_LINKS=/wheels" \
        --build-arg "PIP_NO_INDEX=1" \
        --no-cache \
        "${PROJECT_ROOT}"

    # Note: The Dockerfile must COPY the vendor directory and mount it at /wheels
    # during the pip install RUN step. Example Dockerfile snippet:
    #
    #   COPY vendor/wheels /wheels
    #   RUN pip install \
    #         --find-links /wheels \
    #         --no-index \
    #         --requirement requirements.txt
    #
    # If your Dockerfile doesn't already do this, add it before the pip install step.

    log "Image built successfully: ${IMAGE_NAME}:${IMAGE_TAG}"
    log ""
    log "To save for transfer:"
    log "  docker save '${IMAGE_NAME}:${IMAGE_TAG}' | gzip > ${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"
    log ""
    log "To load on the air-gapped host:"
    log "  docker load < ${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"
}

# ---------------------------------------------------------------------------
# all — vendor then build in one shot
# ---------------------------------------------------------------------------
cmd_all() {
    cmd_vendor
    cmd_build
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
ACTION="${1:-all}"

case "${ACTION}" in
    vendor) cmd_vendor ;;
    build)  cmd_build ;;
    all)    cmd_all ;;
    *)
        die "Unknown action: '${ACTION}'. Valid actions: vendor | build | all"
        ;;
esac
