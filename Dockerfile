# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for moviepy, imageio, and opencv
# curl for HEALTHCHECK (Coolify/Docker); build-essential and python3-dev for compilation
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    curl \
    ffmpeg \
    imagemagick \
    libsm6 \
    libxext6 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Fix ImageMagick policy to allow PDF/Text operations
RUN sed -i 's/none/read,write/g' /etc/ImageMagick-6/policy.xml || true

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app
COPY . .

# Create directory for static files if not exists
RUN mkdir -p app/static/videos app/static/covers app/static/icons

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Set environment variables
ENV MODULE_NAME="app.main"
ENV VARIABLE_NAME="app"
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Coolify/Render: set PORT in environment if your platform uses a different port
# exec so uvicorn receives SIGTERM and can shut down cleanly
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

# Health check so the platform knows when the app is ready (avoids "Restarting" loop)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD sh -c "curl -f http://localhost:${PORT:-8000}/health || exit 1"
