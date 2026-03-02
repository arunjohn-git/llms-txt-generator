# MUST BE AT THE VERY TOP
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
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse
from collections import Counter, defaultdict
from flask import Flask, request, render_template, send_file, jsonify, Response, stream_with_context
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "llms-txt-secret-2026")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; llms-txt-generator/1.0)"}
jobs = {}

# ─────────────────────────────────────────────────────
# URL & SITEMAP UTILITIES
# ─────────────────────────────────────────────────────

def clean_url(url):
    try:
        parsed = urlparse(url)
        path = re.sub(r'/{2,}', '/', parsed.path)
        return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
    except: return url

def parse_sitemap(source, is_file=False):
    def extract_urls(xml_text):
        urls = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for child in root.findall("sm:sitemap", ns):
                loc = child.find("sm:loc", ns)
                if loc is not None and loc.text:
                    try:
                        r = requests.get(loc.text.strip(), headers=HEADERS, timeout=10)
                        if r.status_code == 200: urls.extend(extract_urls(r.text))
                    except: pass
            for url_el in root.findall("sm:url", ns):
                loc = url_el.find("sm:loc", ns)
                if loc is not None and loc.text: urls.append(loc.text.strip())
        except: pass
        return urls

    if is_file:
        xml_text = source.decode("utf-8", errors="ignore") if isinstance(source, bytes) else source
    else:
        try:
            r = requests.get(source, headers=HEADERS, timeout=15)
            xml_text = r.text if r.status_code == 200 else ""
        except: xml_text = ""
    return extract_urls(xml_text)

# ─────────────────────────────────────────────────────
# PRODUCT DETECTION
# ─────────────────────────────────────────────────────

def find_divergence_depth(urls):
    parsed_paths = [[p for p in urlparse(u).path.strip('/').split('/') if p] for u in urls]
    if not parsed_paths: return 1
    min_depth = min(len(p) for p in parsed_paths)
    common_depth = 0
    for depth in range(min_depth):
        if len(set(p[depth] for p in parsed_paths if len(p) > depth)) == 1:
            common_depth = depth + 1
        else: break
    return common_depth + 1

def build_product_map(urls):
    if not urls: return {}
    domain_urls = defaultdict(list)
    for u in urls: domain_urls[urlparse(u).netloc].append(u)
    
    product_map = {}
    for domain, u_list in domain_groups.items():
        depth = find_divergence_depth(u_list)
        for url in u_list:
            parts = [p for p in urlparse(url).path.strip('/').split('/') if p]
            key = '/'.join(parts[:depth])
            product_map[url] = key.replace('-', ' ').title() if key else domain
    return product_map

# ─────────────────────────────────────────────────────
# AI PIPELINE & QA
# ─────────────────────────────────────────────────────

def summarize_gpt(url, content):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a technical writer. Return JSON: {'title': '...', 'description': '...'}"},
                      {"role": "user", "content": f"URL: {url}\nContent: {content[:3500]}"}],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except: return None

def fix_quality_cloud(summaries, q_progress):
    # Phase 1: Structural Fixes
    q_progress.put({"type": "stage", "msg": "QA Phase 1 — Structural Fixes", "pct": 88})
    title_count = Counter(s['title'] for s in summaries)
    for s in summaries:
        if title_count[s['title']] > 1:
            s['title'] = s['url'].rstrip('/').split('/')[-1].replace('-', ' ').title()
    
    # Phase 2: Similarity Check
    q_progress.put({"type": "stage", "msg": "QA Phase 2 — Differentiation", "pct": 92})
    # logic to de-duplicate highly similar descriptions
    
    return summaries

# ─────────────────────────────────────────────────────
# FLASK ENGINE
# ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
def start():
    mode = request.form.get("input_mode", "csv")
    urls = []
    
    if mode == "csv" and "csv_file" in request.files:
        stream = io.StringIO(request.files["csv_file"].stream.read().decode("utf-8"))
        urls = [cell.strip() for row in csv.reader(stream) for cell in row if cell.strip().startswith("http")]
    elif mode == "sitemap":
        urls = parse_sitemap(request.form.get("sitemap_url"))
    
    urls = [clean_url(u) for u in list(dict.fromkeys(urls))]
    if not urls: return jsonify({"error": "No URLs found"}), 400

    job_id = str(int(time.time() * 1000))
    q = queue.Queue()
    jobs[job_id] = {"queue": q, "result": None, "done": False}

    def pipeline():
        try:
            pages = []
            q.put({"type": "stage", "msg": "Web Fetching", "pct": 5})
            for i, u in enumerate(urls, 1):
                res = requests.get(u, timeout=10, headers=HEADERS)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    for s in soup(["script", "style"]): s.decompose()
                    pages.append({"url": u, "content": soup.get_text()[:5000]})
                q.put({"type": "fetch", "current": i, "total": len(urls), "url": u, "ok": res.status_code == 200})

            q.put({"type": "stage", "msg": "Summarising with GPT", "pct": 40})
            summaries = []
            for i, p in enumerate(pages, 1):
                res = summarize_gpt(p['url'], p['content'])
                if res: summaries.append({"url": p['url'], **res})
                q.put({"type": "summarize", "current": i, "total": len(pages), "url": p['url'], "ok": res is not None})

            summaries = fix_quality_cloud(summaries, q)
            
            output = "\n".join([f"- Source: {s['url']}\n  Title: {s['title']}\n  Description: {s['description']}\n" for s in summaries])
            jobs[job_id]["result"] = output.encode("utf-8")
            q.put({"type": "done", "total": len(summaries)})
        except Exception as e: q.put({"type": "error", "msg": str(e)})
        finally: jobs[job_id]["done"] = True

    threading.Thread(target=pipeline, daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/progress/<job_id>")
def progress(job_id):
    def stream():
        q = jobs[job_id]["queue"]
        while True:
            msg = q.get()
            yield f"data: {json.dumps(msg)}\n\n"
            if msg["type"] in ("done", "error"): break
    return Response(stream_with_context(stream()), mimetype="text/event-stream")

@app.route("/download/<job_id>")
def download(job_id):
    return send_file(io.BytesIO(jobs[job_id]["result"]), as_attachment=True, download_name="llms.txt")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
