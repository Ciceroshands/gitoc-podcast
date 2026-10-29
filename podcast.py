#!/usr/bin/env python3
"""
GI-TOC report podcast generator.

Every run:
  1. Finds new reports published by the Global Initiative Against Transnational
     Organized Crime (globalinitiative.net).
  2. Sends each report's PDF to Gemini, which writes a two-host conversation.
  3. Voices that conversation with Gemini's multi-speaker text-to-speech.
  4. Saves an MP3, uploads it as a GitHub Release asset, and rebuilds a
     private podcast RSS feed in docs/feed.xml (served by GitHub Pages).

All settings come from environment variables (set in .github/workflows/daily.yml).
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import html
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import wave
from urllib.parse import urljoin, urlparse, urlunparse
from xml.sax.saxutils import escape

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DOCS_DIR = ROOT / "docs"
OUT_DIR = ROOT / "out"
SEEN_FILE = STATE_DIR / "seen.json"
EPISODES_FILE = STATE_DIR / "episodes.json"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
SAMPLE_RATE = 24000  # Gemini TTS: 24 kHz, mono, 16-bit PCM
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    value = os.getenv(name, "")
    return value.strip() if value.strip() else default


def env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


def env_bool(name: str, default: bool) -> bool:
    return env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    repo = env("GITHUB_REPOSITORY")
    base = env("PUBLIC_BASE_URL")
    if not base and "/" in repo:
        owner, name = repo.split("/", 1)
        base = f"https://{owner.lower()}.github.io/{name}/"
    if base and not base.endswith("/"):
        base += "/"
    return {
        "site": env("SITE_URL", "https://globalinitiative.net"),
        "feeds": env_list(
            "SOURCE_FEEDS",
            "https://globalinitiative.net/analysis/feed/,https://globalinitiative.net/feed/",
        ),
        "listings": env_list("LISTING_URLS", "https://globalinitiative.net/analysis/"),
        "reports_only": env_bool("REPORTS_ONLY", True),
        "max_new": int(env("MAX_NEW_PER_RUN", "2")),
        "backfill": int(env("FIRST_RUN_BACKFILL", "2")),
        "max_attempts": int(env("MAX_ATTEMPTS_PER_REPORT", "3")),
        "script_model": env("SCRIPT_MODEL", "gemini-3.8-flash"),
        "tts_model": env("TTS_MODEL", "gemini-3.8-flash-tts"),
        "host_a": env("HOST_A_NAME", "Maya"),
        "host_b": env("HOST_B_NAME", "Daniel"),
        "voice_a": env("HOST_A_VOICE", "Kore"),
        "voice_b": env("HOST_B_VOICE", "Charon"),
        "minutes": int(env("EPISODE_MINUTES", "12")),
        "tts_chunk_chars": int(env("TTS_CHUNK_CHARS", "4000")),
        "language": env("EPISODE_LANGUAGE", "English"),
        "focus": env("LISTENER_FOCUS", ""),
        "show_title": env("SHOW_TITLE", "Organized Crime Report Briefings"),
        "show_author": env("SHOW_AUTHOR", "Private feed"),
        "keep": int(env("KEEP_EPISODES", "150")),
        "base_url": base,
        "repo": repo,
        "api_key": env("GEMINI_API_KEY"),
        "dry_run": env_bool("DRY_RUN", False),
    }


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 1. Discovering reports
# --------------------------------------------------------------------------

def http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en;q=0.9"})
    return s


def norm_url(url: str) -> str:
    p = urlparse(url)
    path = p.path if p.path.endswith("/") or "." in p.path.rsplit("/", 1)[-1] else p.path + "/"
    return urlunparse((p.scheme or "https", p.netloc.lower().replace("www.", ""), path, "", "", ""))


def is_article_url(url: str, site: str) -> bool:
    p, s = urlparse(url), urlparse(site)
    if p.netloc.replace("www.", "") != s.netloc.replace("www.", ""):
        return False
    parts = [x for x in p.path.split("/") if x]
    return len(parts) == 2 and parts[0] == "analysis" and parts[1] not in ("feed", "page")


def discover(cfg: dict, session: requests.Session) -> list[dict]:
    """Return candidate article pages, newest first: [{url, title}]."""
    found: dict[str, dict] = {}

    for feed_url in cfg["feeds"]:
        try:
            r = session.get(feed_url, timeout=30)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            for e in parsed.entries:
                link = e.get("link")
                if link:
                    found.setdefault(norm_url(link), {"url": norm_url(link), "title": e.get("title", "")})
            log(f"Feed {feed_url}: {len(parsed.entries)} items")
        except Exception as exc:  # noqa: BLE001
            log(f"Feed {feed_url} unavailable ({exc})")

    if not found:
        for listing in cfg["listings"]:
            try:
                r = session.get(listing, timeout=30)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    url = norm_url(urljoin(listing, a["href"]))
                    if is_article_url(url, cfg["site"]):
                        title = " ".join(a.get_text(" ", strip=True).split())
                        found.setdefault(url, {"url": url, "title": title})
                log(f"Listing {listing}: {len(found)} article links")
            except Exception as exc:  # noqa: BLE001
                log(f"Listing {listing} unavailable ({exc})")

    return list(found.values())


TRANSLATION_HINT = re.compile(r"[-_ ](es|fr|ar|pt|ru|sp|esp|fra|french|spanish|arabic)[-_. ]", re.I)


def inspect_article(url: str, session: requests.Session) -> dict:
    """Fetch an article page: title, date, description, best PDF link, text."""
    r = session.get(url, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content", "").strip() if tag else ""

    title = meta("og:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "")
    title = re.sub(r"\s*[-|–]\s*Global Initiative.*$", "", title).strip()
    published = meta("article:published_time")
    description = meta("og:description") or meta("description")

    pdfs = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        host = urlparse(href).netloc.lower()
        own = host == "globalinitiative.net" or host.endswith(".globalinitiative.net")
        if own and urlparse(href).path.lower().endswith(".pdf") and href not in pdfs:
            pdfs.append(href)
    # Prefer English, non-summary versions.
    pdfs.sort(key=lambda h: (bool(TRANSLATION_HINT.search(urlparse(h).path + "-")),
                             "summary" in h.lower()))

    body = soup.find("article") or soup.find("main") or soup.body
    for tag in body.find_all(["script", "style", "nav", "footer", "aside", "form"]) if body else []:
        tag.decompose()
    text = " ".join(body.get_text(" ", strip=True).split()) if body else ""

    return {"url": url, "title": title, "published": published, "description": description,
            "pdfs": pdfs, "text": text}


def download_pdf(url: str, session: requests.Session, dest: pathlib.Path) -> pathlib.Path | None:
    r = session.get(url, timeout=120)
    r.raise_for_status()
    data = r.content
    if not data.startswith(b"%PDF"):
        log(f"  {url} did not return a PDF")
        return None
    if len(data) > 48 * 1024 * 1024:
        log(f"  PDF is {len(data) // 1_048_576} MB, above Gemini's 50 MB limit; skipping")
        return None
    dest.write_bytes(data)
    return dest


# --------------------------------------------------------------------------
# 2-3. Gemini: script + speech
# --------------------------------------------------------------------------

class QuotaExhausted(RuntimeError):
    """The free tier's daily allowance is used up; try again tomorrow."""


