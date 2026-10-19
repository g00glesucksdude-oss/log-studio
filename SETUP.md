# ADPCM Log Studio in the cloud: setup (one time, free, doable on a phone)

## How it works
1. **Web page** (`index.html` on github.io) looks like your desktop app. You select audio files, it reads their
   times, uploads them to **Google Drive (account A)** with those times kept, and starts an **ingest** job.
   Everything lives in one Drive folder that the app creates for you: **Logger workspace**
   (`uploads/` for jobs, `outputs/audio`, `outputs/video`, `index/` for the Audio tree, and `_index.json`, a combined copy of the tree for other programs).
2. The **ingest job** (GitHub Actions) merges the files like the desktop app, then saves a waveform file and a
   small playable copy back to Drive. A few minutes later the page shows the waveform.
3. In the page you zoom, play, set start/end points and build the segment queue.
4. **Export** starts a second cloud job. It renders `.ogg` or `.mp4` parts, uploads videos to **YouTube
   (account B, unlisted)**, then uploads to your **Drive output folder (public)**. You can close the page.
- GitHub is separate from both Google accounts. Drive and YouTube can be different accounts.

## 1. Repo and GitHub Pages
1. Make a **public** repo (free plans only serve Pages from public repos).
2. Add `index.html` and `cloud_pipeline.py` at the root, and the three workflows `log-studio.yml`, `log-studio-split.yml` and
   `log-studio-merge.yml` in `.github/workflows/`. (`log-studio-worker.yml` is only for the optional second repo, see "Second repo"; `log-studio-gemini.yml` is only for the optional Gemini chat.)
3. Settings → Pages → Deploy from branch → `main` / root. Your page: `https://<user>.github.io/<repo>/`.
4. Public repo means run logs are public. They show file names and the links. Secrets stay hidden.

## 2. Google Cloud project (free)
1. Enable **YouTube Data API v3** and **Google Drive API**.
2. OAuth consent screen: External. Add **both** Google accounts as test users. Scopes: `youtube.upload` and `drive`.
   Then **Publish app**, or tokens die after 7 days.
3. Create an OAuth client, type **Web application**:
   - Authorized JavaScript origin: `https://<user>.github.io`
   - Authorized redirect URI: `https://developers.google.com/oauthplayground`
4. Keep the client ID and secret. One client serves both accounts.

## 3. Two refresh tokens (OAuth Playground)
Open https://developers.google.com/oauthplayground → gear → "Use your own OAuth credentials".
- **Drive token, signed in as account A:** scope `https://www.googleapis.com/auth/drive` → Exchange code → copy refresh token.
- **YouTube token, signed in as account B:** scope `https://www.googleapis.com/auth/youtube.upload` → same steps.
  For a brand channel, choose it on the channel picker.

## 4. GitHub secrets
Settings → Secrets and variables → Actions
- Secrets: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (Drive, account A), `YT_REFRESH_TOKEN` (YouTube, account B)
- No variable is needed. The output folders are created inside **Logger workspace** automatically.

## 5. Token for the page
GitHub → Settings → Developer settings → Fine-grained tokens. Repository: only this repo. Permission: **Actions: Read and write**.

## 6. First open
Open the page. The gear (top right) opens Settings. Under Connections enter `owner/repo` (for example `YOURNAME/log-studio`),
branch `main`, your Google client ID and the GitHub token, then tap **Save connections** and **Done**.

## Using it
1. **Select files…** (audio, or a **.zip** of audio) and pick the sort. Check the list. **Upload and analyse**, choose account A, and keep the screen on until the upload finishes.
2. Wait for "Master ready". If you closed the page, reopen it and use the **Audio tree** and **Open**.
   - **Open** on a job that was never analysed starts the analysis for you and loads it when it is done.
   - **Add files…** (next to Audio tree) adds more audio, or a .zip, to the job that is open and analyses it again.
     The menu next to it chooses *added after the existing files* (default) or *sorted in with the existing files*
     (uses the "Sort by" setting and renumbers the existing files; the audio itself is not touched).
     A .zip is unpacked in the cloud: only audio entries are used, in name order, and they take the place of the zip in the order.
3. Edit like the desktop app, then use an Export button. Links appear in the run's Summary on GitHub and in the **Audio tree**.

## Gear menu (Settings)
Prefixes, custom video title, resolution, Vorbis quality, part lengths, video look and background image, plus the upload targets:
- Videos go to **YouTube (unlisted)** by default. Optionally also save a **public copy in Drive** (`outputs/video`).
- Audio goes to Drive `outputs/audio` with a public link.
- **Add the upload date and time** to titles and descriptions (YouTube title, Drive file name and description).
- Move the uploaded audio to the Drive trash after an export.
- **Visualizer frame rate**: Normal (25 fps) or Lite (12 fps, about 2x faster).

