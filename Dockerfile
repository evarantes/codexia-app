# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for moviepy, imageio, and opencv
# Added build-essential and python3-dev for compilation support
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    ffmpeg \
    imagemagick \
    libsm6 \
    libxext6 \
    libgl1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick policy to allow PDF/Text operations
RUN sed -i 's/none/read,write/g' /etc/ImageMagick-6/policy.xml || true

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium used by Playwright for Amazon KDP automation
RUN python -m playwright install --with-deps chromium

# Copy the current directory contents into the container at /app
COPY . .

# Apply deterministic hardening before any runtime validation/startup.
# This keeps the legacy large files unchanged in unrelated regions while
# enforcing UI boot safety, publication decoupling and human duration approval.
RUN python scripts/apply_consolidated_hardening.py --apply && \
    python scripts/apply_consolidated_hardening.py --check && \
    python scripts/apply_duration_confirmation_hardening.py --apply && \
    python scripts/apply_duration_confirmation_hardening.py --check

# Create directory for static files if not exists
RUN mkdir -p app/static/videos app/static/covers app/static/icons && \
    chmod -R 755 /app && \
    ls -la /app/app/static

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Set environment variables
ENV MODULE_NAME="app.main"
ENV VARIABLE_NAME="app"
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Run the application with uvicorn directly (lighter for free tier).
# In production, derive the exact YouTube OAuth callback from BASE_URL unless
# YOUTUBE_OAUTH_REDIRECT_URI was explicitly configured. This keeps localhost
# available only when no public BASE_URL exists (development/homologation).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

CMD sh -c 'if [ -z "${YOUTUBE_OAUTH_REDIRECT_URI:-}" ] && [ -n "${BASE_URL:-}" ]; then export YOUTUBE_OAUTH_REDIRECT_URI="${BASE_URL%/}/youtube/auth/callback"; fi; python scripts/apply_migrations.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}'