def gemini(cfg: dict, body: dict, timeout: int = 600) -> dict:
    last, last_status = None, None
    for attempt in range(5):
        try:
            r = requests.post(GEMINI_URL, json=body, timeout=timeout,
                              headers={"x-goog-api-key": cfg["api_key"],
                                       "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json()
            last, last_status = f"HTTP {r.status_code}: {r.text[:500]}", r.status_code
            if r.status_code == 429 and re.search(r"per ?day|PerDay|daily", r.text, re.I):
                raise QuotaExhausted(last)
            if r.status_code not in (408, 429, 500, 502, 503, 504):
                break
        except requests.RequestException as exc:
            last, last_status = str(exc), None
        wait = min(30 * 2 ** attempt, 240)
        log(f"  Gemini busy ({last[:120]}); retrying in {wait}s")
        time.sleep(wait)
    if last_status == 429:
        raise QuotaExhausted(last)
    raise RuntimeError(f"Gemini request failed: {last}")


def _output_content(resp: dict):
    steps = resp.get("steps") or []
    outputs = [s for s in steps if s.get("type") == "model_output"] or steps[-1:]
    for step in outputs:
        for item in step.get("content") or []:
            yield item


def extract_text(resp: dict) -> str:
    if isinstance(resp.get("output_text"), str) and resp["output_text"]:
        return resp["output_text"]
    return "".join(c.get("text", "") for c in _output_content(resp) if c.get("type") == "text")


def extract_audio(resp: dict) -> bytes:
    if isinstance(resp.get("output_audio"), dict) and resp["output_audio"].get("data"):
        return base64.b64decode(resp["output_audio"]["data"])
    chunks = [c for c in _output_content(resp) if c.get("type") == "audio" and c.get("data")]
    if not chunks:
        raise RuntimeError(f"No audio in Gemini response: {json.dumps(resp)[:400]}")
    return base64.b64decode(chunks[-1]["data"])


def to_pcm(audio: bytes) -> bytes:
    """Gemini returns raw PCM when asked, but handle a WAV container just in case."""
    if audio[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio)) as w:
            return w.readframes(w.getnframes())
    return audio


SCRIPT_PROMPT = """You are writing the script for a private podcast episode that explains a newly
published report from the Global Initiative Against Transnational Organized Crime (GI-TOC).
The report is attached{source_note}.

Two hosts discuss it: {a} and {b}. They are smart, curious and conversational, like a good
audio deep-dive: {a} tends to frame and explain, {b} asks sharp questions and pushes on
implications. Speak to an informed listener who follows organized crime, security and policy.

Requirements:
- Length: about {words} words of dialogue (roughly {minutes} minutes).
- Language: {language}.
- Open by naming the report and saying it is from GI-TOC. Briefly say who would care and why.
- Cover the main findings, the most striking evidence, numbers and cases, the actors and
  markets involved, regional context, and the report's recommendations.
- End with the takeaways and what to watch next.
- Use ONLY facts from the report. Do not invent statistics, names, quotes or events. If the
  report is uncertain about something, say so.
- Natural spoken style: contractions, short back-and-forth turns, occasional "right", "hm",
  reactions. No headings, no lists, no stage directions or sound effects in the text.
  You may rarely use the inline tags <short pause> or <laugh>; nothing else in angle brackets.
{focus}
Return ONLY a JSON object, no markdown fences, in exactly this shape:
{{"title": "short episode title",
  "summary": "2-3 sentence plain-text episode description",
  "turns": [{{"speaker": "{a}", "text": "..."}}, {{"speaker": "{b}", "text": "..."}}]}}
"""


def parse_script(raw: str, host_a: str, host_b: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Script response contained no JSON object")
    data = json.loads(raw[start:end + 1])
    turns = []
    for i, t in enumerate(data.get("turns") or []):
        text = " ".join(str(t.get("text", "")).split())
        if not text:
            continue
        speaker = str(t.get("speaker", "")).strip()
        if speaker.lower() == host_a.lower():
            speaker = host_a
        elif speaker.lower() == host_b.lower():
            speaker = host_b
        else:
            speaker = host_a if i % 2 == 0 else host_b
        # Strip any angle-bracket tags other than the two we allow.
        text = re.sub(r"<(?!short pause>|laugh>)[^>]*>", "", text)
        turns.append({"speaker": speaker, "text": text})
    if len(turns) < 4:
        raise ValueError(f"Script too short ({len(turns)} turns)")
    return {"title": str(data.get("title", "")).strip(),
            "summary": str(data.get("summary", "")).strip(),
            "turns": turns}


def write_script(cfg: dict, article: dict, pdf_path: pathlib.Path | None) -> dict:
    words = cfg["minutes"] * 150
    focus = f"- Listener's particular interest: {cfg['focus']}. Draw this out where the report supports it.\n" if cfg["focus"] else ""
    source_note = "" if pdf_path else " as web page text, because no PDF was available"
    prompt = SCRIPT_PROMPT.format(a=cfg["host_a"], b=cfg["host_b"], words=words,
                                  minutes=cfg["minutes"], language=cfg["language"],
                                  focus=focus, source_note=source_note)
    if pdf_path:
        doc = {"type": "document", "mime_type": "application/pdf",
               "data": base64.b64encode(pdf_path.read_bytes()).decode()}
    else:
        doc = {"type": "text", "text": f"REPORT TITLE: {article['title']}\n\n{article['text'][:200_000]}"}
    body = {"model": cfg["script_model"], "input": [doc, {"type": "text", "text": prompt}]}
    last_err = None
    for _ in range(2):  # one retry if the JSON comes back malformed
        resp = gemini(cfg, body)
        try:
            return parse_script(extract_text(resp), cfg["host_a"], cfg["host_b"])
        except (ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            log(f"  Script parse failed ({exc}); asking again")
    raise RuntimeError(f"Could not get a usable script: {last_err}")


def chunk_turns(turns: list[dict], max_chars: int = 1500, max_turns: int = 40) -> list[list[dict]]:
    """Group turns into TTS-sized requests without splitting a turn."""
    chunks, current, size = [], [], 0
    for t in turns:
        n = len(t["text"])
        if current and (size + n > max_chars or len(current) >= max_turns):
            chunks.append(current)
            current, size = [], 0
        current.append(t)
        size += n
    if current:
        chunks.append(current)
    return chunks


def speak(cfg: dict, turns: list[dict]) -> bytes:
    """Voice the whole dialogue; returns raw 24 kHz mono 16-bit PCM."""
    gap = b"\x00\x00" * int(SAMPLE_RATE * 0.25)
    pcm = bytearray()
    chunks = chunk_turns(turns, cfg["tts_chunk_chars"])
    for i, chunk in enumerate(chunks, 1):
        log(f"  Voicing part {i}/{len(chunks)}")
        body = {
            "model": cfg["tts_model"],
            "input": [{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": t["text"],
                    "annotations": [{"type": "speech_metadata", "speaker": t["speaker"]}],
                } for t in chunk],
            }],
            "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": SAMPLE_RATE},
            "generation_config": {"speech_config": {
                "speakers": [{"speaker": cfg["host_a"], "voice": cfg["voice_a"]},
                             {"speaker": cfg["host_b"], "voice": cfg["voice_b"]}],
            }},
        }
        pcm += to_pcm(extract_audio(gemini(cfg, body)))
        pcm += gap
    return bytes(pcm)


