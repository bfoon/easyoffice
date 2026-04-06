FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=easyoffice.settings

# Install system dependencies (debian bookworm compatible)
# Includes LibreOffice for server-side document → PDF conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    postgresql-client \
    gettext \
    libmagic1 \
    libmagic-dev \
    libffi-dev \
    curl \
    libreoffice \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    fonts-dejavu-core \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/media /app/staticfiles /app/logs

# Create a real home directory so LibreOffice can write its user profile.
# --system users default to /nonexistent which causes the fatal LO startup error.
RUN addgroup --system easyoffice && \
    adduser --system --ingroup easyoffice --home /home/easyoffice easyoffice && \
    mkdir -p /home/easyoffice && \
    chown -R easyoffice:easyoffice /home/easyoffice /app

# Pre-warm the LibreOffice user profile as the app user so the first
# conversion does not fail trying to initialise the profile at runtime.
USER easyoffice
RUN HOME=/home/easyoffice libreoffice --headless --norestore --nofirststartwizard \
        --convert-to pdf /dev/null --outdir /tmp 2>/dev/null || true

EXPOSE 8000