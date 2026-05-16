FROM python:3.12-slim

# Stdlib-only — no pip install. Tiny image.
WORKDIR /app
COPY pylon_mini_node.py .

# Run as non-root.
RUN useradd -r -u 1000 -s /usr/sbin/nologin minode \
  && chown -R minode:minode /app
USER minode

ENV LOG_LEVEL=INFO

ENTRYPOINT ["python3", "/app/pylon_mini_node.py"]
