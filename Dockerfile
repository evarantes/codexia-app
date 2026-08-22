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
# Cost control runs after duration + OpenAI defaults so it can safely add
# per-production quality, budget confirmation and the cost panel.
# Caption integrity converts the legacy text mismatch validator into local
# recovery; final visual quality makes visual warnings review-safe; checkpoint
# recovery normalizes saved assets; final-render recovery runs last so an MP4
# already written at stage_6 is salvaged before any paid retry can start.
# Production manifest runs after all recovery layers; narration contract then
# protects TTS/CTA and persists paid assets immediately before temp cleanup.
# Manifest diagnostics is read-only and exposes the durable recovery state.
# Adaptive render threads runs last and may choose 1 or 2 FFmpeg threads.
RUN python scripts/apply_consolidated_hardening.py --apply && \
    python scripts/apply_consolidated_hardening.py --check && \
    python scripts/apply_duration_confirmation_hardening.py --apply && \
    python scripts/apply_duration_confirmation_hardening.py --check && \
    python scripts/apply_openai_quality_cost_hardening.py --apply && \
    python scripts/apply_openai_quality_cost_hardening.py --check && \
    python scripts/apply_video_cost_backend_hardening.py --apply && \
    python scripts/apply_video_cost_backend_hardening.py --check && \
    python scripts/apply_video_cost_ui_hardening.py --apply && \
    python scripts/apply_video_cost_ui_hardening.py --check && \
    python scripts/apply_voice_closure_hardening.py --apply && \
    python scripts/apply_voice_closure_hardening.py --check && \
    python scripts/apply_caption_integrity_self_heal.py --apply && \
    python scripts/apply_caption_integrity_self_heal.py --check && \
    python scripts/apply_final_visual_quality_gate_self_heal.py --apply && \
    python scripts/apply_final_visual_quality_gate_self_heal.py --check && \
    python scripts/apply_recovery_checkpoint_hardening.py --apply && \
    python scripts/apply_recovery_checkpoint_hardening.py --check && \
    python scripts/apply_final_render_recovery.py --apply && \
    python scripts/apply_final_render_recovery.py --check && \
    python scripts/apply_final_render_recovery_compat.py --apply && \
    python scripts/apply_final_render_recovery_compat.py --check && \
    python scripts/apply_final_render_recovery_scope.py --apply && \
    python scripts/apply_final_render_recovery_scope.py --check && \
    python scripts/apply_production_manifest_hardening.py --apply && \
    python scripts/apply_production_manifest_hardening.py --check && \
    python scripts/apply_narration_contract_hardening.py --apply && \
    python scripts/apply_narration_contract_hardening.py --check && \
    python scripts/apply_manifest_diagnostics_hardening.py --apply && \
    python scripts/apply_manifest_diagnostics_hardening.py --check && \
    python scripts/apply_adaptive_render_threads_hardening.py --apply && \
    python scripts/apply_adaptive_render_threads_hardening.py --check && \
    python -m compileall -q app scripts

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
