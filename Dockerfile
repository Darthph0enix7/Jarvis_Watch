# ─────────────────────────────────────────────────────────────────────────────
# Jarvis Voice Assistant — Production Dockerfile
# Targets Koyeb (or any OCI-compliant container runtime).
#
# Build:  docker build -t jarvis-server .
# Run:    docker run -p 8000:8000 --env-file .env jarvis-server
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Ensure Python output is sent straight to logs, no buffering
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build-time deps for packages with C extensions (cryptography, etc.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Copy requirements first so this layer is rebuilt only when deps change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Least-privilege user ──────────────────────────────────────────────────────
RUN useradd --no-create-home --shell /bin/false jarvis \
    && chown -R jarvis:jarvis /app
USER jarvis

# ── Port ─────────────────────────────────────────────────────────────────────
# Koyeb injects $PORT at runtime (typically 8000).
# The server reads os.environ.get("PORT", 8000) automatically.
EXPOSE 8000

# ── Health check ─────────────────────────────────────────────────────────────
# GET /health is unauthenticated and returns {"status": "ok"}.
# Uses stdlib only — no curl required in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
        "import urllib.request, os; \
         urllib.request.urlopen( \
           'http://localhost:' + os.environ.get('PORT', '8000') + '/health' \
         ).read()"

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["python", "server.py"]
