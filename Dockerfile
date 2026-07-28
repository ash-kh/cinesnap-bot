FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY moviebot.py README.md ./

ENV MOVIE_DB=/data/movies.sqlite3

CMD ["python", "moviebot.py"]
