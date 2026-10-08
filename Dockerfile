FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRIP_JOURNAL_DATA=/data \
    TRIP_JOURNAL_PHOTOS=/photos

WORKDIR /app
COPY pyproject.toml README.md ./
COPY trip_journal ./trip_journal
RUN pip install .

# Run as an unprivileged user; /data holds the database and thumbnails.
RUN useradd --create-home --uid 1000 journal \
    && mkdir -p /data /photos \
    && chown journal:journal /data
USER journal
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/trips', timeout=4)"
CMD ["trip-journal", "serve", "--host", "0.0.0.0", "--port", "8000"]
