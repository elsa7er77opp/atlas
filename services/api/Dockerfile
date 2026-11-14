# Project Atlas - API service
# AT-26-L1-002  |  NFR-01, NFR-02

FROM python:3.12-slim

# Non-root, matching the pattern of the other Atlas services. uid 10003 is
# taken by the anomaly engine, so this one is 10004.
RUN groupadd -g 10004 atlas && useradd -u 10004 -g atlas -M -s /usr/sbin/nologin atlas

WORKDIR /app

# Dependencies as their own layer: application edits do not re-resolve them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The image must not inherit whatever permissions the build context
# happened to carry. Read and traverse for everyone, write for no one:
# the service runs non-root and never modifies its own source.
RUN chmod -R a=rX /app/app

USER atlas
EXPOSE 8000

# One worker. The service is I/O bound on the database, and the LISTEN thread
# in routers/live.py is per-process: with N workers there would be N listening
# connections and a client would receive one copy per worker it is not
# connected to. Scaling out needs a shared broker for fan-out first, which is
# recorded as an M3 item.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header"]
