FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ingest.py .
RUN useradd -m -u 10002 atlas && chown -R atlas /app
USER atlas
CMD ["python","-u","ingest.py"]
