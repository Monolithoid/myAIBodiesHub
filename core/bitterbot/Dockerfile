# Dockerfile maintained by monolithoid/ia-agent
# Replaces the upstream Dockerfile of Bitterbot-AI/bitterbot-desktop
# which references paths missing from the repo (ui/, patches/, pnpm ui:build).
#
# Differences from upstream:
#   + RUN apt-get: system dependencies for the native addons
#                   (missing from node:22-bookworm by default)
#   - COPY ui/package.json  (ui/ does not exist in the repo)
#   - COPY patches          (no patchedDependencies → an empty mkdir is enough)
#   - RUN pnpm ui:build     (script missing from package.json)

FROM node:22-slim

# Install Bun (required for build scripts)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

RUN corepack enable

WORKDIR /app

# ----------------------------------------------------------------
# System dependencies for the native addons (onlyBuiltDependencies)
#
# authenticate-pam → node-gyp builds from source, PAM headers required
#   No prebuilt npm binary → libpam0g-dev required
#
# @napi-rs/canvas → prebuilt NAPI-RS binary for linux-x64,
#   but the postinstall checks the Cairo headers — they are installed
#   in case the build fallback is triggered
#
# @lydell/node-pty → node-gyp, build-essential + python3 required
#
# node-llama-cpp → prebuilt CPU binary available for linux-x64,
#   cmake as a fallback if the prebuilt does not match the environment
# ----------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # node-gyp build tools
    build-essential \
    python3 \
    # authenticate-pam (compile from source, no prebuilt)
    libpam0g-dev \
    # @napi-rs/canvas (Cairo renderer)
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    # node-llama-cpp (compile fallback)
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Extra APT packages through a build arg (optional, inherited from upstream)
ARG BITTERBOT_DOCKER_APT_PACKAGES=""
RUN if [ -n "$BITTERBOT_DOCKER_APT_PACKAGES" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $BITTERBOT_DOCKER_APT_PACKAGES && \
      apt-get clean && \
      rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*; \
    fi

# Copy the manifests to optimise the pnpm install layer cache.
# Note: ui/package.json does not exist in this repo — COPY removed.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY scripts ./scripts

# patches/ missing from the repo (no patchedDependencies in package.json)
RUN mkdir -p patches

# --no-frozen-lockfile: precaution for lockfile/package.json mismatches,
# frequent in an active upstream; pnpm resolves without error.
RUN pnpm install --no-frozen-lockfile

COPY . .
RUN pnpm build
# Note: `pnpm ui:build` is in the upstream Dockerfile but the script
# does not exist in package.json at this commit — removed on purpose.

ENV NODE_ENV=production

# Allows writing temporary files at runtime.
RUN chown -R node:node /app

# Security: run as non-root (uid 1000 = node)
USER node

CMD ["node", "bitterbot.mjs", "gateway", "--allow-unconfigured"]
