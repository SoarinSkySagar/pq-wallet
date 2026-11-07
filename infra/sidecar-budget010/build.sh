#!/usr/bin/env bash
# Build the ERC-8421 alt-mempool sidecar with [BUDGET-010] per-entity validation budgets.
#
# Base: the published workshop sidecar image (forshtat/mempool-workshop-sidecar, MIT), pinned by digest;
# its bundled sources are identical to upstream commit 2e22ff0a. The only change is budget-010-src.patch:
# the EIP-8141 combined MAX_VERIFY_GAS (100k) check becomes ERC-8421's per-entity check
# (MAX_VERIFY_GAS_PER_ENTITY = 100k unstaked, MAX_VERIFY_GAS_STAKED_ENTITY = 1M staked).
# The executor package is recompiled inside the image with the image's own toolchain and node_modules,
# so nothing is downloaded from npm. budget-010-full.patch = the same change + its unit tests (upstream PR).
#
#   infra/sidecar-budget010/build.sh            -> image pqvault/mempool-sidecar:budget-010
#   SIDECAR_IMAGE=pqvault/mempool-sidecar:budget-010 infra/sidecar-local.sh <port> <staking>
set -euo pipefail
BASE="ghcr.io/forshtat/mempool-workshop-sidecar@sha256:c4cdc5c707d94f3968566ba34904d1a7c4ab4dee962d822e1bac1bf6714e022e"
TAG="${TAG:-pqvault/mempool-sidecar:budget-010}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT

docker pull -q "$BASE" >/dev/null
cid="$(docker create "$BASE")"
mkdir -p "$CTX/root/packages/executor"
docker cp "$cid:/usr/app/packages/executor/src" "$CTX/root/packages/executor/src"
docker rm "$cid" >/dev/null
patch -d "$CTX/root" -p1 --forward < "$HERE/budget-010-src.patch"

cat > "$CTX/Dockerfile" <<EOF
FROM $BASE
COPY root/packages/executor/src /usr/app/packages/executor/src
RUN cd /usr/app/packages/executor && ../../node_modules/.bin/tsc -p tsconfig.build.json \\
 && grep -q MAX_VERIFY_GAS_STAKED_ENTITY lib/services/UserOpValidation/utils/validationPrefix.js
LABEL org.opencontainers.image.description="forshtat/mempool-workshop-sidecar@2e22ff0a + ERC-8421 [BUDGET-010] per-entity validation budgets (Leafguard)"
EOF
docker build -q -t "$TAG" "$CTX"
echo "built $TAG"
