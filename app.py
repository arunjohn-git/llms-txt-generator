# MUST BE AT THE VERY TOP
from gevent import monkey
monkey.patch_all()

import os
import requests
import time
import json
import io
import threading
import queue
import re
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, send_file
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "llms-txt-secret-2026")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Use the logic from your local setup
jobs = {}

# ─────────────────────────────────────────────────────
# CORE LOGIC FROM YOUR LOCAL SETUP
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
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
        content = soup.get_text(separator=' ', strip=True)[:6000]
        return content if len(content) > 100 else None
    except: return None

# ─────────────────────────────────────────────────────
# CLOUD-ADAPTED SUMMARIZATION (OpenAI instead of Ollama)
# ─────────────────────────────────────────────────────

def summarize_with_openai(url, content):
    """Replaces your local 'summarize' function using OpenAI."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a technical writer creating llms.txt entries. Return raw JSON: {'title': '...', 'description': '...'}"},
                {"role": "user", "content": f"URL: {url}\nContent: {content[:3500]}"}
            ],
            response_format={ "type": "json_object" }
        )
        return json.loads(response.choices[0].message.content)
    except: return None

# ─────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return "OK", 200

@app.route('/start', methods=['POST'])
def start():
    # Detect if it's a direct URL or a file upload from your UI
    target_url = request.form.get('url')
    
    # Simple list creation (you can add your sitemap logic back here)
    urls = [target_url] 
    
    job_id = str(int(time.time() * 1000))
    q = queue.Queue()
    jobs[job_id] = {"queue": q, "result": None, "done": False}

    def run_pipeline():
        try:
            total = len(urls)
            q.put({"type": "stage", "msg": "Web Fetching", "pct": 5})
            
            pages = []
            for i, url in enumerate(urls, 1):
                content = fetch_page(url)
                if content:
                    pages.append({"url": url, "content": content})
                q.put({"type": "fetch", "current": i, "total": total, "url": url, "ok": content is not None})

            q.put({"type": "stage", "msg": "Summarising with GPT-4o-mini", "pct": 40})
            summaries = []
            for i, page in enumerate(pages, 1):
                result = summarize_with_openai(page['url'], page['content'])
                if result:
                    summaries.append({"url": page['url'], **result})
                q.put({"type": "summarize", "current": i, "total": len(pages), "url": page['url'], "ok": result is not None})

            # Final Step
            output = ""
            for s in summaries:
                output += f"- Source: {s['url']}\n  Title: {s['title']}\n  Description: {s['description']}\n\n"
            
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
            except:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                if job["done"]: break
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")

@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs or jobs[job_id]["result"] is None:
        return jsonify({"error": "Not ready"}), 404
    return send_file(io.BytesIO(jobs[job_id]["result"]), mimetype="text/plain", as_attachment=True, download_name="llms.txt")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
