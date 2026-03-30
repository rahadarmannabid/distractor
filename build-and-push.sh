#!/usr/bin/env bash
# Docker image: local build OR multi-arch push (Koyeb / registry).
# Run from repo root: ./build-and-push.sh
set -euo pipefail

DOCKER_REPO="${DOCKER_REPO:-rahadarmannabid/codeoracle}"
# Default: both architectures. Koyeb often runs linux/amd64 — an arm64-only image fails there.
# Faster local multi-arch push: DOCKER_PLATFORMS=linux/amd64 ./build-and-push.sh v1.0.0
DOCKER_PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER_NAME="${BUILDX_BUILDER:-codeoracle-multiarch}"
# Set to 1 to force a clean dependency layer: DOCKER_BUILD_NO_CACHE=1 ./build-and-push.sh local
DOCKER_BUILD_NO_CACHE="${DOCKER_BUILD_NO_CACHE:-0}"

usage() {
    cat <<'EOF'

Usage:
  ./build-and-push.sh local [tag]
      Build on this machine only (plain docker build, no registry push).
      Default tag: latest. Example: ./build-and-push.sh local dev

  ./build-and-push.sh [version]
      Multi-arch buildx build + push to DOCKER_REPO.
      If version is omitted, uses vYYYYMMDD-HHMMSS.

Environment:
  DOCKER_REPO              Image name (default: rahadarmannabid/codeoracle)
  DOCKER_PLATFORMS         e.g. linux/amd64 for faster push
  DOCKER_BUILD_NO_CACHE=1  Full rebuild without cache (local or push)
  BUILDX_BUILDER           buildx builder name (default: codeoracle-multiarch)

Examples:
  ./build-and-push.sh local
  DOCKER_BUILD_NO_CACHE=1 ./build-and-push.sh local
  ./build-and-push.sh v1.2.3
  DOCKER_PLATFORMS=linux/amd64 ./build-and-push.sh
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

# ─── Local build (this machine only, no push) ─────────────────────────────
# Note: avoid empty ${arr[@]} with set -u (macOS bash 3.2 errors) — branch on --no-cache instead.
if [ "${1:-}" = "local" ]; then
    TAG="${2:-latest}"
    echo "=========================================="
    echo "Local Docker build (current architecture)"
    echo "Repository: $DOCKER_REPO"
    echo "Tag:        $TAG"
    echo "=========================================="
    if [ "$DOCKER_BUILD_NO_CACHE" = "1" ]; then
        docker build --no-cache -f dockerfile \
            -t "${DOCKER_REPO}:${TAG}" \
            -t "${DOCKER_REPO}:latest" \
            .
    else
        docker build -f dockerfile \
            -t "${DOCKER_REPO}:${TAG}" \
            -t "${DOCKER_REPO}:latest" \
            .
    fi
    echo ""
    echo "✅ Image: ${DOCKER_REPO}:${TAG} (also tagged :latest)"
    echo "   Run:   docker run --rm -p 9000:8000 --env-file .env ${DOCKER_REPO}:${TAG}"
    echo "=========================================="
    exit 0
fi

# ─── Multi-arch build + push ────────────────────────────────────────────────
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
if [ "$DOCKER_BUILD_NO_CACHE" = "1" ]; then
    docker buildx build --no-cache \
        --platform "$DOCKER_PLATFORMS" \
        -f dockerfile \
        -t "${DOCKER_REPO}:${VERSION}" \
        -t "${DOCKER_REPO}:latest" \
        --push \
        .
else
    docker buildx build \
        --platform "$DOCKER_PLATFORMS" \
        -f dockerfile \
        -t "${DOCKER_REPO}:${VERSION}" \
        -t "${DOCKER_REPO}:latest" \
        --push \
        .
fi

echo ""
echo "✅ Done. Image supports: $DOCKER_PLATFORMS"
echo "   docker pull ${DOCKER_REPO}:latest"
echo "   (Runners pick amd64 or arm64 automatically.)"
echo "=========================================="
