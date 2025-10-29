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

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy .env file (will be included in image)
COPY --chown=appuser:appuser .env .env

# Copy application code (respects .dockerignore)
COPY --chown=appuser:appuser . .

# CRITICAL: Verify all required files exist - FAIL BUILD if missing
RUN echo "=== VERIFYING CRITICAL FILES AND DIRECTORIES ===" && \
    echo "Current directory:" && pwd && \
    echo "" && \
    echo "=== Checking main application files ===" && \
    test -f /app/main.py && echo "✅ main.py exists" || (echo "❌ main.py MISSING" && exit 1) && \
    test -f /app/admin.py && echo "✅ admin.py exists" || (echo "❌ admin.py MISSING" && exit 1) && \
    test -f /app/.env && echo "✅ .env exists" || (echo "❌ .env MISSING" && exit 1) && \
    echo "" && \
    echo "=== Checking templates/ ===" && \
    test -d /app/templates && echo "✅ templates/ directory exists" || (echo "❌ templates/ MISSING" && exit 1) && \
    ls -lh /app/templates/ && \
    test -f /app/templates/home.html && echo "✅ home.html exists" || (echo "❌ home.html MISSING" && exit 1) && \
    test -f /app/templates/index.html && echo "✅ index.html exists" || (echo "❌ index.html MISSING" && exit 1) && \
    test -f /app/templates/survey.html && echo "✅ survey.html exists" || (echo "❌ survey.html MISSING" && exit 1) && \
    echo "" && \
    echo "=== Checking surveys/ ===" && \
    test -d /app/surveys && echo "✅ surveys/ directory exists" || (echo "❌ surveys/ MISSING" && exit 1) && \
    ls -lh /app/surveys/ && \
    echo "" && \
    echo "=== Checking survey JSON files ===" && \
    test -f /app/surveys/pattern.json && echo "✅ pattern.json exists" || echo "⚠️  pattern.json missing" && \
    test -f /app/surveys/warmup.json && echo "✅ warmup.json exists" || echo "⚠️  warmup.json missing" && \
    echo "" && \
    echo "=== Checking surveys/images/ ===" && \
    test -d /app/surveys/images && echo "✅ surveys/images/ directory exists" || echo "⚠️  surveys/images/ missing" && \
    if [ -d /app/surveys/images ]; then ls -lh /app/surveys/images/; fi && \
    echo "" && \
    echo "=== Checking assignment/ ===" && \
    test -d /app/assignment && echo "✅ assignment/ directory exists" || echo "⚠️  assignment/ missing" && \
    if [ -d /app/assignment ]; then ls -lh /app/assignment/; fi && \
    echo "" && \
    echo "=== Checking assignment PDFs ===" && \
    test -f /app/assignment/pattern.pdf && echo "✅ pattern.pdf exists" || echo "⚠️  pattern.pdf missing" && \
    test -f /app/assignment/warmup.pdf && echo "✅ warmup.pdf exists" || echo "⚠️  warmup.pdf missing" && \
    echo "" && \
    echo "=== Checking starter_code/ ===" && \
    test -d /app/starter_code && echo "✅ starter_code/ directory exists" || echo "⚠️  starter_code/ missing" && \
    if [ -d /app/starter_code ]; then ls -lh /app/starter_code/; fi && \
    echo "" && \
    echo "=== File count summary ===" && \
    echo "Survey JSONs: $(find /app/surveys -name '*.json' 2>/dev/null | wc -l)" && \
    echo "Survey images: $(find /app/surveys/images -type f 2>/dev/null | wc -l)" && \
    echo "Assignment PDFs: $(find /app/assignment -name '*.pdf' 2>/dev/null | wc -l)" && \
    echo "Starter code files: $(find /app/starter_code -type f 2>/dev/null | wc -l)" && \
    echo "Templates: $(find /app/templates -name '*.html' 2>/dev/null | wc -l)" && \
    echo "" && \
    echo "✅ Verification complete!"

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
    CMD curl -f http://localhost:8000/health || exit 1

# Start application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]