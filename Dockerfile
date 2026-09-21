FROM python:3.12-slim

# No compiler needed: every dependency ships wheels, and Sieve itself is pure
# Python. That keeps the image small enough to run comfortably beside Invidious.
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sieve ./sieve
RUN pip install --no-cache-dir .

ENV SIEVE_DATA_DIR=/data
VOLUME /data
EXPOSE 8377

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://127.0.0.1:8377/api/status').status_code==200 else 1)"

CMD ["sieve", "serve", "--host", "0.0.0.0", "--port", "8377"]
