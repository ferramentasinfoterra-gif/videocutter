#!/usr/bin/env python3
import http.server, json, os, re, shutil, subprocess, tempfile, threading, traceback, uuid, zipfile
from urllib.parse import urlparse

PORT   = int(os.environ.get("PORT", 8765))
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videocutter.html")

JOBS  = {}
FILES = {}
RECENT_ERRORS = []

# Lazy-load Whisper (tiny model)
WHISPER_MODEL = None
WHISPER_LOCK  = threading.Lock()


def get_whisper():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        with WHISPER_LOCK:
            if WHISPER_MODEL is None:
                print("[whisper] loading tiny model…", flush=True)
                from faster_whisper import WhisperModel
                WHISPER_MODEL = WhisperModel("tiny", device="cpu", compute_type="int8")
                print("[whisper] ready", flush=True)
    return WHISPER_MODEL


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
            out    = job["output"]
            is_zip = job.get("output_type") == "zip"
            size   = os.path.getsize(out)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip" if is_zip else "video/mp4")
            self.send_header("Content-Length", size)
            fname = "cortes.zip" if is_zip else "video_final.mp4"
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.cors()
            self.end_headers()
            with open(out, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

        elif path == "/debug":
            self.send_json(200, {
                "errors": RECENT_ERRORS,
                "jobs": {k: {kk: vv for kk, vv in v.items() if kk not in ("output", "srt")}
                         for k, v in list(JOBS.items())[-10:]}
            })

        else:
            self.send_json(404, {"error": "não encontrado"})

    def do_POST(self):
        path = urlparse(self.path).path

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

        elif path == "/import-url":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json(400, {"error": "JSON inválido"})
                return
            url = (data.get("url") or "").strip()
            if not url or not (url.startswith("http://") or url.startswith("https://")):
                self.send_json(400, {"error": "URL inválida"})
                return
            job_id = str(uuid.uuid4())
            JOBS[job_id] = {"status": "queued", "progress": 0,
                            "message": "Preparando download…", "kind": "import"}
            threading.Thread(target=run_import_url, args=(job_id, url), daemon=True).start()
            self.send_json(200, {"job_id": job_id})

        elif path == "/transcribe":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json(400, {"error": "JSON inválido"})
                return
            file_id  = data.get("file_id")
            language = data.get("language") or "pt"
            fpath    = FILES.get(file_id)
            if not fpath or not os.path.exists(fpath):
                self.send_json(400, {"error": "arquivo não encontrado"})
                return
            job_id = str(uuid.uuid4())
            JOBS[job_id] = {"status": "queued", "progress": 0,
                            "message": "Preparando transcrição…", "kind": "transcribe"}
            threading.Thread(target=run_transcribe, args=(job_id, fpath, language), daemon=True).start()
            self.send_json(200, {"job_id": job_id})

        elif path == "/process":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                self.send_json(400, {"error": "JSON inválido"})
                return

            clips = data.get("clips", [])
            mode  = data.get("mode", "join")
            if not clips:
                self.send_json(400, {"error": "nenhum clipe"})
                return

            clips_data = []
            for c in clips:
                fpath = FILES.get(c.get("file_id"))
                if not fpath or not os.path.exists(fpath):
                    self.send_json(400, {"error": f"arquivo não encontrado: {c.get('file_id')}"})
                    return
                clips_data.append({
                    "path": fpath,
                    "start": c["start"], "end": c["end"],
                    "width": c.get("width", 0), "height": c.get("height", 0),
                    "srt": c.get("srt") or None,
                    "headline": (c.get("headline") or "").strip() or None,
                })

            job_id = str(uuid.uuid4())
            JOBS[job_id] = {
                "status": "queued", "progress": 0, "message": "Aguardando…",
                "output": None, "output_type": "zip" if mode == "separate" else "mp4",
                "kind": "process",
            }
            threading.Thread(target=run_job, args=(job_id, clips_data, mode), daemon=True).start()
            self.send_json(200, {"job_id": job_id})

        else:
            self.send_json(404, {"error": "rota não encontrada"})


# ── SRT parsing ─────────────────────────────────────────────────────────────
_SRT_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)

