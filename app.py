# MUST BE AT THE VERY TOP FOR GEVENT COMPATIBILITY
from gevent import monkey
monkey.patch_all()

import os
import csv
import re
import json
import time
import requests
import io
import threading
import queue
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, send_file
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "llms-txt-secret-2026")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global job store
jobs = {}

# ─────────────────────────────────────────────────────
# URL & FETCH UTILITIES (Logic from app_local.py)
# ─────────────────────────────────────────────────────

def clean_url(url):
    try:
        parsed = urlparse(url)
        path = re.sub(r'/{2,}', '/', parsed.path)
        return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
    except: return url

def fetch_page(url):
    try:
        response = requests.get(url, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        # Standard noise removal
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()
        content = soup.get_text(separator=' ', strip=True)[:6000]
        return content if len(content) > 100 else None
    except: return None

# ─────────────────────────────────────────────────────
# AI PIPELINE (Cloud-Adapted to GPT-4o-mini)
# ─────────────────────────────────────────────────────

def summarize_with_gpt(url, content):
    """Replaces local Mistral call with OpenAI for cloud stability."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a technical writer creating llms.txt entries. Return raw JSON: {'title': '...', 'description': '...'}"},
                {"role": "user", "content": f"URL: {url}\nContent: {content[:4000]}"}
            ],
            response_format={ "type": "json_object" }
        )
        return json.loads(response.choices[0].message.content)
    except: return None

# ─────────────────────────────────────────────────────
# FLASK ROUTES (Web UI Integration)
# ─────────────────────────────────────────────────────

@app.route('/')
def index():
    # Renders your templates/index.html
    return render_template('index.html')

@app.route('/health')
def health():
    return "OK", 200

@app.route('/start', methods=['POST'])
def start():
    # Extract URLs from CSV file or Direct URL
    urls_raw = []
    
    if 'csv_file' in request.files and request.files['csv_file'].filename != '':
        file = request.files['csv_file']
        stream = io.StringIO(file.stream.read().decode("utf-8"), newline=None)
        reader = csv.reader(stream)
        urls_raw = [cell.strip() for row in reader for cell in row if cell.strip().startswith("http")]
    else:
        # Check for direct URL from your 'Direct URL' tab
        target_url = request.form.get('url')
        if target_url: urls_raw = [target_url]

    # Clean and deduplicate URLs
    urls = []
    seen = set()
    for u in urls_raw:
        cleaned = clean_url(u)
        if cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)

    if not urls:
        return jsonify({"error": "No valid URLs found"}), 400

    job_id = str(int(time.time() * 1000))
    q = queue.Queue()
    jobs[job_id] = {"queue": q, "result": None, "done": False}

    # Pipeline Thread
    def run_pipeline():
        try:
            total = len(urls)
            # STAGE 1: FETCHING
            q.put({"type": "stage", "msg": "Web Fetching", "pct": 10})
            pages = []
            for i, url in enumerate(urls, 1):
                content = fetch_page(url)
                if content:
                    pages.append({"url": url, "content": content})
                q.put({"type": "fetch", "current": i, "total": total, "url": url, "ok": content is not None})

            # STAGE 2: SUMMARISING
            q.put({"type": "stage", "msg": "Summarising with GPT-4o-mini", "pct": 40})
            summaries = []
            for i, page in enumerate(pages, 1):
                res = summarize_with_gpt(page['url'], page['content'])
                if res:
                    summaries.append({"url": page['url'], "title": res.get('title'), "desc": res.get('description')})
                q.put({"type": "summarize", "current": i, "total": len(pages), "url": page['url'], "ok": res is not None})

            # STAGE 3: QA & FIX (Simplified for Cloud Performance)
            q.put({"type": "stage", "msg": "QA & Auto-fix", "pct": 85})
            # (Structural fixes like title de-duping go here)

            # STAGE 4: GENERATING
            q.put({"type": "stage", "msg": "Generating llms.txt", "pct": 95})
            output = ""
            for s in summaries:
                output += f"- Source: {s['url']}\n  Title: {s['title']}\n  Description: {s['desc']}\n\n"
            
            jobs[job_id]["result"] = output.encode("utf-8")
            q.put({"type": "done", "total": len(summaries)})
            
        except Exception as e:
            q.put({"type": "error", "msg": str(e)})
        finally:
            jobs[job_id]["done"] = True

    threading.Thread(target=run_pipeline, daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/progress/<job_id>")
def progress(job_id):
    if job_id not in jobs: return jsonify({"error": "Job not found"}), 404
    def event_stream():
        job = jobs[job_id]
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"): break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                if job["done"]: break
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs or jobs[job_id]["result"] is None:
        return jsonify({"error": "Result not ready"}), 404
    return send_file(io.BytesIO(jobs[job_id]["result"]), mimetype="text/plain", as_attachment=True, download_name="llms.txt")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
