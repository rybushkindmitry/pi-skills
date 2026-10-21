#!/usr/bin/env python3
"""Подготовка YouTube-видео к анализу: субтитры + кадры + коллажи.

Использование:
    python3 prepare.py <URL> [--out DIR] [--interval N] [--dedup N]
                       [--lang en,ru] [--max-height 720|360]

Делает (всё локально, без LLM):
  1. метаданные видео (название, канал, длительность)
  2. субтитры: построчные + агрегированные (~60 сек на абзац)
  3. видео в низком разрешении (только видеопоток)
  4. кадры с шагом --interval сек -> дедупликация (imagehash, порог --dedup)
  5. коллажи 2x2 из уникальных кадров с подписями (файл, таймкод)

Результат печатается в stdout как сводка; файлы — в --out (по умолчанию
/tmp/video-digest-<video_id>/). Повторный запуск переиспользует скачанное.

Первый запуск сам создаёт venv (~/.cache/video-digest/venv) и ставит
зависимости (yt-dlp, youtube-transcript-api, imagehash, pillow).
"""
import argparse
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

VENV = Path.home() / ".cache" / "video-digest" / "venv"
PY = VENV / "bin" / "python"


def ensure_venv() -> None:
    """Создаёт venv с зависимостями при первом запуске и перезапускается в нём."""
    if sys.executable == str(PY):
        return
    if not PY.exists():
        print("Первый запуск: создаю venv и ставлю зависимости...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        subprocess.run([str(VENV / "bin" / "pip"), "install", "-q",
                        "yt-dlp", "youtube-transcript-api", "imagehash", "pillow",
                        "curl_cffi"], check=True)
    os_exec = [str(PY), __file__] + sys.argv[1:]
    raise SystemExit(subprocess.run(os_exec).returncode)


