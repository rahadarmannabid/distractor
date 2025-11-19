#!/bin/bash

# Docker Hub repository
DOCKER_REPO="rahadarmannabid/ai-code-assistant"

# Get version from user or use timestamp
if [ -z "$1" ]; then
    VERSION="v$(date +%Y%m%d-%H%M%S)"
    echo "No version specified, using: $VERSION"
else
    VERSION="$1"
fi

echo "=========================================="
echo "Building Docker Image"
echo "Repository: $DOCKER_REPO"
echo "Version: $VERSION"
echo "Platform: linux/amd64 (for Linux servers)"
echo "=========================================="

# Build the Docker image for Linux platform
echo "Step 1: Building Docker image for Linux (amd64)..."
docker build --platform linux/amd64 -t ${DOCKER_REPO}:${VERSION} -t ${DOCKER_REPO}:latest .

if [ $? -ne 0 ]; then
    echo "❌ Docker build failed!"
    exit 1
fi

echo "✅ Docker build successful!"

# Display image info
echo ""
echo "Image details:"
docker images | grep ${DOCKER_REPO}

echo ""
echo "=========================================="
echo "Pushing to Docker Hub"
echo "=========================================="

# Push version tag
echo "Step 2: Pushing versioned tag (${VERSION})..."
docker push ${DOCKER_REPO}:${VERSION}

if [ $? -ne 0 ]; then
    echo "❌ Docker push failed for version tag!"
    exit 1
fi

# Push latest tag
echo "Step 3: Pushing latest tag..."
docker push ${DOCKER_REPO}:latest

if [ $? -ne 0 ]; then
    echo "❌ Docker push failed for latest tag!"
    exit 1
fi

echo ""
echo "=========================================="
echo "✅ SUCCESS!"
echo "=========================================="
echo "Image pushed to Docker Hub:"
echo "  - ${DOCKER_REPO}:${VERSION}"
echo "  - ${DOCKER_REPO}:latest"
echo ""
echo "To pull this image, use:"
echo "  docker pull ${DOCKER_REPO}:latest"
echo "  docker pull ${DOCKER_REPO}:${VERSION}"
echo "=========================================="

