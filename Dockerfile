# Playwright base image — includes Python + Chromium + all browser deps
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first for layer caching
COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

# Install Playwright browser binaries (Chromium only — we only use that)
RUN python -m playwright install chromium

# Copy application code
COPY backend /app/backend
WORKDIR /app/backend

EXPOSE 8765

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8765"]
