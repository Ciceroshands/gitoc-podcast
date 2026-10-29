# GI-TOC report podcast

Every night this checks globalinitiative.net for new reports. For each one it writes a
two-host conversation from the PDF, voices it, and adds it to a private podcast feed.
Your podcast app downloads new episodes overnight.

All of it can run for free: GitHub hosts and schedules it, and Gemini's free tier reads the
reports and does the voices.

## One-time setup (about 15 minutes)

1. **Get a free Gemini API key.** Go to https://aistudio.google.com/apikey, sign in with a
   Google account, and create a key. No card is needed. Don't turn on billing if you want it
   to stay free: without billing, Google stops you at the free limit instead of charging.
   One trade-off: Google may use free-tier requests to improve its products. That matters
   little here, since the reports are already public.

2. **Create a GitHub repository.** Sign in at github.com, then go to **New repository**.
   Name it e.g. `gitoc-podcast` and set it to **Public**. GitHub Pages and public audio
   links need that, but the feed is still hidden from podcast directories and search
   engines. Upload every file from this folder, including the `.github` folder. Dragging
   the whole folder onto the "uploading an existing file" page works.

3. **Add your key.** In the repo, go to **Settings → Secrets and variables → Actions →
   New repository secret**. Name: `GEMINI_API_KEY`. Value: your key.

4. **Turn on the feed website.** Go to **Settings → Pages**. Set Source to **Deploy from a
   branch**, then choose branch `main` and folder `/docs`. Save.

5. **Run it once now.** Go to **Actions → Daily GI-TOC podcast → Run workflow**. The first
   run makes the two newest reports into episodes, which takes about 5–15 minutes. After
   that it runs every night by itself.

6. **Subscribe.** In your podcast app, add a show by URL:
   `https://YOUR-GITHUB-USERNAME.github.io/gitoc-podcast/feed.xml`
   - Apple Podcasts: Library → ⋯ → Follow a Show by URL
   - Pocket Casts / Overcast / AntennaPod: search box → paste the URL

   Turn on auto-download for the show, and new episodes will be waiting each morning.

## Staying within the free tier

Google sets daily free limits per model. You can see yours at
https://aistudio.google.com/rate-limit. Each episode uses 1 script request and about 3 voice
requests. If a night's allowance runs out, the job stops cleanly and the remaining reports
wait for the next night. Nothing is lost or skipped. If that happens often, lower
`MAX_NEW_PER_RUN` to 1 or switch `TTS_MODEL` to `gemini-3.8-flash-lite-tts`.

## Changing things

Edit the settings block in `.github/workflows/daily.yml`:

| Setting | What it does |
|---|---|
| `cron: "17 4 * * *"` | When it runs, in UTC. 04:17 UTC is early morning in Europe and late night in the Americas. |
| `EPISODE_MINUTES` | Target length (default 12) |
| `LISTENER_FOCUS` | Steers every episode, e.g. `"implications for Latin American policy"` |
| `MAX_NEW_PER_RUN` | Cap on episodes per night (default 2); the rest wait for the next night |
| `REPORTS_ONLY` | `true` = only pages with a PDF (research reports, policy briefs, risk bulletins). `false` also covers short articles. |
| `HOST_A_VOICE` / `HOST_B_VOICE` | Voices, e.g. Kore, Charon, Puck, Zephyr, Sulafat, Achird, Sadaltager, Gacrux |
| `EPISODE_LANGUAGE` | Episodes in another language, whatever the report's language |

To test without publishing, run the workflow with **"Test run"** ticked. The MP3s appear
under that run's **Artifacts**.

## How it works

- **New reports:** read from GI-TOC's site feed. If the feed isn't available, it falls back
  to their `/analysis/` page. A page counts as a report if it links a PDF; English versions
  are preferred over translations.
- **Script:** the PDF goes to `gemini-3.8-flash`, which writes the dialogue as JSON. It is
  told to use only facts from the report. Scripts are saved in `state/scripts/` so you can
  check them against the report.
- **Audio:** `gemini-3.8-flash-tts` with two voices, then ffmpeg converts it to a 64 kbps MP3.
- **Hosting:** each MP3 is a GitHub Release asset. The feed is `docs/feed.xml` on GitHub
  Pages. The oldest episodes are removed after 150.
- **Memory:** `state/seen.json` records what has been done, so nothing is repeated. A
  report that fails is retried on the next three nights, then skipped.

## If something goes wrong

Open **Actions** and click the latest run to see the log.

- *"GEMINI_API_KEY is not set"*: step 3 is missing.
- *"Could not reach GI-TOC's site"*: their site blocked or rate-limited GitHub's servers that night. It tries again the next night.
- *HTTP 400 mentioning the model*: Google renamed a model. Update `SCRIPT_MODEL` / `TTS_MODEL`.
- No runs for a while: GitHub pauses scheduled jobs in repos with no activity for 60 days. New episodes count as activity. If it pauses, click **Enable workflow** in the Actions tab.

## Fair use of GI-TOC's work

The episodes are AI summaries of GI-TOC's reports for your own listening. Keep the feed URL
to yourself and don't list it publicly. Every episode description links to the original report.

## Offline test

```
pip install -r requirements.txt
python tests/test_offline.py
```

This fakes GI-TOC's site and the Gemini API, and checks discovery, script parsing, audio
assembly, the RSS feed, and that nothing is ever made twice.