def video_id(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "v" in q:
        return q["v"][0]
    m = re.search(r"(youtu\.be/|/shorts/|/embed/)([\w-]{11})", url)
    return m.group(2) if m else re.sub(r"\W+", "-", url)[-24:]


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def ytdlp() -> str:
    """yt-dlp из venv скилла, fallback на PATH."""
    p = VENV / "bin" / "yt-dlp"
    return str(p) if p.exists() else "yt-dlp"


def fetch_meta(url: str, out: Path) -> dict:
    fmt = "%(title)s|%(channel)s|%(duration_string)s|%(upload_date)s"
    last = None
    for attempt in range(4):
        try:
            r = run([ytdlp(), "--skip-download", "--impersonate", "chrome",
                     "--print", fmt, url])
            break
        except subprocess.CalledProcessError as e:
            last = e
            tail = (e.stderr or "")[-200:]
            print(f"  yt-dlp (попытка {attempt+1}/4): {tail}", file=sys.stderr)
            import time
            time.sleep(5)
    else:
        raise SystemExit(f"yt-dlp не смог получить метаданные: {last}")
    title, channel, duration, upload = (r.stdout.strip().split("\n")[-1].split("|", 3)
                                        + ["?"] * 4)[:4]
    meta = {"title": title, "channel": channel, "duration": duration, "upload": upload}
    (out / "meta.txt").write_text(
        f"{title}\nКанал: {channel}\nДлительность: {duration}\nДата: {upload}\nURL: {url}\n")
    return meta


def fetch_subs(vid: str, url: str, out: Path, langs: str) -> str | None:
    """Субтитры: youtube-transcript-api -> fallback yt-dlp. Возвращает путь или None."""
    post = out / "subs.txt"
    para = out / "subs_para.txt"
    try:
        code = f"""
import sys
from youtube_transcript_api import YouTubeTranscriptApi
api = YouTubeTranscriptApi()
langs = "{langs}".split(",")
last = None
for attempt in range(5):
    try:
        tr = api.fetch("{vid}", languages=langs)
        break
    except Exception as e:
        last = e
        import time; time.sleep(5)
else:
    sys.exit(f"все попытки не удались: {{last}}")
with open(r"{post}", "w") as f:
    for s in tr:
        f.write(f"[{{int(s.start//60):02d}}:{{int(s.start%60):02d}}] {{s.text}}\\n")
print(len(tr.snippets))
"""
        r = run([sys.executable, "-c", code])
        print(f"  субтитры: {r.stdout.strip()} фрагментов (youtube-transcript-api)")
    except subprocess.CalledProcessError as e:
        print(f"  youtube-transcript-api не сработал ({e.stderr[-120:]}), fallback на yt-dlp",
              file=sys.stderr)
        try:
            run([ytdlp(), "--skip-download", "--impersonate", "chrome",
                 "--write-auto-subs", "--sub-langs", langs, "--sub-format", "vtt",
                 "-o", str(out / "vid"), url])
            import glob
            vtt = next(iter(glob.glob(str(out / "vid*.vtt"))), None)
            if not vtt:
                return None
            lines = []
            for ln in Path(vtt).read_text(errors="replace").splitlines():
                m = re.match(r"(\d+):(\d+)[.,]\d+ -->", ln)
                if m:
                    lines.append(f"[{int(m.group(1)):02d}:{m.group(2)}] ")
                elif ln.strip() and lines and not lines[-1].endswith(": "):
                    if not lines[-1].endswith("\n"):
                        lines[-1] += ln.strip() + "\n"
            Path(post).write_text("".join(lines))
        except subprocess.CalledProcessError:
            return None
    # агрегация: ~60 сек на абзац (таймкоды могут быть > 59 мин — парсим полностью)
    paras, buf, base = [], [], None
    for ln in post.read_text().splitlines():
        if "] " not in ln:
            continue
        ts, text = ln[1:].split("] ", 1)
        mm, ss = ts.split(":")
        t = int(mm) * 60 + int(ss)
        if base is None:
            base = t
        buf.append(text)
        if t - base >= 60:
            paras.append(f"[{base//60:02d}:{base%60:02d}] " + " ".join(buf))
            buf, base = [], None
    if buf:
        paras.append(f"[{base//60:02d}:{base%60:02d}] " + " ".join(buf))
    para.write_text("\n".join(paras))
    return para.name


def fetch_video(url: str, out: Path, max_height: int) -> Path | None:
    try:
        run([ytdlp(), "-f", f"bv*[height<={max_height}][ext=mp4]/w",
             "--impersonate", "chrome", "-o", str(out / "video.%(ext)s"), url])
        return next(iter(out.glob("video.*")), None)
    except subprocess.CalledProcessError as e:
        print(f"  видео не скачалось: {e.stderr[-150:]}", file=sys.stderr)
        return None


def frames_grid(video: Path | None, out: Path, interval: int, dedup: int) -> int:
    if not video:
        return 0
    fdir = out / "frames"
    fdir.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", video, "-vf", f"fps=1/{interval}", "-q:v", "2",
                    str(fdir / "f_%03d.jpg")], check=True)
    code = f"""
import os, sys
from PIL import Image, ImageDraw
import imagehash
d = r"{fdir}"
files = sorted(os.listdir(d))
kept, prev = [], None
for f in files:
    h = imagehash.phash(Image.open(os.path.join(d, f)))
    if prev is not None and (h - prev) <= {dedup}:
        continue
    kept.append(f); prev = h
W, H = Image.open(os.path.join(d, files[0])).size
W = W // 2 if W > 1400 else W; H = W // 2 if W > 1400 else W
gdir = r"{out / 'grids'}"
os.makedirs(gdir, exist_ok=True)
per = 4
n = (len(kept) + per - 1) // per
for g in range(n):
    sheet = Image.new("RGB", (W*2, H*2), "black")
    dr = ImageDraw.Draw(sheet)
    for i, f in enumerate(kept[g*per:(g+1)*per]):
        im = Image.open(os.path.join(d, f))
        if im.size != (W, H):
            im = im.resize((W, H))
        x, y = (i % 2) * W, (i // 2) * H
        sheet.paste(im, (x, y))
        sec = (int(f[2:5]) - 1) * {interval}
        dr.rectangle([x+4, y+4, x+260, y+30], fill="black")
        dr.text((x+10, y+8), f"{{f}} {{sec//60:02d}}:{{sec%60:02d}}", fill="yellow")
    sheet.save(os.path.join(gdir, f"g{{g+1:02d}}.jpg"), quality=82)
with open(r"{out / 'frames_kept.txt'}", "w") as o:
    for f in kept:
        sec = (int(f[2:5]) - 1) * {interval}
        o.write(f"{{f}} t={{sec//60:02d}}:{{sec%60:02d}}\\n")
print(f"{{len(kept)}}/{{len(files)}} уникальных, {{n}} коллажей")
"""
    r = run([sys.executable, "-c", code])
    print(f"  кадры: {r.stdout.strip()}")
    return len(list((out / "grids").glob("*.jpg")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--out", default=None)
    ap.add_argument("--interval", type=int, default=10,
                    help="шаг кадров, сек (доклад/презентация: 10; интервью/подкаст: 30)")
    ap.add_argument("--dedup", type=int, default=5,
                    help="порог imagehash (доклад: 5; интервью: 8)")
    ap.add_argument("--lang", default="en,ru", help="языки субтитров через запятую")
    ap.add_argument("--max-height", type=int, default=720,
                    help="высота видео (многочасовое: 360)")
    a = ap.parse_args()
    ensure_venv()
    vid = video_id(a.url)
    out = Path(a.out) if a.out else Path(f"/tmp/video-digest-{vid}")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Видео {vid} -> {out}")
    meta = fetch_meta(a.url, out)
    print(f"  «{meta['title']}» — {meta['channel']}, {meta['duration']}, {meta['upload']}")
    subs = fetch_subs(vid, a.url, out, a.lang)
    print(f"  субтитры: {'subs_para.txt (агрегат) + subs.txt (построчно)' if subs else 'НЕ НАЙДЕНЫ — конспект только по кадрам'}")
    video = fetch_video(a.url, out, a.max_height)
    grids = frames_grid(video, out, a.interval, a.dedup)
    print("\n=== ГОТОВО. Дальше: ===")
    if subs:
        print(f"  1. прочитай {out / 'subs_para.txt'} (агрегат по минутам)")
    print(f"  2. просматривай коллажи {out / 'grids'}/g*.jpg пачками по 3 (read)")
    print(f"  3. ключевые слайды — полным кадром из {out / 'frames'}/")
    print(f"     список уникальных: {out / 'frames_kept.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
