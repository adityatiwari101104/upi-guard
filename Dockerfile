FROM python:3.11-slim

WORKDIR /app

# Install system deps for psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Train ML fraud detection model (compatible with container's scikit-learn)
RUN python train_fraud_model.py

EXPOSE 5000

CMD ["gunicorn", "--worker-class", "gthread", "-w", "2", "--threads", "4", "--bind", "0.0.0.0:5000", "app:app"]
