import os, re, uuid, time, socket, ipaddress, threading
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, send_from_directory
import yt_dlp
from google.oauth2 import id_token
from google.auth.transport import requests as greq

BASE = os.path.dirname(os.path.abspath(__file__))
DL_DIR = os.path.join(BASE, "downloads")
os.makedirs(DL_DIR, exist_ok=True)
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
JOBS = {}
PROJECT = os.environ.get("FIREBASE_PROJECT_ID", "vidio-donlod")
GREQ = greq.Request()
MAX_ACTIVE = 2


def auth(f):
    """Wajib login: verifikasi Firebase ID token dari header Authorization."""
    @wraps(f)
    def w(*a, **k):
        h = request.headers.get("Authorization", "")
        try:
            claims = id_token.verify_firebase_token(h.split(" ", 1)[1], GREQ, audience=PROJECT)
            if not claims:
                raise ValueError("token kosong")
            request.uid = claims["sub"]
        except Exception:
            return jsonify(error="Sesi tidak valid. Silakan masuk lagi."), 401
        return f(*a, **k)
    return w


def safe_url(u):
    """Hanya http(s) ke host publik (cegah akses ke jaringan internal / SSRF)."""
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        for r in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(r[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except socket.gaierror:
        return False
    return True


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/info")
@auth
def info():
    url = (request.json or {}).get("url", "").strip()
    if not safe_url(url):
        return jsonify(error="Link tidak valid atau tidak diizinkan."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as y:
            d = y.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error="Sumber video tidak ditemukan: " + str(e)[:250]), 422
    heights = sorted({f["height"] for f in d.get("formats", [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"), duration=d.get("duration"),
                   uploader=d.get("uploader"), site=d.get("extractor_key"), heights=heights)


def run(jid, url, height, audio):
    def hook(d):
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            JOBS[jid].update(status="downloading",
                             pct=round(d["downloaded_bytes"] / tot * 100, 1) if tot else 0,
                             speed=(d.get("_speed_str") or "").strip(), eta=(d.get("_eta_str") or "").strip())
        elif d["status"] == "finished":
            JOBS[jid].update(status="processing", pct=100)

    if audio:
        fmt = "bestaudio/best"
    elif height:
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    else:
        fmt = "bv*+ba/b"
    opts = {"format": fmt, "outtmpl": os.path.join(DL_DIR, jid + ".%(ext)s"),
            "merge_output_format": "mp4", "noplaylist": True, "quiet": True,
            "progress_hooks": [hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
        files = [f for f in os.listdir(DL_DIR) if f.startswith(jid + ".")]
        path = os.path.join(DL_DIR, files[0])
        name = re.sub(r'[\\/:*?"<>|]', "_", d.get("title") or "video") + os.path.splitext(path)[1]
        JOBS[jid].update(status="done", pct=100, path=path, name=name, audio=bool(audio))
    except Exception as e:
        JOBS[jid].update(status="error", error=str(e)[:300])


@app.post("/api/prepare")
@auth
def prepare():
    j = request.json or {}
    url = j.get("url", "").strip()
    if not safe_url(url):
        return jsonify(error="Link tidak valid atau tidak diizinkan."), 400
    active = sum(1 for x in JOBS.values() if x.get("uid") == request.uid
                 and x["status"] in ("starting", "downloading", "processing"))
    if active >= MAX_ACTIVE:
        return jsonify(error="Maksimal 2 proses berjalan bersamaan. Tunggu salah satu selesai."), 429
    jid = uuid.uuid4().hex
    JOBS[jid] = {"status": "starting", "pct": 0, "uid": request.uid}
    threading.Thread(target=run, args=(jid, url, j.get("height"), j.get("audio")), daemon=True).start()
    return jsonify(id=jid)


@app.get("/api/progress/<jid>")
@auth
def progress(jid):
    j = JOBS.get(jid)
    if not j or j.get("uid") != request.uid:
        return jsonify(status="error", error="Tugas tidak ditemukan."), 404
    return jsonify({k: v for k, v in j.items() if k not in ("path", "uid")})


@app.get("/api/file/<jid>")
def file(jid):
    j = JOBS.get(jid)
    if not j or j.get("status") != "done":
        return "Belum siap", 404
    # conditional=True -> dukung Range, jadi video bisa di-seek saat diputar
    return send_file(j["path"], conditional=True, as_attachment=request.args.get("dl") == "1",
                     download_name=j["name"])


def cleanup():
    while True:
        time.sleep(600)
        for f in os.listdir(DL_DIR):
            p = os.path.join(DL_DIR, f)
            if time.time() - os.path.getmtime(p) > 3600:
                try:
                    os.remove(p)
                except OSError:
                    pass


threading.Thread(target=cleanup, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
