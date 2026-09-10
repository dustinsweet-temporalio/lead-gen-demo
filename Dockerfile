# Durable Lead Intake — app image (web server + the supervised worker subprocess).
# The worker is a subprocess of this container (supervisor uses sys.executable), so the
# "Crash worker / Restart worker" demo works by SIGKILLing the subprocess inside the
# container — the container itself stays up, exactly like the local run.

FROM python:3.12-slim

# Pre-warm the tiktoken vocab into the image so the fallback estimator never needs network
# at runtime (the demo is offline except for the model API calls).
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken_cache

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY app ./app

EXPOSE 8000
# The web server owns the worker supervisor and connects to Temporal at TEMPORAL_ADDRESS.
CMD ["python", "-m", "app.web.server"]
