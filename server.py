#!/usr/bin/env python3
import http.server, json, os, re, shutil, subprocess, tempfile, threading, traceback, uuid
from urllib.parse import urlparse

PORT  = int(os.environ.get("PORT", 8765))
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videocutter.html")

JOBS  = {}   # job_id  -> dict
FILES = {}   # file_id -> path
RECENT_ERRORS = []  # last 20 errors for /debug


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/videocutter.html"):
            with open(HTML_FILE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path.startswith("/status/"):
            job_id = path[len("/status/"):]
            job = JOBS.get(job_id)
            self.send_json(200 if job else 404, job or {"error": "não encontrado"})

        elif path.startswith("/download/"):
            job_id = path[len("/download/"):]
            job = JOBS.get(job_id)
            if not job or job["status"] != "done" or not os.path.exists(job.get("output", "")):
                self.send_json(404, {"error": "arquivo não disponível"})
                return
            out = job["output"]
            size = os.path.getsize(out)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", size)
            self.send_header("Content-Disposition", 'attachment; filename="video_final.mp4"')
            self.cors()
            self.end_headers()
            with open(out, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

        elif path == "/debug":
            self.send_json(200, {"errors": RECENT_ERRORS, "jobs": {k: v for k, v in list(JOBS.items())[-10:]}})

        else:
            self.send_json(404, {"error": "não encontrado"})

    # ── POST ─────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path

        # Upload de um arquivo (stream direto para disco — não bufferiza em RAM)
        if path == "/upload":
            length   = int(self.headers.get("Content-Length", 0))
            filename = self.headers.get("X-Filename", "video.mp4")
            ext      = os.path.splitext(filename)[1] or ".mp4"
            file_id  = str(uuid.uuid4())
            tmp_path = os.path.join(tempfile.gettempdir(), f"vc_{file_id}{ext}")

            with open(tmp_path, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)

            FILES[file_id] = tmp_path
            self.send_json(200, {"file_id": file_id})

        # Inicia processamento
        elif path == "/process":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json(400, {"error": "JSON inválido"})
                return

            clips = data.get("clips", [])
            if not clips:
                self.send_json(400, {"error": "nenhum clipe"})
                return

            # Resolve file_id → path
            clips_data = []
            for c in clips:
                fpath = FILES.get(c.get("file_id"))
                if not fpath or not os.path.exists(fpath):
                    self.send_json(400, {"error": f"arquivo não encontrado: {c.get('file_id')}"})
                    return
                clips_data.append({"path": fpath, "start": c["start"], "end": c["end"],
                                   "width": c.get("width", 0), "height": c.get("height", 0)})

            job_id = str(uuid.uuid4())
            JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Aguardando…", "output": None}
            threading.Thread(target=run_job, args=(job_id, clips_data), daemon=True).start()
            self.send_json(200, {"job_id": job_id})

        else:
            self.send_json(404, {"error": "rota não encontrada"})


def run_job(job_id, clips):
    job    = JOBS[job_id]
    tmpdir = tempfile.mkdtemp(prefix="vcjob_")
    segs   = []
    total  = len(clips)

    try:
        # Use dimensions reported by the browser (videoWidth/videoHeight)
        tw = int(clips[0].get("width") or 1280)
        th = int(clips[0].get("height") or 720)

        # Make dimensions even (required by libx264)
        tw = tw if tw % 2 == 0 else tw - 1
        th = th if th % 2 == 0 else th - 1

        print(f"[job {job_id[:8]}] target={tw}x{th}, clips={total}", flush=True)

        for i, clip in enumerate(clips):
            src   = clip["path"]
            start = float(clip["start"])
            end   = float(clip["end"])
            out   = os.path.join(tmpdir, f"seg_{i}.mp4")
            segs.append(out)

            job["status"]   = "running"
            job["progress"] = 5 + int(i / total * 80)
            job["message"]  = f"Cortando clipe {i+1}/{total}…"

            vf = (
                f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1"
            )

            cmd = [
                FFMPEG, "-y",
                "-ss", str(start), "-to", str(end),
                "-i", src,
                "-vf", vf,
                "-c:v", "libx264", "-c:a", "aac",
                "-preset", "ultrafast", "-crf", "23",
                "-avoid_negative_ts", "make_zero",
                "-reset_timestamps", "1",
                out,
            ]
            print(f"[job {job_id[:8]}] seg {i}: {' '.join(cmd)}", flush=True)

            r = subprocess.run(cmd, capture_output=True)

            if r.returncode != 0:
                err = r.stderr.decode("utf-8", errors="replace")
                print(f"[job {job_id[:8]}] FFmpeg error seg {i}:\n{err}", flush=True)
                raise RuntimeError(err[-1200:])

        job["progress"] = 88
        job["message"]  = "Unindo clipes…"

        if total == 1:
            final = segs[0]
        else:
            lst = os.path.join(tmpdir, "list.txt")
            with open(lst, "w") as f:
                for s in segs:
                    f.write(f"file '{s}'\n")
            final = os.path.join(tmpdir, "output.mp4")
            cmd2 = [
                FFMPEG, "-y", "-f", "concat", "-safe", "0",
                "-i", lst, "-c", "copy", final,
            ]
            print(f"[job {job_id[:8]}] concat: {' '.join(cmd2)}", flush=True)
            r2 = subprocess.run(cmd2, capture_output=True)
            if r2.returncode != 0:
                err2 = r2.stderr.decode("utf-8", errors="replace")
                print(f"[job {job_id[:8]}] FFmpeg concat error:\n{err2}", flush=True)
                raise RuntimeError(err2[-1200:])

        size_mb = os.path.getsize(final) / 1024 / 1024
        job.update(status="done", progress=100,
                   message=f"{total} clipe{'s' if total>1 else ''} • {size_mb:.1f} MB",
                   output=final)
        print(f"[job {job_id[:8]}] done, {size_mb:.1f} MB", flush=True)

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[job {job_id[:8]}] EXCEPTION:\n{tb}", flush=True)
        RECENT_ERRORS.append({"job": job_id, "error": str(e)})
        if len(RECENT_ERRORS) > 20:
            RECENT_ERRORS.pop(0)
        job.update(status="error", message=str(e)[-500:])


print(f"▶  VideoCutter → http://localhost:{PORT}")
with http.server.ThreadingHTTPServer(("", PORT), Handler) as s:
    s.serve_forever()
