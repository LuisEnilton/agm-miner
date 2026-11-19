# agm-miner in a container: `docker compose run --rm agm <command>` (see docs/instalacao.md).
# Debian trixie ships git 2.47; the miner needs git >= 2.44 (`--no-lazy-fetch`), which bookworm
# (git 2.39) does not have.
FROM python:3.11-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1
RUN pip install "poetry==2.1.4"

WORKDIR /app
# Dependencies first, so a code change does not reinstall them. EXTRAS=gemini adds the Gemini SDK
# (used only in the pilot).
ARG EXTRAS=""
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root ${EXTRAS:+--extras "$EXTRAS"}
COPY src ./src
RUN poetry install --only main ${EXTRAS:+--extras "$EXTRAS"}
COPY alembic.ini study.yaml ./
COPY alembic ./alembic
COPY codebooks ./codebooks

# The image has no .git: the commit it was built from is recorded in every run and manifest.
ARG CODE_VERSION=unknown
ENV AGM_CODE_VERSION=${CODE_VERSION} \
    DATA_DIR=/data \
    CLONE_DIR=/data/clones \
    HOME=/tmp
VOLUME /data

ENTRYPOINT ["agm"]
CMD ["help"]
