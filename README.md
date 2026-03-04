# llms.txt Generator

Generate production-ready `llms.txt` files from any website — upload a CSV or sitemap and get a structured AI-readable index instantly.

Two versions available:
- **Cloud** (`app.py`) — GPT-4o-mini via OpenAI, deployable to Railway/Render
- **Local** (`app_local.py`) — Mistral via Ollama, runs entirely on your machine

---

## Run Locally (Ollama/Mistral — Zero Cost)

### Prerequisites
- Python 3.9+
- [Ollama](https://ollama.com) installed

### Setup

```bash
# Pull the Mistral model
ollama pull mistral

# Clone and set up
git clone https://github.com/arunjohn-git/llms-txt-generator
cd llms-txt-generator
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-local.txt

# Start the app (caffeinate prevents Mac from sleeping on long runs)
caffeinate -i python3 app_local.py
```

Open [http://localhost:5000](http://localhost:5000)

### Troubleshooting

| Problem | Fix |
|---|---|
| `python: command not found` | Use `python3` instead |
| `venv broken / No such file` | `rm -rf venv` then recreate with `python3 -m venv venv` |
| Port 5000 conflict (AirPlay) | Change port to `5001` in last line of `app_local.py` |
| Mac sleeps mid-run | Always use `caffeinate -i python3 app_local.py` |
| Ollama model not found | Run `ollama list` — if missing, run `ollama pull mistral` |

---

## Deploy to Railway (Cloud/GPT-4o-mini)

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   gh repo create llms-txt-generator --public --push
   ```

2. **Deploy on Railway**
   - Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
   - Select your repo
   - Railway auto-detects Python and deploys via `Procfile`

3. **Set environment variables** in Railway dashboard → Variables:

   | Variable | Value | Required |
   |---|---|---|
   | `OPENAI_API_KEY` | `sk-...` | Optional — if set, users don't need to enter it |
   | `APP_PASSWORD` | any password | Optional — locks the app behind a password |
   | `SECRET_KEY` | random string | Recommended — secures Flask sessions |

4. **Done** — Railway gives you a public URL

> ⚠️ Keep `--workers 1` in the Procfile. Multi-worker deployments break in-memory job storage.

---

## Deploy to Render

1. Push to GitHub (same as above)
2. Go to [render.com](https://render.com) → New Web Service → Connect repo
3. Set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:$PORT`
4. Add environment variables under "Environment"

---

## Environment Variables (Cloud)

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | If set, users don't need to enter a key. If not set, UI shows key input. |
| `APP_PASSWORD` | — | If set, app requires a password to access. Leave unset for open access. |
| `SECRET_KEY` | `llms-txt-secret-2024` | Flask session secret. Set to a random string in production. |
| `PORT` | `5000` | Set automatically by Railway/Render. |

---

## Cost Estimate (GPT-4o-mini)

| Pages | Approx cost |
|---|---|
| 20 pages | ~$0.01 |
| 100 pages | ~$0.05 |
| 500 pages | ~$0.25 |
| 1000 pages | ~$0.50 |
