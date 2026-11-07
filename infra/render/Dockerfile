# Leafguard web: the wallets (alice/bob/carol), the explorer and the gateway that serves them on one port.
# The chain and the alt mempool run elsewhere (your server, started with ./start.sh); see infra/render/README.md.
# Build from the repository root (submodules checked out):
#   docker build -f infra/render/Dockerfile -t leafguard-web .
#   docker run -p 10000:10000 --env-file leafguard.env leafguard-web   # env: infra/render/export-env.sh
# Devnet only, never real funds.
FROM ghcr.io/astral-sh/uv:0.8 AS uv

FROM ubuntu:24.04
ARG FOUNDRY_VERSION=v1.3.1
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*
# cast (Foundry): the wallets encode calls and send the deployer's faucet / account-setup transactions with it
RUN curl -fsSL "https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY_VERSION}/foundry_${FOUNDRY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local/bin cast
# Python with OpenSSL >= 3.2 (native KECCAK-256); the toolkit needs no pip packages
COPY --from=uv /uv /usr/local/bin/uv
RUN uv python install 3.13 --install-dir /opt/python \
 && ln -s "$(ls -d /opt/python/cpython-3.13*/bin/python3.13 | head -1)" /usr/local/bin/pqpython \
 && pqpython -c 'import hashlib; hashlib.new("KECCAK-256")'

WORKDIR /app
COPY . /app
RUN test -f third_party/JARDIN/script/jardin_fors_plain_signer.py \
    || (echo "third_party/JARDIN is empty: check out the git submodules" && exit 1)
RUN chmod +x infra/render/entrypoint.sh
ENV PORT=10000
EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "--", "/app/infra/render/entrypoint.sh"]