## Long exports (automatic, nothing to switch on)
There is no parallel option any more. The page decides by itself, using the time each mode needs:

`V_max = 360 min x 0.90 / (R x E)`, also capped by what fits on the runner's disk.
R (job minutes per video minute) was measured on your run: **Lite 0.6545, Normal 1.309** (x2.1 at 1080p, x0.5 at 480p).
So one job renders at most about **495 min (Lite)** or **248 min (Normal)** of video. Still-image and audio renders are much faster; they are limited by disk (about 31 h of 44.1 kHz stereo).

- A part (the 600 min video parts, as before) that fits comfortably is rendered by one normal job, exactly like before.
- A longer part is cut into `N = ceil(part / V_max)` equal chunks (cuts fall on whole seconds). Each chunk is rendered by its own job
  (a silent video plus a lossless FLAC for the sound) and put in `Logger workspace/stage/`. The last chunk to finish starts the **merge** job.
  The merge joins the video **by stream copy** (no quality loss) and joins the sound sample-exactly before encoding it once, so there are no gaps at the joins.
  Then it uploads to YouTube / Drive as before and deletes the stage folder.
- If you chose several 600 min parts, each part is its own run, so a 40 h log is 4 runs, each split into chunks.
- A failed chunk does not lose the others. **Retry missing** in the Audio tree re-runs the part; chunks that already finished are skipped.
- A merge that fails (for example a YouTube error) can be re-run from the Actions page ("Re-run failed jobs"). The chunks stay in `stage/` until it succeeds.
- Every cloud job starts with a **Maximize disk space** step (removes .NET, Android SDK, Haskell, CodeQL and Docker images) and works on the runner's large second disk.
- Chunk files wait in your Drive while a merge is pending (about 1 to 3 GB per chunk). Free Drive space needs room for them.
- The playable copy and the waveform are built while the audio is decoded, one file at a time. A log of any length is never stored whole on the runner.
- The only question the page may still ask is the YouTube daily-quota warning (more than about 6 parts a day).

### Second repo (optional, doubles the 20 job limit)
GitHub's limit of 20 jobs at once applies **per account or organization**, not per repo. A second repo under the same login shares the same 20 slots,
so Repo B must belong to a different owner. As far as I know GitHub allows one free personal account per person, so the safe way is a free **organization** that you own.
Repo A gets the even chunks, Repo B the odd ones. Without Repo B everything runs in Repo A (up to 20 chunks at a time).

1. GitHub → + → New organization → Free plan. Name it anything.
2. In the organization, create **Repo B**: public (same runner size as Repo A). Add `cloud_pipeline.py` at the root and `.github/workflows/log-studio-worker.yml`.
3. Two fine-grained tokens (Settings → Developer settings → Fine-grained tokens), each with **Contents: Read and write** and **Actions: Read and write**:
   `MAIN_PAT`: Resource owner = your user, only Repo A. `WORKER_PAT`: Resource owner = the organization, only Repo B.
4. Repo B → Settings → Secrets and variables → Actions: secrets `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` (same Drive values as Repo A) and `MAIN_PAT`. Variable `MAIN_REPO` = `YOURNAME/repo-a`.
5. Repo A → same place: secret `WORKER_PAT`, variable `WORKER_REPO` = `YOURORG/repo-b`.
   Optional variables `E_REPO_A` and `E_REPO_B` (speed factors, default 1; 1.5 = runners 50% slower). Chunks are sized for the slower one.
6. Test: set `E_REPO_A` to 4 temporarily (this shrinks the chunk size), export about 3 hours in Normal mode, and check that Repo B's Actions tab shows a "worker" run. Delete `E_REPO_A` afterwards.
7. If Repo B cannot be reached, Repo A renders those chunks itself.

## Gemini chat (optional)
A chat panel under the Audio tree that talks to Gemini about your **audio exports**. Your phone never uploads the audio; a cloud job does.

1. Get an API key at Google AI Studio (aistudio.google.com).
2. Repo secret: `GEMINI_API_KEY` = that key (the cloud job uploads the files with it).
3. Add `.github/workflows/log-studio-gemini.yml` to the main repo (it uses the same Google Drive secrets as the other workflows).
4. In the page: Settings (gear) > **Gemini chat**: paste the same key (saved in this browser only), check the model name, and write your
   **standing instructions** (sent invisibly with every message) and an optional **prefix** that is put in front of every message you send.
