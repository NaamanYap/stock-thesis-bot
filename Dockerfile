# Multi-arch base: pulls linux/arm64 on the Oracle Ampere A1 host, amd64 elsewhere.
# 3.14 matches local development.
FROM python:3.14-slim

# Unbuffered so the bot's logging reaches `docker logs` as it happens rather than
# sitting in a pipe buffer until the process exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so a source edit does not invalidate the wheel install layer.
# Every dependency ships a manylinux aarch64 wheel (numpy, pandas, lxml, reportlab,
# pydantic-core, curl_cffi), so no compiler is needed. If that ever stops being true,
# build wheels in a separate stage rather than adding build-essential here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# reportlab uses the built-in Helvetica base-14 fonts, so no system font packages.
# The bot writes PDFs under out/ before uploading them, so that has to be writable.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/out \
    && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
