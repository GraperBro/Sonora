"""SONORA personal audio converter. Save only media you have permission to download."""
import io
import os
import re
import secrets
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)
app.json.ensure_ascii = False
app.config["MAX_CONTENT_LENGTH"] = 8192
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_SIZE = 100 * 1024 * 1024
SLOT = threading.BoundedSemaphore(1)


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Вставь ссылку YouTube.")
    value = value.strip()
    if value.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/", "music.youtube.com/", "youtu.be/")):
        value = "https://" + value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError as error:
        raise ValueError("Неправильная ссылка.") from error
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Нужна ссылка YouTube.")
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        path = parsed.path.strip("/")
        video_id = parse_qs(parsed.query).get("v", [""])[0] if path == "watch" else (
            path.split("/")[1] if path.startswith(("shorts/", "live/", "embed/")) else "")
    else:
        raise ValueError("Поддерживаются только youtube.com и youtu.be.")
    if not ID_PATTERN.fullmatch(video_id):
        raise ValueError("Не удалось определить ID видео.")
    return "https://www.youtube.com/watch?v=" + video_id, video_id


def check_video(info):
    if not isinstance(info, dict):
        raise ValueError("Информация о видео недоступна.")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise ValueError("Прямые эфиры не поддерживаются.")
    if info.get("duration") is not None and info["duration"] > 3600:
        raise ValueError("Максимальная длина видео — 60 минут.")


def match_video(info, **kwargs):
    try:
        check_video(info)
    except ValueError as exc:
        return str(exc)
    return None


def progress(data):
    if data.get("status") == "downloading" and data.get("downloaded_bytes", 0) > MAX_SIZE:
        raise DownloadError("Размер аудио превышает лимит.")


def settings(**kwargs):
    return dict(quiet=True, no_warnings=True, noplaylist=True, ignoreconfig=True,
                cachedir=False, format="bestaudio", max_filesize=MAX_SIZE,
                socket_timeout=20, retries=2, fragment_retries=2, **kwargs)


@app.before_request
def login():
    if request.path == "/healthz" or os.getenv("PUBLIC_DEPLOYMENT") != "1":
        return None
    secret = os.getenv("APP_PASSWORD", "")
    if not secret:
        return Response("Set APP_PASSWORD on hosting.", status=503)
    auth = request.authorization
    if not (auth and auth.type.lower() == "basic"
            and secrets.compare_digest(auth.username or "", "sonora")
            and secrets.compare_digest(auth.password or "", secret)):
        return Response("Требуется пароль SONORA.", status=401,
                        headers={"WWW-Authenticate": 'Basic realm="SONORA", charset="UTF-8"'})
    return None


@app.after_request
def headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/info")
def info():
    try:
        url, video_id = canonical_url(request.args.get("url"))
        with YoutubeDL(settings(skip_download=True)) as ydl:
            data = ydl.extract_info(url, download=False)
        check_video(data)
        return jsonify(title=data.get("title") or "Без названия",
                       channel=data.get("channel") or data.get("uploader") or "YouTube",
                       duration=data.get("duration"),
                       thumbnail="https://i.ytimg.com/vi/" + video_id + "/hqdefault.jpg")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except DownloadError:
        return jsonify(error="Не удалось получить информацию от YouTube."), 502
    except Exception:
        app.logger.exception("Video info error")
        return jsonify(error="Ошибка при чтении данных видео."), 500


@app.post("/api/download")
def download():
    params = request.get_json(silent=True)
    if not isinstance(params, dict):
        return jsonify(error="Ожидались параметры файла."), 400
    try:
        url, video_id = canonical_url(params.get("url"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    output_format = params.get("format", "original")
    if output_format not in ("original", "mp3_320", "mp3_192"):
        return jsonify(error="Неверный формат."), 400
    if output_format != "original" and not shutil.which("ffmpeg"):
        return jsonify(error="FFmpeg не найден на сервере."), 503
    if not SLOT.acquire(blocking=False):
        return jsonify(error="Другая загрузка уже выполняется."), 429
    try:
        with tempfile.TemporaryDirectory(prefix="sonora_") as folder:
            opts = settings(outtmpl=str(Path(folder) / "%(id)s.%(ext)s"),
                            progress_hooks=[progress], match_filter=match_video)
            if output_format != "original":
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                            "preferredcodec": "mp3",
                                            "preferredquality": output_format.split("_")[1]}]
            with YoutubeDL(opts) as ydl:
                data = ydl.extract_info(url, download=True)
            check_video(data)
            extensions = (".mp3",) if output_format != "original" else (
                ".webm", ".m4a", ".opus", ".ogg", ".aac", ".mp3")
            candidates = [p for p in Path(folder).glob(video_id + ".*")
                          if p.is_file() and p.suffix.lower() in extensions]
            if not candidates:
                return jsonify(error="Аудиофайл не создан."), 502
            path = max(candidates, key=lambda p: p.stat().st_size)
            if path.stat().st_size > MAX_SIZE:
                return jsonify(error="Файл превышает 100 МБ."), 413
            ext = path.suffix.lower()[1:]
            name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "", str(data.get("title") or "audio"))
            name = (name.strip().strip(".")[:85] or "audio") + "." + ext
            mime = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "webm": "audio/webm",
                    "opus": "audio/ogg", "ogg": "audio/ogg", "aac": "audio/aac"}.get(
                        ext, "application/octet-stream")
            return send_file(io.BytesIO(path.read_bytes()), mimetype=mime,
                             as_attachment=True, download_name=name)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except DownloadError:
        return jsonify(error="YouTube не отдал аудио. Возможно, запросы облачного сервера ограничены."), 502
    except Exception:
        app.logger.exception("Download error")
        return jsonify(error="Ошибка обработки аудио."), 500
    finally:
        SLOT.release()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
