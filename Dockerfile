FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/ backend/
COPY frontend/ frontend/
# Canonical brand assets (logo/icons) shared by web + Teams + meeting bot, served
# at /brand/*. Only assets/brand is un-ignored in .dockerignore.
COPY assets/ assets/

RUN uv sync --frozen --no-dev

EXPOSE 3000

CMD ["uv", "run", "--no-sync", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "3000"]