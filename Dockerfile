# Browser runs on the phone; FFmpeg and Deno run in this container.
FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DENO_NO_UPDATE_CHECK=1 DENO_NO_PROMPT=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /srv/sonora
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY templates ./templates
RUN useradd --create-home --shell /usr/sbin/nologin sonora
USER sonora
EXPOSE 10000
CMD ["gunicorn","app:app","--bind","0.0.0.0:10000","--workers","1","--threads","2","--timeout","300","--access-logfile","-"]
