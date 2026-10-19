# Log Studio Cloud – Fast Setup Tutorial

One-time setup. Free. Works on phone or computer.

---

## 1. Create the GitHub repo

1. Go to GitHub → **New repository**
2. Name it anything (example: `log-studio`)
3. Make it **Public**
4. Create the repo (do **not** add a README)

5. Upload these files to the **root** of the repo:
   - `index.html`
   - `cloud_pipeline.py`
   - folder `.github/workflows/` containing:
     - `log-studio.yml`
     - `log-studio-split.yml`
     - `log-studio-merge.yml`

6. Turn on GitHub Pages:
   - Repo → **Settings** → **Pages**
   - Source: **Deploy from a branch**
   - Branch: `main` / `/ (root)`
   - Save

Your page will be:  
`https://YOURNAME.github.io/log-studio/`

---

## 2. Google Cloud project (one time)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (any name)
3. Enable these two APIs:
   - **Google Drive API**
   - **YouTube Data API v3**

4. **OAuth consent screen**
   - User type: **External**
   - Fill in App name + your email
   - Scopes: add  
     `https://www.googleapis.com/auth/drive`  
     `https://www.googleapis.com/auth/youtube.upload`
   - Add **both** of your Google accounts as **Test users**
   - Click **Publish app** (important – otherwise tokens die after 7 days)

5. **Create OAuth client**
   - Credentials → Create credentials → **OAuth client ID**
   - Application type: **Web application**
   - Authorized JavaScript origins:  
     `https://YOURNAME.github.io`
   - Authorized redirect URIs:  
     `https://developers.google.com/oauthplayground`
   - Create → copy the **Client ID** and **Client Secret**

---

## 3. Get the two refresh tokens

Go to: https://developers.google.com/oauthplayground

1. Click the gear (top right) → tick **Use your own OAuth credentials**  
   → paste Client ID + Client Secret

2. **Drive token (Account A)**
   - Left side → select scope: `https://www.googleapis.com/auth/drive`
   - Click **Authorize APIs** → sign in with Account A
   - Click **Exchange authorization code for tokens**
   - Copy the **Refresh token**

3. **YouTube token (Account B)**
   - Clear the playground / open a private window
   - Same steps, but use scope: `https://www.googleapis.com/auth/youtube.upload`
   - Sign in with Account B (choose the brand channel if you have one)
   - Copy the **Refresh token**

---

## 4. Put secrets in GitHub

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these four secrets:

| Name                    | Value                          |
|-------------------------|--------------------------------|
| `GOOGLE_CLIENT_ID`      | your Client ID                 |
| `GOOGLE_CLIENT_SECRET`  | your Client Secret             |
| `GOOGLE_REFRESH_TOKEN`  | Drive refresh token (Account A)|
| `YT_REFRESH_TOKEN`      | YouTube refresh token (Account B) |

---

## 5. GitHub token for the web page

1. GitHub → profile picture → **Settings** → **Developer settings** → **Fine-grained tokens** → **Generate new token**
2. Token name: anything
3. Repository access: **Only select repositories** → choose your log-studio repo
4. Permissions → **Actions**: Read and write
5. Generate → **copy the token** (you only see it once)

---

## 6. First time opening the page

1. Open `https://YOURNAME.github.io/log-studio/`
2. Click the **gear** (top right)
3. Under **Connections** fill in:
   - Repository: `YOURNAME/log-studio`
   - Branch: `main`
   - Google Client ID
   - GitHub token (the fine-grained one)
4. Tap **Save connections** → **Done**

You’re ready.

---

## Quick usage

1. **Select files…** (or a zip of audio) → choose sort order → **Upload and analyse**
2. Wait until it says **Master ready**
3. Zoom / play / set start-end points → add segments
4. Hit an **Export** button (audio or video)
5. You can close the page. Links appear in the **Audio tree** at the bottom and in the GitHub Actions run summary.

---

## Optional: Second repo (doubles parallel jobs)

Only needed if you regularly render very long videos and hit the 20-job limit.
GitHub's 20-job limit is **per owner**, so the second repo must belong to a different owner:
create a free **organization** (GitHub → **+** → **New organization** → Free) and make the repo inside it.

1. In the organization, make a second **public** repo.
2. Put only `cloud_pipeline.py` + `.github/workflows/log-studio-worker.yml` in it.
3. Make two fine-grained tokens, each with **Contents: Read and write** and **Actions: Read and write**:
   - `MAIN_PAT`: Resource owner = you, only the main repo
   - `WORKER_PAT`: Resource owner = the organization, only the second repo
4. Second repo → secrets: the same three Google secrets + `MAIN_PAT`. Variable: `MAIN_REPO` = `YOURNAME/log-studio`.
5. Main repo → secret `WORKER_PAT`. Variable: `WORKER_REPO` = `YOURORG/second-repo`.

(See SETUP.md for the full details and the test.)

---

That’s the whole setup. After this you never touch Google Cloud or tokens again unless you regenerate them.
