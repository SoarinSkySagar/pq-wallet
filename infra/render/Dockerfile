# Leafguard demo in one container: single-node EIP-8141 devnet (ethrex eip8141-tracer + Lighthouse focil),
# ERC-8421 alt-mempool sidecar with [BUDGET-010], Leafguard contracts, wallets and explorer.
# Build from the repository root (submodules checked out):  docker build -f infra/render/Dockerfile -t leafguard .
# Run:  docker run -p 10000:10000 leafguard   ->  http://localhost:10000 (explorer), /wallet (alice)
# Devnet only, never real funds.

# --- pinned upstream images (the same digests the local Kurtosis devnet runs)
FROM ghcr.io/forshtat/ethrex@sha256:920295ae5496669a6f90a44431d4cbcb4703feec0e89207599fe19a251739873 AS ethrex
FROM ethpandaops/lighthouse@sha256:dd7e92e2e9c3127750e9bff5f8a0efec686f296445a44ef5d9c1eb750e8906b0 AS lighthouse
FROM oven/bun:1.2.23 AS bun
FROM ghcr.io/astral-sh/uv:0.8 AS uv

# --- the workshop sidecar + our ERC-8421 [BUDGET-010] patch (same steps as infra/sidecar-budget010/build.sh)
FROM ghcr.io/forshtat/mempool-workshop-sidecar@sha256:c4cdc5c707d94f3968566ba34904d1a7c4ab4dee962d822e1bac1bf6714e022e AS sidecar
COPY infra/sidecar-budget010/budget-010-src.patch /tmp/budget-010-src.patch
RUN apk add --no-cache patch \
 && cd /usr/app && patch -p1 --forward < /tmp/budget-010-src.patch \
 && cd packages/executor && ../../node_modules/.bin/tsc -p tsconfig.build.json \
 && grep -q MAX_VERIFY_GAS_STAKED_ENTITY lib/services/UserOpValidation/utils/validationPrefix.js

# --- runtime
FROM ubuntu:24.04
ARG FOUNDRY_VERSION=v1.3.1
ENV DEBIAN_FRONTEND=noninteractive PATH=/root/.foundry/bin:$PATH PQV_SOLC=/root/.svm/0.8.28/solc-0.8.28
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl libssl3 tini procps \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /root/.foundry/bin \
 && curl -fsSL "https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY_VERSION}/foundry_${FOUNDRY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /root/.foundry/bin forge cast
COPY --from=ethrex /usr/local/bin/ethrex /usr/local/bin/ethrex
COPY --from=lighthouse /usr/local/bin/lighthouse /usr/local/bin/lighthouse
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=sidecar /usr/app /opt/sidecar
# Python with OpenSSL >= 3.2 (native KECCAK-256); the toolkit needs no pip packages
COPY --from=uv /uv /usr/local/bin/uv
RUN uv python install 3.13 --install-dir /opt/python \
 && ln -s "$(ls -d /opt/python/cpython-3.13*/bin/python3.13 | head -1)" /usr/local/bin/pqpython \
 && pqpython -c 'import hashlib; hashlib.new("KECCAK-256")'

WORKDIR /app
COPY . /app
RUN test -f third_party/JARDIN/src/JardinForsPlainVerifier.sol || (echo "third_party/JARDIN is empty: check out the git submodules" && exit 1)
RUN cd contracts && forge build -q && test -x "$PQV_SOLC"
RUN chmod +x infra/render/entrypoint.sh
ENV PORT=10000
EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/infra/render/entrypoint.sh"]