5. Gemini chat > **Attach audio…**: tick exports, then **Attach selected**. A cloud job (about a minute) uploads them to Gemini.
   Then chat as long as you like: each message is only text, so it uses almost no mobile data.
6. **New chat** clears the conversation and the attached audio.

### Asking Gemini to do things (exports, cuts, visualizers)
You don't have to attach any audio or open a project. Just ask, for example "cut 10:00 to 20:00 of the Tuesday job as audio" or
"make a lite 720p visualizer of the whole second job". Gemini has these tools: list your jobs and exports, read a job's file list and length,
start an audio export or a visualizer video (with any cuts), convert a time in an exported file to the matching time in the original log, and check
the cloud runs. It uses the same automatic splitting as the normal buttons.

- **Data use:** the chat only reads small text files (the job's `_meta.json` and the Audio tree entries). It never downloads the waveform, the playable
  copy or the master, which is where the big data use of opening a project comes from.
- **Confirmation:** by default you get a prompt that says exactly what Gemini wants to start (a video that goes to YouTube is flagged). The Settings
  checkbox "Let Gemini start exports without asking me first" turns the prompt off. Leave it off if you attach audio you don't fully control,
  because spoken words in the audio could try to give Gemini instructions.
- **Listening and cutting:** if you attach exported audio, Gemini can listen and pick cut times. Time 0 of an attached file is the start of that export;
  `map_export_time` converts it to log time. This only works for exports made after this update (they now remember which stretches of the log they contain).
- Each export request is one cloud job set. Several separate files means several requests.

Notes: Gemini keeps uploaded files for 48 hours (attach again after that). Audio can total up to about 9.5 hours per conversation, but keep it to the
length that gives you reliable transcripts (you use 30 minute exports). Model names change over time, so if you get a "model not found" error, change the
Model field in Settings to a current one. The chat history is stored in this browser only. Your audio goes to Google, so keep that in mind for sensitive logs.

## Audio tree (bottom of the page)
Shows each job and the exports it produced, with Drive and YouTube links. Each job and export is a small file in `Logger workspace/index`, so jobs running in parallel never overwrite each other.
`Logger workspace/_index.json` is a combined copy for other programs; it is rebuilt after every run and whenever the page refreshes the tree. Buttons: **Open** a job, **Delete job**, **Delete** one export,
**Delete all audio exports / video exports**, and **Check Drive and clean up** (removes entries for things you deleted by hand in Drive).
Deleting moves files to the Drive trash and updates the tree. Empty the Drive trash to free space. A YouTube video must be deleted in YouTube Studio.

## Cleanup
The cloud job works in a temporary folder on GitHub's runner and deletes it at the end. Only the Drive files above are kept.

## Limits
- Browsers expose only last-modified time, not creation time. "Timestamp in name" and "File name" sorts are also offered.
- The waveform is built from 200 bins per second, so the 5 s zoom is a little coarser than the desktop app.
- Playback uses a small mono copy (about 11 MB per hour of audio). Logs longer than 20 hours are split into about 1-hour pieces; the page loads only the current hour (and the next) so memory stays low even for multi-day recordings.
- Keyboard shortcuts (Space, C) only work with a keyboard. On a phone use the buttons.
- There is no Cancel for cloud jobs; cancel a run from GitHub's Actions page.
- Jobs from an older version of the page (folder "Log Studio uploads") are moved into Logger workspace as `uploads` the first time you use the new page.
- YouTube: API projects that haven't passed the audit get videos forced to private. Videos over 15 minutes need a verified channel. Parts stay under 11 h.
- GitHub jobs stop at 6 h. Long parts are split automatically (see "Long exports"). The Lite visualizer or "Image / GIF only (fast)" make renders faster still.
- Memory (RAM) is not a limit here: audio streams through ffmpeg and frames are never kept, so there is no frame batch to tune. The limits that matter are time (handled by the chunk size) and runner disk (handled by decoding only what a chunk needs).
- A zip's audio is ordered by file name inside the zip. The "Sort by" setting only orders files you select in the page.

## If GitHub says "Not Found" (404)
It means GitHub can't see the repo, the token's access to it, or the workflow file. Check:
1. Repo is typed as `YOURNAME/log-studio` (no https, no spaces).
2. The file is at exactly `.github/workflows/log-studio.yml` on the `main` branch, and the repo's **Actions** tab lists "Log Studio".
3. The fine-grained token's repository access includes this repo, with **Actions: Read and write**.
