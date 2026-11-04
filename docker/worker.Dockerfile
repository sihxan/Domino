# Prefect worker image: same Prefect version as prefect-server + the monorepo installed with uv.
FROM prefecthq/prefect:3.9.1-python3.12

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

# Install into the image's own interpreter (where prefect already lives), never download a Python.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# dependency layer (cached until pyproject.toml / uv.lock change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --inexact

# project layer (see .dockerignore for what is left out)
COPY . .
RUN uv sync --frozen --no-dev --inexact

ENV PYTHONPATH=/app
