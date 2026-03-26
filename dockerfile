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
    echo "=== Checking surveys/ (images for /survey-images; JSON optional) ===" && \
    test -d /app/surveys && echo "✅ surveys/ directory exists" || (echo "❌ surveys/ MISSING" && exit 1) && \
    ls -lh /app/surveys/ && \
    test -d /app/surveys/images && echo "✅ surveys/images/ exists" || (echo "❌ surveys/images/ MISSING" && exit 1) && \
    ls -lh /app/surveys/images/ && \
    echo "" && \
    echo "=== File count summary ===" && \
    echo "Instructor default survey JSONs:" && \
    test -f /app/surveys/instructor_default_pre_survey.json && echo "  ✅ instructor_default_pre_survey.json" || echo "  ⚠️  pre missing" && \
    test -f /app/surveys/instructor_default_post_survey.json && echo "  ✅ instructor_default_post_survey.json" || echo "  ⚠️  post missing" && \
    test -f /app/surveys/instructor_default_recurrent_survey.json && echo "  ✅ instructor_default_recurrent_survey.json" || echo "  ⚠️  recurrent missing" && \
    echo "Other survey JSONs (optional): $(find /app/surveys -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)" && \
    echo "Survey images: $(find /app/surveys/images -type f 2>/dev/null | wc -l)" && \
    echo "Templates: $(find /app/templates -name '*.html' 2>/dev/null | wc -l)" && \
    echo "" && \
    echo "✅ Verification complete!"

# Create necessary directories with proper permissions
RUN mkdir -p \
    submissions \
    session_logs \
    logs \
    surveys/quiz_assets \
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