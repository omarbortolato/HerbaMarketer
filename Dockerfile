FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip uninstall -y google-ads google-auth googleapis-common-protos \
    grpcio grpcio-status protobuf 2>/dev/null || true
RUN pip install --no-cache-dir --force-reinstall \
    google-ads==21.3.0 \
    google-auth==2.29.0 \
    googleapis-common-protos==1.63.0 \
    grpcio==1.62.1 \
    grpcio-status==1.62.1 \
    protobuf==4.25.3
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Run Alembic migrations then start the app
CMD ["sh", "-c", "alembic upgrade head && uvicorn dashboard.app:app --host 0.0.0.0 --port 8000"]
