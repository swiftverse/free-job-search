# Free Job Search

A small web app that searches public, no-paywall job boards and lets you download a PDF of the matches. There are no accounts, no paid APIs required, and no AI or LLM calls at runtime.

Someone enters a job title, an experience level, and optionally a location or a remote-only filter. The app looks up live listings, ranks them, and can export the snapshot as a PDF with clickable apply links.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt && python app.py
```

Then open [http://localhost:7860](http://localhost:7860). The server reads `PORT` from the environment and defaults to `7860`.

To run the same way a host would:

```bash
gunicorn app:app --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 4 --timeout 120
```

## What it searches

These sources are queried in parallel. If one is down, the others still return results.

| Source | Access | Notes |
| --- | --- | --- |
| [Remotive](https://remotive.com/api/remote-jobs) | Free, no key | Remote roles; the `search` parameter is passed through |
| [Arbeitnow](https://www.arbeitnow.com/api/job-board-api) | Free, no key | Mostly Europe, with a remote flag |
| [RemoteOK](https://remoteok.com/api) | Free, no key | Remote roles; a User-Agent is required |
| [Jobicy](https://jobicy.com/api/v2/remote-jobs) | Free, no key | Remote roles; filtered locally after a `tag` hint |
| [Himalayas](https://himalayas.app/jobs/api) | Free, no key | Latest remote roles (the public feed does not reliably filter by search) |
| [Working Nomads](https://www.workingnomads.com/api/exposed_jobs/) | Free, no key | Remote roles with short, useful tags |
| [The Muse](https://www.themuse.com/api/public/jobs) | Free, no key | Used when the public API responds |
| [Adzuna](https://developer.adzuna.com/) | Optional | Only if `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` are set. You may also set `ADZUNA_COUNTRY` (default `us`). |

LinkedIn, Indeed and Naukri are not searched. They do not offer a free public API.

## Deploy free in 5 minutes

You can put this on a free host so other people only need a link. No paid plan is required for a first version. Free tiers usually sleep after a period of inactivity, so the first visit after a pause can take a minute to wake up.

### Render (connect a GitHub repo)

1. Create a GitHub repository and push this project to it (the repo root should contain `app.py`, `requirements.txt` and `render.yaml`).
2. Sign in at [https://render.com](https://render.com).
3. Click **New +**, choose **Web Service**, and connect the GitHub repository.
4. Render should pick up `render.yaml`. If you configure the service by hand, use:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   - **Instance type:** Free
5. Click **Deploy**. When the deploy finishes, open the `*.onrender.com` URL and share that link.

Optional: in the Render dashboard, add `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` if you want Adzuna results. Leave them unset otherwise.

### Hugging Face Spaces (Docker)

1. Open [https://huggingface.co/new-space](https://huggingface.co/new-space) and sign in.
2. Give the Space a name. Set **Space SDK** to **Docker** and hardware to **CPU basic** (free).
3. Create the Space, then push this project to it (or connect the GitHub repository from the Space settings). The `Dockerfile` in this repo is what Spaces will build.
4. The container listens on port `7860` (or whatever `PORT` is set to). Wait for the build to finish, then open the Space URL and share that link.

## Limitations

- The results are a snapshot of whatever the free public APIs return, not every job on the internet.
- Coverage is strongest for remote and European listings. Arbeitnow is Germany-heavy.
- Remotive's public feed can be small at times.
- The Muse public API has been unavailable at times; the app simply skips it when that happens.
- Duplicate postings are removed by title and company, but the same role can still appear on more than one board with slightly different titles.

## License

MIT. See [LICENSE](LICENSE).
