#!/usr/bin/env bash
# Build the Docker image on this computer, push it to Docker Hub with the next version tag,
# optionally roll Koyeb onto it. macOS / Linux terminal, or Git Bash on Windows.
#
#   ./release.sh                     # v1.0.3 -> v1.0.4, push :v1.0.4 and :latest
#   ./release.sh minor               # v1.0.3 -> v1.1.0
#   ./release.sh major               # v1.0.3 -> v2.0.0
#   ./release.sh --tag v2.0.0-rc1    # explicit tag
#   ./release.sh --koyeb distractors/web --koyeb distractors/runner
#   ./release.sh --local             # build for this machine only, no push, no bump
#   ./release.sh --dry-run           # print the commands without running them
#
# Other options: --platforms linux/amd64 (faster; enough for Koyeb), --no-cache, --force
# (overwrite an existing tag), --no-latest (leave :latest alone), --repo user/name.
# The last released version is kept in VERSION; commit it after a release.
set -euo pipefail
cd "$(dirname "$0")"

REPO="${DOCKER_REPO:-rahadarmannabid/distractors}"
PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"   # Koyeb runs amd64, Apple Silicon arm64
BUILDER="distractors-multiarch"
PART="patch"
TAG=""
LOCAL=0 DRY_RUN=0 FORCE=0 NO_LATEST=0 NO_CACHE=0
KOYEB_SERVICES=()

info() { printf '==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# Print a command, then run it (unless --dry-run).
run() {
  printf '  $'; printf ' %s' "$@"; printf '\n'
  [[ "$DRY_RUN" == 1 ]] || "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    patch|minor|major) PART="$1" ;;
    --tag)       TAG="${2:?--tag needs a value}"; shift ;;
    --repo)      REPO="${2:?--repo needs a value}"; shift ;;
    --platforms) PLATFORMS="${2:?--platforms needs a value}"; shift ;;
    --koyeb)     KOYEB_SERVICES+=("${2:?--koyeb needs APP/SERVICE}"); shift ;;
    --local)     LOCAL=1 ;;
    --dry-run)   DRY_RUN=1 ;;
    --force)     FORCE=1 ;;
    --no-latest) NO_LATEST=1 ;;
    --no-cache)  NO_CACHE=1 ;;
    -h|--help)   usage ;;
    *)           die "unknown option: $1 (see ./release.sh --help)" ;;
  esac
  shift
done

command -v docker >/dev/null || die "docker not found. Install Docker Desktop (macOS/Windows) or Docker Engine (Linux)."
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop (or the docker service) and try again."

# ── Version ─────────────────────────────────────────────────────────────────
SEMVER='^v?([0-9]+)\.([0-9]+)\.([0-9]+)$'
if [[ -n "$TAG" ]]; then
  [[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "invalid Docker tag: $TAG"
elif [[ "$LOCAL" == 1 ]]; then
  TAG="local"
elif [[ ! -f VERSION ]]; then
  TAG="v1.0.0"  # first release
else
  current="$(tr -d '[:space:]' < VERSION)"
  [[ "$current" =~ $SEMVER ]] || die "VERSION must hold a version like 1.2.3 (found '$current')"
  major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}" patch="${BASH_REMATCH[3]}"
  case "$PART" in
    major) TAG="v$((major + 1)).0.0" ;;
    minor) TAG="v${major}.$((minor + 1)).0" ;;
    patch) TAG="v${major}.${minor}.$((patch + 1))" ;;
  esac
fi
IMAGE="${REPO}:${TAG}"

BUILD=(docker buildx build -f Dockerfile --build-arg "APP_VERSION=${TAG}" -t "$IMAGE")
[[ "$LOCAL" == 1 || "$NO_LATEST" == 1 ]] || BUILD+=(-t "${REPO}:latest")
[[ "$NO_CACHE" == 1 ]] && BUILD+=(--no-cache)

# ── Local build (this machine only) ─────────────────────────────────────────
if [[ "$LOCAL" == 1 ]]; then
  info "Local build of ${IMAGE} (this machine's architecture, not pushed)"
  run "${BUILD[@]}" --load .
  info "Done. Try it:  docker run --rm -p 8000:8000 --env-file .env ${IMAGE}"
  exit 0
fi

# ── Multi-arch build + push ─────────────────────────────────────────────────
docker buildx version >/dev/null 2>&1 || die "docker buildx not found. It ships with Docker Desktop; on Linux install docker-buildx-plugin."
if [[ "$FORCE" != 1 ]] && docker buildx imagetools inspect "$IMAGE" >/dev/null 2>&1; then
  die "${IMAGE} already exists on Docker Hub. Bump again, pass --tag, or use --force to overwrite."
fi

info "Releasing ${IMAGE}  (platforms: ${PLATFORMS})"
echo "    If the push is denied, run 'docker login' (user: rahadarmannabid) and retry."

# Multi-arch builds need a docker-container builder; the default "docker" driver cannot do them.
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  run docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
fi
run "${BUILD[@]}" --builder "$BUILDER" --platform "$PLATFORMS" --push .

if [[ "$TAG" =~ $SEMVER && "$DRY_RUN" != 1 ]]; then
  echo "${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}" > VERSION
  info "VERSION is now $(cat VERSION) (commit it)"
fi

# ── Optional: point Koyeb services at the new tag ───────────────────────────
if [[ ${#KOYEB_SERVICES[@]} -gt 0 ]]; then
  command -v koyeb >/dev/null || die "pushed ${IMAGE}, but the koyeb CLI is not installed; update the services in the Koyeb dashboard."
  for svc in "${KOYEB_SERVICES[@]}"; do
    info "Deploying ${IMAGE} to Koyeb service ${svc}"
    run koyeb service update "$svc" --docker "$IMAGE"
  done
fi

info "Done: ${IMAGE}$([[ "$NO_LATEST" == 1 ]] || echo " and ${REPO}:latest")"
echo "    docker pull ${IMAGE}"
