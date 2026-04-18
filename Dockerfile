FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY agent/ agent/
COPY channels/ channels/
COPY ingest/ ingest/
COPY retrieval/ retrieval/
COPY ui/ ui/
COPY config.py .

# Create data directory
RUN mkdir -p data logs

# Expose port
EXPOSE 8501

CMD streamlit run ui/app.py \
    --server.port ${PORT:-8501} \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.fileWatcherType none