def parse_srt(text):
    entries = []
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        ts_idx = 0
        if "-->" not in lines[0] and "-->" in lines[1]:
            ts_idx = 1
        if ts_idx >= len(lines) or "-->" not in lines[ts_idx]:
            continue
        m = _SRT_TIME_RE.search(lines[ts_idx])
        if not m:
            continue
        s = int(m[1])*3600 + int(m[2])*60 + int(m[3]) + int(m[4])/1000.0
        e = int(m[5])*3600 + int(m[6])*60 + int(m[7]) + int(m[8])/1000.0
        txt = " ".join(lines[ts_idx+1:]).strip()
        if txt and e > s:
            entries.append((s, e, txt))
    return entries


def filter_shift_srt(entries, cut_start, cut_end):
    out = []
    for s, e, t in entries:
        if e <= cut_start or s >= cut_end:
            continue
        ns = max(0.0, s - cut_start)
        ne = min(cut_end - cut_start, e - cut_start)
        if ne > ns + 0.05:
            out.append((ns, ne, t))
    return out


def fmt_ass_time(secs):
    h  = int(secs // 3600)
    m  = int((secs % 3600) // 60)
    s  = int(secs % 60)
    cs = int(round((secs - int(secs)) * 100))
    if cs >= 100: cs = 99
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def fmt_srt_time(secs):
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = int(secs % 60)
    ms = int(round((secs - int(secs)) * 1000))
    if ms >= 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def write_ass_reels(entries, path, video_w, video_h, words_per_group=2):
    """Estilo Reels: grupos de N palavras, grande, maiúscula, na parte inferior."""
    fs      = max(36, int(video_h * 0.065))
    mv      = int(video_h * 0.22)
    outline = max(3, int(fs * 0.12))
    shadow  = max(1, int(fs * 0.05))

    header = (
        f"[Script Info]\n"
        f"Title: Reels\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        f"WrapStyle: 2\n"
        f"ScaledBorderAndShadow: yes\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Reels,DejaVu Sans,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,60,60,{mv},1\n\n"
        f"[Events]\n"
        f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events = []
    for s, e, text in entries:
        words = text.split()
        if not words:
            continue
        duration = e - s
        groups = [words[i:i+words_per_group] for i in range(0, len(words), words_per_group)]
        if not groups:
            continue
        per = duration / len(groups)
        for i, grp in enumerate(groups):
            gs = s + i * per
            ge = s + (i + 1) * per if i < len(groups) - 1 else e
            line = ass_escape(" ".join(grp).upper())
            events.append(
                f"Dialogue: 0,{fmt_ass_time(gs)},{fmt_ass_time(ge)},Reels,,0,0,0,,{line}"
            )

    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(events) + "\n")


def write_ass_headline(text, duration, path, video_w, video_h):
    """Headline no topo do vídeo durante o trecho inteiro."""
    fs      = max(28, int(video_h * 0.048))
    mv_top  = int(video_h * 0.06)
    outline = max(3, int(fs * 0.11))
    shadow  = max(1, int(fs * 0.04))
    # Cor accent #E8FF47 em ASS BGR = &H0047FFE8
    header = (
        f"[Script Info]\n"
        f"Title: Headline\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {video_w}\n"
        f"PlayResY: {video_h}\n"
        f"WrapStyle: 2\n"
        f"ScaledBorderAndShadow: yes\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Headline,DejaVu Sans,{fs},&H0047FFE8,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},8,60,60,{mv_top},1\n\n"
        f"[Events]\n"
        f"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{fmt_ass_time(duration)},Headline,,0,0,0,,{ass_escape(text.upper())}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)


def escape_for_filter(path):
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\\\'")


# ── URL import job (yt-dlp) ─────────────────────────────────────────────────
def run_import_url(job_id, url):
    job = JOBS[job_id]
    try:
        job.update(status="running", progress=5, message="Obtendo informações…")

        # Obter título do vídeo (rápido)
        title = "video"
        try:
            t = subprocess.run(
                ["yt-dlp", "--get-title", "--no-playlist", "--no-warnings", url],
                capture_output=True, timeout=30,
            )
            if t.returncode == 0:
                title = (t.stdout.decode("utf-8", errors="replace").strip() or "video")[:120]
        except Exception:
            pass

        job.update(progress=15, message=f"Baixando “{title}”…")
        file_id  = str(uuid.uuid4())
        out_path = os.path.join(tempfile.gettempdir(), f"vc_{file_id}.mp4")

        # Baixa com yt-dlp — prefere MP4 direto, senão mescla melhores streams
        dl_cmd = [
            "yt-dlp",
            "-f", "best[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/b",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--no-warnings",
            "-o", out_path,
            url,
        ]
        r = subprocess.run(dl_cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(err or "falha ao baixar")

        if not os.path.exists(out_path):
            raise RuntimeError("arquivo baixado não encontrado")

        job.update(progress=85, message="Analisando vídeo…")
        width, height, duration = 1280, 720, 0.0
        try:
            probe = subprocess.run([
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json", out_path,
            ], capture_output=True, timeout=30)
            pdata = json.loads(probe.stdout.decode("utf-8", errors="replace"))
            st = (pdata.get("streams") or [{}])[0]
            width  = int(st.get("width")  or 1280)
            height = int(st.get("height") or 720)
            duration = float((pdata.get("format") or {}).get("duration") or 0.0)
        except Exception as pe:
            print(f"[import {job_id[:8]}] probe error: {pe}", flush=True)

        FILES[file_id] = out_path
        size_mb = os.path.getsize(out_path) / 1024 / 1024

        job.update(
            status="done", progress=100,
            message=f"{title} • {size_mb:.1f} MB",
            file_id=file_id, name=title,
            width=width, height=height, duration=duration,
        )
        print(f"[import {job_id[:8]}] done: {title} {width}x{height} {duration:.1f}s", flush=True)

    except subprocess.TimeoutExpired:
        job.update(status="error", message="timeout: o vídeo é muito longo ou a conexão está lenta")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[import {job_id[:8]}] EXCEPTION:\n{tb}", flush=True)
        RECENT_ERRORS.append({"job": job_id, "error": str(e), "kind": "import"})
        if len(RECENT_ERRORS) > 20:
            RECENT_ERRORS.pop(0)
        job.update(status="error", message=str(e)[-500:])


# ── Transcribe job ──────────────────────────────────────────────────────────
def run_transcribe(job_id, video_path, language="pt"):
    job = JOBS[job_id]
    tmpdir = tempfile.mkdtemp(prefix="vctrans_")
    try:
        job.update(status="running", progress=10, message="Extraindo áudio…")
        audio_path = os.path.join(tmpdir, "audio.mp3")
        r = subprocess.run([
            FFMPEG, "-y", "-i", video_path,
            "-vn", "-acodec", "mp3", "-ab", "64k",
            "-ar", "16000", "-ac", "1",
            audio_path,
        ], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", errors="replace")[-800:])

        job.update(progress=25, message="Carregando Whisper…")
        model = get_whisper()

        job.update(progress=35, message="Transcrevendo (pode levar alguns minutos)…")
        segments, info = model.transcribe(
            audio_path, language=language, beam_size=1, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )

        lines = []
        idx = 0
        for seg in segments:
            idx += 1
            text = seg.text.strip()
            if not text:
                idx -= 1
                continue
            lines.append(f"{idx}")
            lines.append(f"{fmt_srt_time(seg.start)} --> {fmt_srt_time(seg.end)}")
            lines.append(text)
            lines.append("")
            # approx progress update
            if info.duration and seg.end:
                pct = 35 + int((seg.end / info.duration) * 60)
                job["progress"] = min(95, pct)

        srt = "\n".join(lines).strip() + "\n"
        job.update(status="done", progress=100, message=f"{idx} legendas geradas", srt=srt)
        print(f"[whisper {job_id[:8]}] done, {idx} segments", flush=True)

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[whisper {job_id[:8]}] EXCEPTION:\n{tb}", flush=True)
        RECENT_ERRORS.append({"job": job_id, "error": str(e), "kind": "transcribe"})
        if len(RECENT_ERRORS) > 20:
            RECENT_ERRORS.pop(0)
        job.update(status="error", message=str(e)[-500:])
    finally:
        try: shutil.rmtree(tmpdir, ignore_errors=True)
        except: pass


# ── Video processing job ────────────────────────────────────────────────────
def run_job(job_id, clips, mode="join"):
    job    = JOBS[job_id]
    tmpdir = tempfile.mkdtemp(prefix="vcjob_")
    segs   = []
    total  = len(clips)

    try:
        tw = int(clips[0].get("width")  or 1280)
        th = int(clips[0].get("height") or 720)
        tw = tw if tw % 2 == 0 else tw - 1
        th = th if th % 2 == 0 else th - 1

        print(f"[job {job_id[:8]}] mode={mode} target={tw}x{th} clips={total}", flush=True)

        srt_stats = []  # per-clip debug info

        for i, clip in enumerate(clips):
            src      = clip["path"]
            start    = float(clip["start"])
            end      = float(clip["end"])
            srt      = clip.get("srt")
            headline = clip.get("headline")
            out      = os.path.join(tmpdir, f"seg_{i:03d}.mp4")
            segs.append(out)

            job["status"]   = "running"
            job["progress"] = 5 + int(i / total * 80)
            job["message"]  = f"Cortando trecho {i+1}/{total}…"

            vf_parts = [
                f"scale={tw}:{th}:force_original_aspect_ratio=decrease",
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black",
                "setsar=1",
            ]

            clip_debug = {"i": i, "srt_len": len(srt) if srt else 0}

            # SRT → ASS legendas Reels (inferior)
            if srt:
                try:
                    entries = parse_srt(srt)
                    clip_debug["parsed"] = len(entries)
                    entries = filter_shift_srt(entries, start, end)
                    clip_debug["after_filter"] = len(entries)
                    if entries:
                        ass_path = os.path.join(tmpdir, f"seg_{i:03d}_sub.ass")
                        write_ass_reels(entries, ass_path, tw, th)
                        # IMPORTANT: use forward-slash path, single-quoted, for ass filter
                        vf_parts.append(f"ass='{ass_path}'")
                        clip_debug["sub_applied"] = True
                        clip_debug["ass_path"] = ass_path
                        print(f"[job {job_id[:8]}] seg {i}: SRT applied ({len(entries)} entries)", flush=True)
                    else:
                        clip_debug["sub_applied"] = False
                        clip_debug["reason"] = "no entries after filter/parse"
                except Exception as srt_err:
                    clip_debug["sub_error"] = str(srt_err)
                    print(f"[job {job_id[:8]}] SRT error seg {i}: {srt_err}", flush=True)
                    tb = traceback.format_exc()
                    print(tb, flush=True)

            # Headline → ASS (topo) durante todo o trecho
            if headline:
                try:
                    ass_hl = os.path.join(tmpdir, f"seg_{i:03d}_hl.ass")
                    write_ass_headline(headline, end - start, ass_hl, tw, th)
                    vf_parts.append(f"ass='{ass_hl}'")
                    clip_debug["headline_applied"] = True
                    print(f"[job {job_id[:8]}] seg {i}: headline applied", flush=True)
                except Exception as hl_err:
                    clip_debug["headline_error"] = str(hl_err)
                    print(f"[job {job_id[:8]}] headline error seg {i}: {hl_err}", flush=True)

            srt_stats.append(clip_debug)
            vf = ",".join(vf_parts)
            print(f"[job {job_id[:8]}] seg {i} vf={vf}", flush=True)

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
            print(f"[job {job_id[:8]}] seg {i}: ss={start} to={end}", flush=True)
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", errors="replace")
                print(f"[job {job_id[:8]}] FFmpeg error:\n{err}", flush=True)
                raise RuntimeError(err[-1200:])

        job["progress"] = 88

        if mode == "separate":
            job["message"] = "Compactando arquivos…"
            zip_path = os.path.join(tmpdir, "cortes.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, seg in enumerate(segs):
                    zf.write(seg, f"corte_{i+1:02d}.mp4")
            size_mb = os.path.getsize(zip_path) / 1024 / 1024
            job.update(status="done", progress=100,
                       message=f"{total} corte{'s' if total>1 else ''} • {size_mb:.1f} MB",
                       output=zip_path, output_type="zip", srt_debug=srt_stats)
        else:
            job["message"] = "Unindo trechos…"
            if total == 1:
                final = segs[0]
            else:
                lst = os.path.join(tmpdir, "list.txt")
                with open(lst, "w") as f:
                    for s in segs:
                        f.write(f"file '{s}'\n")
                final = os.path.join(tmpdir, "output.mp4")
                r2 = subprocess.run([
                    FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", final,
                ], capture_output=True)
                if r2.returncode != 0:
                    err2 = r2.stderr.decode("utf-8", errors="replace")
                    print(f"[job {job_id[:8]}] concat error:\n{err2}", flush=True)
                    raise RuntimeError(err2[-1200:])
            size_mb = os.path.getsize(final) / 1024 / 1024
            job.update(status="done", progress=100,
                       message=f"{total} trecho{'s' if total>1 else ''} • {size_mb:.1f} MB",
                       output=final, output_type="mp4", srt_debug=srt_stats)

        print(f"[job {job_id[:8]}] done", flush=True)

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
