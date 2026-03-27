#!/usr/bin/env bash
# Multi-arch push so amd64 (Koyeb, most clouds) and arm64 (Graviton, Apple Silicon servers) both work.
set -euo pipefail

DOCKER_REPO="${DOCKER_REPO:-rahadarmannabid/codeoracle}"
# Default: both architectures. Koyeb often runs linux/amd64 — an arm64-only image fails there.
# Single-arch (faster local build): DOCKER_PLATFORMS=linux/amd64 ./build-and-push.sh v1.0.0
DOCKER_PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER_NAME="${BUILDX_BUILDER:-codeoracle-multiarch}"

if [ -z "${1:-}" ]; then
    VERSION="v$(date +%Y%m%d-%H%M%S)"
    echo "No version specified, using: $VERSION"
else
    VERSION="$1"
fi

echo "=========================================="
echo "Multi-arch build + push (docker buildx)"
echo "Repository: $DOCKER_REPO"
echo "Version:    $VERSION"
echo "Platforms:  $DOCKER_PLATFORMS"
echo "=========================================="

if ! docker buildx version >/dev/null 2>&1; then
    echo "❌ docker buildx not found. Install Docker Buildx or use Docker Desktop."
    exit 1
fi

if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    echo "Creating buildx builder: $BUILDER_NAME"
    docker buildx create --name "$BUILDER_NAME" --driver docker-container --bootstrap
fi
docker buildx use "$BUILDER_NAME"

echo "Log in if needed: docker login"
echo "Building and pushing manifest (per-arch layers + combined tag)..."
docker buildx build \
    --platform "$DOCKER_PLATFORMS" \
    -f Dockerfile \
    -t "${DOCKER_REPO}:${VERSION}" \
    -t "${DOCKER_REPO}:latest" \
    --push \
    .

echo ""
echo "✅ Done. Image supports: $DOCKER_PLATFORMS"
echo "   docker pull ${DOCKER_REPO}:latest"
echo "   (Runners pick amd64 or arm64 automatically.)"
echo "=========================================="
