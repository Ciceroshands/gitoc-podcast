"""Offline end-to-end test: fakes GI-TOC's site and the Gemini API.

Run:  python tests/test_offline.py
"""
import base64
import json
import math
import os
import pathlib
import shutil
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import podcast  # noqa: E402

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>GI</title>
<item><title>Report three</title><link>https://globalinitiative.net/analysis/report-three/</link></item>
<item><title>An article</title><link>https://globalinitiative.net/analysis/an-article/</link></item>
<item><title>Report two</title><link>https://globalinitiative.net/analysis/report-two/</link></item>
<item><title>Report one</title><link>https://globalinitiative.net/analysis/report-one</link></item>
</channel></rss>"""


def page(title, pdf=None):
    links = ""
    if pdf:
        links = (f'<a href="/wp-content/uploads/2026/10/{pdf}-ES.pdf">Español</a>'
                 f'<a href="/wp-content/uploads/2026/10/{pdf}.pdf">Download</a>')
    return (f'<html><head><meta property="og:title" content="{title} - Global Initiative">'
            f'<meta property="og:description" content="About {title}."></head>'
            f'<body><nav>menu</nav><article><h1>{title}</h1><p>{"Body text. " * 50}</p>{links}</article></body></html>')


class Resp:
    def __init__(self, status=200, content=b"", text=None, js=None):
        self.status_code, self.content = status, content
        self.text = text if text is not None else content.decode("utf-8", "ignore")
        self._js = js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise podcast.requests.HTTPError(str(self.status_code))

    def json(self):
        return self._js


PAGES = {
    "https://globalinitiative.net/analysis/report-three/": page("Report three", "GITOC-Report-Three"),
    "https://globalinitiative.net/analysis/an-article/": page("An article"),
    "https://globalinitiative.net/analysis/report-two/": page("Report two", "GITOC-Report-Two"),
    "https://globalinitiative.net/analysis/report-one/": page("Report one", "GITOC-Report-One"),
}
fetched_pdfs = []


class FakeSession:
    headers = {}

    def get(self, url, timeout=None):
        if url.endswith("/analysis/feed/"):
            return Resp(content=FEED)
        if url.endswith("/feed/"):
            return Resp(status=404)
        if url.endswith(".pdf"):
            fetched_pdfs.append(url)
            return Resp(content=b"%PDF-1.7 fake report")
        if url in PAGES:
            return Resp(content=PAGES[url].encode())
        return Resp(status=404)


def tone(seconds, freq):
    n = int(podcast.SAMPLE_RATE * seconds)
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / podcast.SAMPLE_RATE))) for i in range(n))


tts_calls = []


def fake_post(url, json=None, timeout=None, headers=None):
    assert url == podcast.GEMINI_URL and headers["x-goog-api-key"] == "test-key"
    if json["model"].endswith("-tts"):
        tts_calls.append(json)
        speakers = {s["speaker"] for s in json["generation_config"]["speech_config"]["speakers"]}
        for item in json["input"][0]["content"]:
            assert item["annotations"][0]["speaker"] in speakers
        audio = base64.b64encode(tone(0.5 * len(json["input"][0]["content"]), 220)).decode()
        return Resp(js={"steps": [{"type": "model_output", "content": [{"type": "audio", "data": audio}]}]})
    assert json["input"][0]["type"] == "document"
    script = {"title": "What the report found",
              "summary": "Two hosts unpack the report.",
              "turns": [{"speaker": "Maya" if i % 2 == 0 else "daniel",
                         "text": f"Turn {i} " + "words " * 60 + ("<laugh>" if i == 3 else "<applause>")}
                        for i in range(30)]}
    text = "```json\n" + __import__("json").dumps(script) + "\n```"
    return Resp(js={"steps": [{"type": "thought", "content": [{"type": "text", "text": "thinking"}]},
                              {"type": "model_output", "content": [{"type": "text", "text": text}]}]})


def run():
    work = pathlib.Path(tempfile.mkdtemp())
    podcast.STATE_DIR = work / "state"
    podcast.DOCS_DIR = work / "docs"
    podcast.OUT_DIR = work / "out"
    podcast.SEEN_FILE = podcast.STATE_DIR / "seen.json"
    podcast.EPISODES_FILE = podcast.STATE_DIR / "episodes.json"
    podcast.http = lambda: FakeSession()
    podcast.requests.post = fake_post
    published = []
    podcast.gh = lambda *a: published.append(a)
    os.environ.update({"GEMINI_API_KEY": "test-key", "GITHUB_REPOSITORY": "Someone/gitoc-podcast",
                       "FIRST_RUN_BACKFILL": "2", "MAX_NEW_PER_RUN": "3"})

    # Pure-function checks
    s = podcast.parse_script('```json\n{"title":"t","summary":"s","turns":[' +
                             ",".join('{"speaker":"X","text":"hi <cough> there"}' for _ in range(5)) + "]}```",
                             "Maya", "Daniel")
    assert [t["speaker"] for t in s["turns"]][:3] == ["Maya", "Daniel", "Maya"]
    assert "<cough>" not in s["turns"][0]["text"]
    chunks = podcast.chunk_turns([{"text": "x" * 400}] * 10)
    assert all(sum(len(t["text"]) for t in c) <= 1500 for c in chunks) and sum(map(len, chunks)) == 10

    # First run: newest two reports only; the article (no PDF) is skipped.
    assert podcast.main() == 0
    eps = json.loads(podcast.EPISODES_FILE.read_text())
    assert [e["report_title"] for e in eps] == ["Report three", "Report two"], eps
    assert all("-ES" not in u for u in fetched_pdfs), fetched_pdfs
    assert eps[0]["audio_url"].startswith("https://github.com/Someone/gitoc-podcast/releases/download/ep-")
    assert len(published) == 2 and published[0][0] == "release"
    seen = json.loads(podcast.SEEN_FILE.read_text())["seen"]
    assert "https://globalinitiative.net/analysis/an-article/" in seen

    # Feed is valid RSS with the right enclosures and a GitHub Pages self link.
    root = ET.parse(podcast.DOCS_DIR / "feed.xml").getroot()
    items = root.findall("./channel/item")
    assert len(items) == 2
    enc = items[0].find("enclosure")
    assert enc.get("type") == "audio/mpeg" and int(enc.get("length")) > 0
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    assert root.find("./channel/atom:link", ns).get("href") == "https://someone.github.io/gitoc-podcast/feed.xml"
    assert (podcast.DOCS_DIR / "cover.png").exists()

    # Audio: real MP3, duration ~ sum of fake speech + gaps.
    mp3 = next(podcast.OUT_DIR.glob("*.mp3"))
    probe = podcast.subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(mp3)], capture_output=True, text=True)
    assert float(probe.stdout) > 10, probe.stdout

    # Second run: only Report one remains; nothing is repeated.
    published.clear()
    assert podcast.main() == 0
    eps = json.loads(podcast.EPISODES_FILE.read_text())
    assert [e["report_title"] for e in eps] == ["Report one", "Report three", "Report two"]
    assert len(published) == 1

    # Third run: nothing new.
    published.clear()
    assert podcast.main() == 0 and not published

    # Free-tier daily limit: the report stays queued and is not counted as a failure.
    global FEED
    FEED = FEED.replace(b"<item>", b"<item><title>Report four</title><link>https://globalinitiative.net/analysis/report-four/</link></item><item>", 1)
    PAGES["https://globalinitiative.net/analysis/report-four/"] = page("Report four", "GITOC-Report-Four")
    real_post = podcast.requests.post

    def quota_post(url, json=None, timeout=None, headers=None):
        if json["model"].endswith("-tts"):
            return Resp(status=429, content=b'{"error":{"message":"Quota exceeded for GenerateRequestsPerDayPerProject"}}')
        return real_post(url, json=json, timeout=timeout, headers=headers)

    podcast.requests.post = quota_post
    published.clear()
    assert podcast.main() == 0 and not published
    state = json.loads(podcast.SEEN_FILE.read_text())
    assert "https://globalinitiative.net/analysis/report-four/" not in state["seen"]
    assert not state["failures"], state["failures"]
    podcast.requests.post = real_post
    assert podcast.main() == 0 and len(published) == 1  # done the next night

    print(f"All checks passed. TTS requests: {len(tts_calls)}. Sample feed: {podcast.DOCS_DIR / 'feed.xml'}")
    return work


if __name__ == "__main__":
    work = run()
    if os.environ.get("KEEP_DIR"):
        shutil.copytree(work, os.environ["KEEP_DIR"], dirs_exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)