def pcm_to_mp3(pcm: bytes, dest: pathlib.Path, title: str, show: str) -> float:
    with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
        f.write(pcm)
        raw = f.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", "1", "-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-codec:a", "libmp3lame", "-b:a", "64k", "-ar", "44100",
             "-metadata", f"title={title}", "-metadata", f"album={show}", str(dest)],
            check=True)
    finally:
        os.unlink(raw)
    return len(pcm) / (SAMPLE_RATE * 2)


# --------------------------------------------------------------------------
# 4. Publishing
# --------------------------------------------------------------------------

def slug(text: str, n: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "episode"


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True)


def publish_audio(cfg: dict, mp3: pathlib.Path, tag: str, title: str, notes: str) -> str:
    if cfg["dry_run"] or not cfg["repo"]:
        return f"{cfg['base_url'] or 'file://' + str(OUT_DIR) + '/'}{mp3.name}"
    gh("release", "create", tag, str(mp3), "--repo", cfg["repo"],
       "--title", title[:120], "--notes", notes[:5000], "--latest=false")
    return f"https://github.com/{cfg['repo']}/releases/download/{tag}/{mp3.name}"


def prune(cfg: dict, episodes: list[dict]) -> list[dict]:
    if len(episodes) <= cfg["keep"]:
        return episodes
    keep, drop = episodes[:cfg["keep"]], episodes[cfg["keep"]:]
    for ep in drop:
        if not cfg["dry_run"] and cfg["repo"] and ep.get("tag"):
            try:
                gh("release", "delete", ep["tag"], "--repo", cfg["repo"], "--yes", "--cleanup-tag")
                log(f"Removed old episode {ep['tag']}")
            except subprocess.CalledProcessError as exc:
                log(f"Could not remove {ep['tag']}: {exc.stderr.strip()}")
    return keep


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_feed(cfg: dict, episodes: list[dict]) -> str:
    base = cfg["base_url"]
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    items = []
    for ep in episodes:
        desc = (f"{ep['summary']}\n\nOriginal report: {ep['source_url']}\n\n"
                "AI-generated audio summary for private listening. Not produced or endorsed by GI-TOC.")
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <description>{escape(desc)}</description>
      <itunes:summary>{escape(desc)}</itunes:summary>
      <link>{escape(ep['source_url'])}</link>
      <guid isPermaLink="false">{escape(ep['guid'])}</guid>
      <pubDate>{ep['pub_date']}</pubDate>
      <enclosure url="{escape(ep['audio_url'])}" length="{ep['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{ep['duration']}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
    </item>""")
    show_desc = ("Automatic two-host audio briefings on new reports from the Global Initiative "
                 "Against Transnational Organized Crime. Unofficial, AI-generated, private feed.")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(cfg['show_title'])}</title>
    <link>{escape(base)}</link>
    <atom:link href="{escape(base)}feed.xml" rel="self" type="application/rss+xml"/>
    <description>{escape(show_desc)}</description>
    <language>en</language>
    <lastBuildDate>{now}</lastBuildDate>
    <itunes:author>{escape(cfg['show_author'])}</itunes:author>
    <itunes:summary>{escape(show_desc)}</itunes:summary>
    <itunes:image href="{escape(base)}cover.png"/>
    <itunes:category text="News"><itunes:category text="Politics"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""


def build_index(cfg: dict, episodes: list[dict]) -> str:
    rows = "\n".join(
        f'<li><a href="{html.escape(e["audio_url"])}">{html.escape(e["title"])}</a>'
        f' <small>{html.escape(e["pub_date"][:16])} · {e["duration"]}</small></li>' for e in episodes[:50])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{html.escape(cfg['show_title'])}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;color:#222}}
code{{background:#f2f2f2;padding:2px 6px;border-radius:4px;word-break:break-all}}li{{margin:.5em 0}}</style></head>
<body><h1>{html.escape(cfg['show_title'])}</h1>
<p>Subscribe in your podcast app with this URL:<br><code>{html.escape(cfg['base_url'])}feed.xml</code></p>
<h2>Latest episodes</h2><ul>{rows or '<li>No episodes yet.</li>'}</ul></body></html>
"""


