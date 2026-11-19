FROM ghcr.io/astral-sh/uv:python3.11-trixie-slim

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy


# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Install only the engine used by the worker; source changes reuse this layer.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && /app/.venv/bin/playwright install --with-deps webkit \
    && rm -rf /var/lib/apt/lists/*

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

COPY src /app/src
COPY utils /app/utils

# Reset the entrypoint, don't invoke `uv`
ENTRYPOINT []

CMD ["bash", "/app/utils/start.sh"]
