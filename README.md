# llms.txt Generator — Cloud

Generate production-ready `llms.txt` files from any website using GPT-4o-mini.

## Deploy to Railway (5 minutes)

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
   - Railway auto-detects Python and deploys

3. **Set environment variables** in Railway dashboard → Variables:

   | Variable | Value | Required |
   |---|---|---|
   | `OPENAI_API_KEY` | `sk-...` | Optional — if set, users don't need to enter it |
   | `APP_PASSWORD` | any password | Optional — locks the app behind a password |
   | `SECRET_KEY` | random string | Recommended — secures sessions |

4. **Done** — Railway gives you a public URL like `https://llms-txt-generator.up.railway.app`

---

## Deploy to Render (free tier)

1. Push to GitHub (same as above)
2. Go to [render.com](https://render.com) → New Web Service → Connect repo
3. Set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 2 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT`
4. Add environment variables under "Environment"

---

## Run locally

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
export APP_PASSWORD=yourpassword   # optional
python app.py
```

Open `http://localhost:5000`

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | If set, users don't need to enter a key. If not set, UI shows key input field. |
| `APP_PASSWORD` | — | If set, app requires a password to access. Leave unset for open access. |
| `SECRET_KEY` | `llms-txt-secret-2024` | Flask session secret. Set to a random string in production. |
| `PORT` | `5000` | Port to run on. Set automatically by Railway/Render. |

---

## Cost estimate (GPT-4o-mini)

| Pages | Approx cost |
|---|---|
| 20 pages | ~$0.01 |
| 100 pages | ~$0.05 |
| 500 pages | ~$0.25 |
| 1000 pages | ~$0.50 |
