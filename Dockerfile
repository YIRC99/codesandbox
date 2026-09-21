# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ARG UV_VERSION=0.11.21
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM python:3.12-slim

RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install --no-install-recommends --yes tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp sandbox \
    && mkdir -p /data/uploads /data/data-files /data/analysis-results \
    && chown 10001:10001 /data/uploads /data/data-files /data/analysis-results

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ /app/src/
COPY --chown=10001:10001 data/ /app/data/

ENV PATH="/app/.venv/bin:$PATH" \
    HOME="/tmp" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1" \
    SANDBOX_ANALYSIS_RESULT_DIR="/data/analysis-results"

USER 10001:10001
EXPOSE 32004

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "src.sandbox.main:app", "--host", "0.0.0.0", "--port", "32004", "--no-access-log"]
