# Use Python 3.11 slim image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    default-jdk \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy requirements first
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy .env file (will be included in image)
COPY --chown=appuser:appuser .env .env

# Copy application code
COPY --chown=appuser:appuser . .

# CRITICAL: Explicitly verify all critical files and directories
RUN echo "=== VERIFYING CRITICAL DIRECTORIES ===" && \
    echo "Current directory:" && pwd && \
    echo "" && \
    echo "Contents of /app:" && ls -la /app/ && \
    echo "" && \
    echo "=== Checking .env file ===" && \
    test -f /app/.env && echo "✅ .env file exists" || echo "❌ .env file missing" && \
    ls -la /app/.env && \
    echo "" && \
    echo "=== Checking surveys/ ===" && \
    ls -la /app/surveys/ && \
    echo "" && \
    echo "=== Checking surveys JSON files ===" && \
    ls -la /app/surveys/*.json 2>/dev/null || echo "No JSON files found" && \
    echo "" && \
    echo "=== Checking surveys/images/ ===" && \
    ls -la /app/surveys/images/ 2>/dev/null || echo "No images directory" && \
    echo "" && \
    echo "=== Checking templates/ ===" && \
    ls -la /app/templates/ && \
    echo "" && \
    echo "=== Checking assignment/ ===" && \
    ls -la /app/assignment/ 2>/dev/null || echo "No assignment directory" && \
    echo "" && \
    echo "=== Checking starter_code/ ===" && \
    ls -la /app/starter_code/ 2>/dev/null || echo "No starter_code directory" && \
    echo "" && \
    echo "✅ All critical directories verified!"

# Create necessary directories with proper permissions
RUN mkdir -p \
    submissions \
    session_logs \
    logs \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Start application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]