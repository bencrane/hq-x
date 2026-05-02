FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:/root/.local/bin:${PATH}"

# System deps for Doppler apt repo + uv installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Install Doppler CLI from the official apt repo.
RUN curl -sLf --retry 3 --tlsv1.2 --proto "=https" \
        "https://packages.doppler.com/public/cli/gpg.DE2A7741A397C129.key" \
        | gpg --dearmor -o /usr/share/keyrings/doppler-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/doppler-archive-keyring.gpg] https://packages.doppler.com/public/cli/deb/debian any-version main" \
        > /etc/apt/sources.list.d/doppler-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends doppler \
    && rm -rf /var/lib/apt/lists/*

# Install uv.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx

WORKDIR /app

# Dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source.
COPY app ./app
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# Non-root user.
RUN useradd --create-home --shell /bin/sh appuser \
    && chown -R appuser:appuser /app /opt/venv
USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