def ensure_cover(cfg: dict) -> None:
    cover = DOCS_DIR / "cover.png"
    if cover.exists():
        return
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 1400), (18, 24, 38))
    d = ImageDraw.Draw(img)
    for r in range(900, 100, -100):  # concentric rings: a simple "network" motif
        d.ellipse([700 - r // 2, 520 - r // 2, 700 + r // 2, 520 + r // 2], outline=(196, 74, 54), width=6)
    d.ellipse([660, 480, 740, 560], fill=(196, 74, 54))

    def font(size):
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                  "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        return ImageFont.load_default(size=size)

    words, lines, line = cfg["show_title"].split(), [], ""
    for w in words:
        if len(line + " " + w) > 18 and line:
            lines.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    lines.append(line)
    y = 1040
    for ln in lines[:3]:
        f = font(92)
        width = d.textlength(ln, font=f)
        d.text(((1400 - width) / 2, y), ln, font=f, fill=(245, 240, 232))
        y += 110
    img.save(cover)


def write_site(cfg: dict, episodes: list[dict]) -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    ensure_cover(cfg)
    (DOCS_DIR / "feed.xml").write_text(build_feed(cfg, episodes), encoding="utf-8")
    (DOCS_DIR / "index.html").write_text(build_index(cfg, episodes), encoding="utf-8")
    (DOCS_DIR / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def make_episode(cfg: dict, session: requests.Session, article: dict) -> dict | None:
    info = inspect_article(article["url"], session)
    title = info["title"] or article.get("title") or article["url"]
    log(f"Report: {title}")

    pdf_path = None
    with tempfile.TemporaryDirectory() as tmp:
        for pdf_url in info["pdfs"][:3]:
            try:
                pdf_path = download_pdf(pdf_url, session, pathlib.Path(tmp) / "report.pdf")
            except requests.RequestException as exc:
                log(f"  PDF download failed: {exc}")
            if pdf_path:
                log(f"  PDF: {pdf_url}")
                break
        if not pdf_path:
            if cfg["reports_only"]:
                log("  No PDF on this page (an article, not a report); skipping")
                return None
            if len(info["text"]) < 2000:
                log("  Page text too short to make an episode; skipping")
                return None

        cache = STATE_DIR / "pending" / (hashlib.sha1(article["url"].encode()).hexdigest()[:12] + ".json")
        script = load_json(cache, None)
        if script:
            log("  Reusing the script saved on a previous attempt")
        else:
            script = write_script(cfg, {**info, "title": title}, pdf_path)
            save_json(cache, script)

    ep_title = script["title"] or title
    log(f"  Script: {len(script['turns'])} turns, ~{sum(len(t['text'].split()) for t in script['turns'])} words")
    pcm = speak(cfg, script["turns"])

    today = dt.datetime.now(dt.timezone.utc)
    digest = hashlib.sha1(article["url"].encode()).hexdigest()[:6]
    tag = f"ep-{today:%Y%m%d}-{digest}"
    OUT_DIR.mkdir(exist_ok=True)
    mp3 = OUT_DIR / f"{today:%Y-%m-%d}-{slug(title)}.mp3"
    seconds = pcm_to_mp3(pcm, mp3, ep_title, cfg["show_title"])
    log(f"  Audio: {fmt_duration(seconds)}, {mp3.stat().st_size / 1_048_576:.1f} MB")

    (STATE_DIR / "scripts").mkdir(parents=True, exist_ok=True)
    save_json(STATE_DIR / "scripts" / f"{mp3.stem}.json", {"source": article["url"], **script})
    cache.unlink(missing_ok=True)

    audio_url = publish_audio(cfg, mp3, tag, ep_title, f"{script['summary']}\n\nSource: {article['url']}")
    return {
        "guid": article["url"],
        "tag": tag,
        "title": ep_title,
        "report_title": title,
        "summary": script["summary"] or info["description"],
        "source_url": article["url"],
        "audio_url": audio_url,
        "bytes": mp3.stat().st_size,
        "duration": fmt_duration(seconds),
        "pub_date": email.utils.format_datetime(today),
    }


def main() -> int:
    cfg = load_config()
    if not cfg["api_key"]:
        log("GEMINI_API_KEY is not set. Add it as a repository secret (see README).")
        return 1

    seen_state = load_json(SEEN_FILE, None)
    first_run = seen_state is None
    seen_state = seen_state or {"seen": [], "failures": {}}
    seen = set(seen_state["seen"])
    episodes = load_json(EPISODES_FILE, [])

    session = http()
    candidates = discover(cfg, session)
    if not candidates:
        log("Could not reach GI-TOC's site or find any reports this run.")
        write_site(cfg, episodes)
        return 0 if episodes else 1

    new = [c for c in candidates if c["url"] not in seen]
    if first_run:
        # Don't turn the whole back catalogue into episodes on day one.
        for c in new[cfg["backfill"] * 4:]:
            seen.add(c["url"])
        new = new[:cfg["backfill"] * 4]
        log(f"First run: considering the {len(new)} most recent items, marking the rest as seen")

    made = 0
    run_eps: list[dict] = []  # newest first, like the source feed
    previous = episodes
    target = cfg["backfill"] if first_run else cfg["max_new"]
    for article in new:
        if made >= target:
            break
        try:
            ep = make_episode(cfg, session, article)
            seen.add(article["url"])
            seen_state["failures"].pop(article["url"], None)
            if ep:
                run_eps.append(ep)
                episodes = run_eps + previous
                made += 1
                log(f"  Published: {ep['title']}")
        except QuotaExhausted as exc:
            # Not the report's fault: leave it queued and stop until tomorrow.
            log(f"  Gemini's free daily allowance is used up ({str(exc)[:160]}).")
            log("  Stopping here; remaining reports will be done on the next run.")
            break
        except Exception as exc:  # noqa: BLE001
            n = seen_state["failures"].get(article["url"], 0) + 1
            seen_state["failures"][article["url"]] = n
            log(f"  Failed (attempt {n}/{cfg['max_attempts']}): {exc}")
            if n >= cfg["max_attempts"]:
                seen.add(article["url"])
                log("  Giving up on this report")
        # Save progress after every item so a crash never repeats work.
        seen_state["seen"] = sorted(seen)
        save_json(SEEN_FILE, seen_state)
        save_json(EPISODES_FILE, episodes)

    episodes = prune(cfg, episodes)
    save_json(EPISODES_FILE, episodes)
    seen_state["seen"] = sorted(seen)
    save_json(SEEN_FILE, seen_state)
    write_site(cfg, episodes)
    log(f"Done: {made} new episode(s); feed has {len(episodes)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
