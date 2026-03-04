import csv
import re
import json
import time
import requests
import ollama
import io
import threading
import queue
from flask import Flask, request, render_template_string, send_file, jsonify, Response, stream_with_context
from collections import Counter, defaultdict
from urllib.parse import urlparse, urlunparse

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

MODEL   = "mistral"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; llms-txt-generator/1.0)"}
jobs    = {}

# ─────────────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────────────
def call_llm(prompt):
    response = ollama.chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    return response["message"]["content"].strip()

# ─────────────────────────────────────────────────────
# URL UTILITIES
# ─────────────────────────────────────────────────────
def clean_url(url):
    try:
        parsed = urlparse(url)
        path   = re.sub(r'/{2,}', '/', parsed.path)
        return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
    except:
        return url

# ─────────────────────────────────────────────────────
# SITEMAP PARSING
# ─────────────────────────────────────────────────────
def parse_sitemap(source, is_file=False):
    import xml.etree.ElementTree as ET

    def extract_urls(xml_text):
        urls = []
        try:
            root = ET.fromstring(xml_text)
            ns   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            for child in root.findall("sm:sitemap", ns):
                loc = child.find("sm:loc", ns)
                if loc is not None and loc.text:
                    try:
                        r = requests.get(loc.text.strip(), headers=HEADERS, timeout=10)
                        if r.status_code == 200:
                            urls.extend(extract_urls(r.text))
                    except:
                        pass
            for url_el in root.findall("sm:url", ns):
                loc = url_el.find("sm:loc", ns)
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
        except ET.ParseError:
            pass
        return urls

    if is_file:
        xml_text = source.decode("utf-8", errors="ignore") if isinstance(source, bytes) else source
    else:
        try:
            r = requests.get(source, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                return [], f"Could not fetch sitemap (HTTP {r.status_code})"
            xml_text = r.text
        except Exception as e:
            return [], str(e)

    return extract_urls(xml_text), None

# ─────────────────────────────────────────────────────
# PAGE FETCHING
# ─────────────────────────────────────────────────────
def extract_meta(html):
    def clean(s):
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"&amp;", "&", s)
        s = re.sub(r"&quot;", '"', s)
        s = re.sub(r"&#39;", "'", s)
        s = re.sub(r"&[a-z]+;", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    meta_title = clean(m.group(1)) if m else ""
    meta_title = re.split(r"\s*[|\u2013\u2014]\s*", meta_title)[0].strip()

    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.IGNORECASE | re.DOTALL)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'"][^>]+name=["\']description["\']', html, re.IGNORECASE | re.DOTALL)
    meta_desc = clean(m.group(1)) if m else ""

    if not meta_desc:
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', html, re.IGNORECASE | re.DOTALL)
        if not m:
            m = re.search(r'<meta[^>]+content=["\'](.*?)["\'"][^>]+property=["\']og:description["\']', html, re.IGNORECASE | re.DOTALL)
        meta_desc = clean(m.group(1)) if m else ""

    if not meta_title:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.IGNORECASE | re.DOTALL)
        meta_title = clean(m.group(1)) if m else ""

    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    h1 = clean(m.group(1)) if m else ""

    h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.IGNORECASE | re.DOTALL)
    h2s = [clean(h) for h in h2s if len(clean(h)) > 3][:6]

    return {
        "meta_title": meta_title[:200],
        "meta_desc" : meta_desc[:400],
        "h1"        : h1[:150],
        "h2s"       : " | ".join(h2s)[:300] if h2s else "",
    }

def extract_main_content(html):
    for tag in ["main", "article"]:
        pat = re.compile(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", re.DOTALL | re.IGNORECASE)
        m = pat.search(html)
        if m and len(m.group(1)) > 200:
            return m.group(1)

    pat = re.compile(r'<[^>]+role="main"[^>]*>(.*?)</(?:div|section|main)>', re.DOTALL | re.IGNORECASE)
    m = pat.search(html)
    if m and len(m.group(1)) > 200:
        return m.group(1)

    for id_val in ["content", "main", "main-content", "page-content", "article", "primary"]:
        pat = re.compile(r'<(?:div|section)[^>]+id="' + id_val + r'"[^>]*>(.*?)</(?:div|section)>', re.DOTALL | re.IGNORECASE)
        m = pat.search(html)
        if m and len(m.group(1)) > 200:
            return m.group(1)

    for cls_val in ["content", "main", "article", "entry-content", "post-content"]:
        pat = re.compile(r'<(?:div|section)[^>]+class="[^"]*' + cls_val + r'[^"]*"[^>]*>(.*?)</(?:div|section)>', re.DOTALL | re.IGNORECASE)
        m = pat.search(html)
        if m and len(m.group(1)) > 200:
            return m.group(1)

    return html

def fetch_page(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        raw_html = r.text
        meta     = extract_meta(raw_html)

        html = raw_html
        for tag in ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "svg", "form"]:
            html = re.sub(r"<" + tag + r"[^>]*>.*?</" + tag + r">", " ", html, flags=re.DOTALL | re.IGNORECASE)

        for noise in ["sidebar", "widget", "banner", "promo", "advertisement",
                      "breadcrumb", "pagination", "related", "social", "share",
                      "cookie", "popup", "modal", "overlay", "newsletter"]:
            pat = re.compile(
                r'<(?:div|section|ul|span)[^>]+(?:class|id)="[^"]*' + noise + r'[^"]*"[^>]*>.*?</(?:div|section|ul|span)>',
                re.DOTALL | re.IGNORECASE
            )
            html = pat.sub(" ", html)

        main_html = extract_main_content(html)
        body = re.sub(r"<[^>]+>", " ", main_html)
        body = re.sub(r"\+\d[\d\s\-(). ]{7,20}", " ", body)
        body = re.sub(r"[A-Fa-f0-9]{40,}", " ", body)
        body = re.sub(r"\s+", " ", body).strip()

        parts = []
        if meta["meta_title"]:
            parts.append(f"META TITLE: {meta['meta_title']}")
        if meta["meta_desc"]:
            parts.append(f"META DESCRIPTION: {meta['meta_desc']}")
        if meta["h1"] and meta["h1"].lower() != meta["meta_title"].lower():
            parts.append(f"H1: {meta['h1']}")
        if meta["h2s"]:
            parts.append(f"H2s: {meta['h2s']}")
        if body:
            parts.append(f"CONTENT: {body[:2000]}")

        structured = "\n".join(parts)
        return structured if len(structured) > 100 else None
    except:
        return None

# ─────────────────────────────────────────────────────
# SUMMARIZATION
# ─────────────────────────────────────────────────────
SUMMARIZE_PROMPT = """You are a technical writer creating llms.txt entries. These entries are read by AI crawlers to understand what a page contains and who it is for — so precision and distinctiveness matter far more than marketing language.

Read the page content carefully and return ONLY a JSON object.

STEP 1 — Classify page type:
feature-list, pricing, download, support, demo, release-notes, documentation, blog, faq, about, legal, other

STEP 2 — Infer primary audience from the language and content of the page:
it-admin, end-user, developer, executive, general
Do NOT guess from the URL. Read the actual content.

STEP 3 — Write title and description.

TITLE (5-8 words):
- Reflect the specific purpose of THIS page
- No dates, version numbers, build numbers
- Must be distinct from any generic product name

DESCRIPTION rules by page type:
- feature-list:   Name the 3-4 most important capability areas. No bullet dumps.
- pricing:        State editions/tiers, pricing model, and what differentiates them.
- download:       State what is downloaded, trial length, platform requirements.
- support:        State exactly what support channels exist on this page.
- demo:           State what the demo shows and how to access it.
- release-notes:  State what kinds of updates are covered and the time range.
- documentation:  State the specific topic covered and who benefits.
- faq:            State the main topic areas addressed — not a feature list.
- blog/about/other: Describe what is unique about this specific page.

UNIVERSAL DESCRIPTION RULES:
- FIRST SENTENCE must contain the single most distinctive, specific fact about this page.
- Weave the audience in naturally: "IT administrators can...", "End users who need to...", "Developers integrating..."
- Maximum 3 sentences. Every sentence must add new information — no repetition.
- No comma-separated lists of more than 3 items.
- No filler openers: never start with "This page", "The page", "A guide", "A list", "Users to", "Covers"
- No nav links, sidebar content, phone numbers, or footer content.
- Active voice, present tense.

JSON FORMAT — double quotes only, no trailing commas, raw JSON only:
{{"title": "...", "description": "..."}}

URL: {url}
Page content:
{content}"""

RESCORE_PROMPT = """You are evaluating an llms.txt description. Score 1-5 on specificity and rewrite if needed.

1 = Generic filler, comma-list dump, or starts with "This page / The page / A guide"
2 = Vague — could describe any page in this category
3 = Adequate — accurate but lacks the most distinctive fact
4 = Good — specific, accurate, audience-aware
5 = Excellent — leads with the most distinctive fact, audience clear, zero padding

IF score <= 2: rewrite — first sentence leads with the single most distinctive fact, audience woven in naturally, max 3 sentences, no filler openers, active voice, present tense.
IF score >= 3: return description unchanged.

URL: {url}
Page content (first 1500 chars): {content}
Current description: {description}

Return ONLY JSON: {{"score": <1-5>, "description": "<final description>"}}"""

DIFFERENTIATE_PROMPT = """Two pages on the same site have descriptions that are too similar. Rewrite Page B's description to focus ONLY on what is unique to Page B.

Max 3 sentences. Lead with the most distinctive fact about Page B. Active voice, present tense. No filler openers.

Page A URL: {url_a}
Page A description: {desc_a}

Page B URL: {url_b}
Page B content: {content_b}
Page B current description: {desc_b}

Return ONLY JSON: {{"description": "<rewritten description for Page B>"}}"""

def summarize(url, content):
    snippet = content[:3500].strip()
    if not snippet:
        return None
    for attempt in range(3):
        try:
            raw = call_llm(SUMMARIZE_PROMPT.format(url=url, content=snippet))
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            if "title" in result and "description" in result:
                return result
        except:
            if attempt < 2:
                time.sleep(0.5)
    return None

def rescore_and_fix(url, content, description):
    try:
        raw = call_llm(RESCORE_PROMPT.format(url=url, content=content[:1500], description=description))
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        desc   = result.get("description", description).strip()
        return int(result.get("score", 3)), desc if desc else description
    except:
        return 3, description

# ─────────────────────────────────────────────────────
# QUALITY ASSURANCE
# ─────────────────────────────────────────────────────
FILLER_STARTERS = [
    "this page ", "the page ", "this guide ", "this tool ", "this api ",
    "this document ", "this article ", "this section ",
    "a quick overview", "an overview of", "a guide to", "a guide for",
    "a list of", "a collection of", "detailed information",
    "users to ", "users the ", "covers ",
]

def strip_filler_opener(desc):
    d, lower = desc.strip(), desc.strip().lower()
    for starter in FILLER_STARTERS:
        if lower.startswith(starter):
            remainder = d[len(starter):].strip()
            if remainder:
                return remainder[0].upper() + remainder[1:]
    return d

def score_description(desc):
    issues = []
    d, lower = desc.strip(), desc.strip().lower()
    for starter in FILLER_STARTERS:
        if lower.startswith(starter):
            issues.append("filler_opener"); break
    if len(d) < 60:
        issues.append("too_short")
    if re.match(r'^(to |for |by )', lower):
        issues.append("fragment")
    if len(d.split()) > 65:
        issues.append("too_long")
    for sent in [s.strip() for s in d.split('.') if s.strip()]:
        if sent.count(',') >= 4:
            issues.append("comma_list"); break
    if lower.rstrip().endswith('and more') or lower.rstrip().endswith('and more.'):
        issues.append("and_more_ending")
    if any(i in issues for i in ("filler_opener", "too_short", "fragment")):
        return 1, issues
    if any(i in issues for i in ("too_long", "comma_list", "and_more_ending")):
        return 2, issues
    return 4, issues

def description_similarity(a, b):
    words_a = set(re.findall(r'\b\w{4,}\b', a.lower()))
    words_b = set(re.findall(r'\b\w{4,}\b', b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))

def fix_quality(summaries, page_map, progress_q=None):
    total = len(summaries)

    # Phase 1 — structural fixes (no LLM)
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 1 — Structural fixes", "pct": 86})

    stripped = 0
    for item in summaries:
        fixed = strip_filler_opener(item["description"])
        if fixed != item["description"]:
            item["description"] = fixed
            stripped += 1

    title_count = Counter(item["title"] for item in summaries)
    dup_fixed   = 0
    for item in summaries:
        if title_count[item["title"]] > 1:
            slug = re.sub(r'\.html?$', '', item["url"].rstrip("/").split("/")[-1])
            slug = slug.replace("-", " ").replace("_", " ").title()
            if slug:
                item["title"] = slug
                dup_fixed += 1

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": stripped, "dups": dup_fixed})

    # Phase 2 — LLM rescore low-quality entries
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 2 — LLM scoring & rewrite", "pct": 90})

    rescore_fixed = 0
    for i, item in enumerate(summaries):
        score, _ = score_description(item["description"])
        if score <= 2:
            content = page_map.get(item["url"], "")
            if content:
                _, new_desc = rescore_and_fix(item["url"], content, item["description"])
                if new_desc != item["description"]:
                    item["description"] = new_desc
                    rescore_fixed += 1
            if progress_q and i % 5 == 0:
                progress_q.put({"type": "qa_rescore", "current": i + 1, "total": total})

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": rescore_fixed, "dups": 0})

    # Phase 3 — sibling deduplication
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 3 — Sibling differentiation", "pct": 94})

    domain_groups = defaultdict(list)
    for item in summaries:
        domain_groups[urlparse(item["url"]).netloc].append(item)

    dedup_fixed = 0
    for items in domain_groups.values():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if description_similarity(items[i]["description"], items[j]["description"]) >= 0.70:
                    content_b = page_map.get(items[j]["url"], "")
                    if not content_b:
                        continue
                    try:
                        raw = call_llm(DIFFERENTIATE_PROMPT.format(
                            url_a=items[i]["url"], desc_a=items[i]["description"],
                            url_b=items[j]["url"], content_b=content_b[:2000],
                            desc_b=items[j]["description"]
                        ))
                        if raw.startswith("```"):
                            raw = raw.split("```")[1]
                            if raw.startswith("json"):
                                raw = raw[4:]
                        new_desc = json.loads(raw.strip()).get("description", "").strip()
                        if new_desc and len(new_desc) > 60:
                            items[j]["description"] = new_desc
                            dedup_fixed += 1
                    except:
                        pass

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": dedup_fixed, "dups": 0})

    return summaries

# ─────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────
def generate_llms_txt(summaries):
    seen, lines = set(), []
    for item in summaries:
        if item["url"] not in seen:
            seen.add(item["url"])
            lines.append(f"- Source: {item['url']}")
            lines.append(f"  Title: {item['title']}")
            lines.append(f"  Description: {item['description']}")
            lines.append("")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────
# HTML UI
# ─────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>llms.txt Generator</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f0f2f8; min-height: 100vh;
      display: flex; align-items: flex-start; justify-content: center; padding: 40px 24px;
    }
    .card {
      background: #fff; border-radius: 20px;
      box-shadow: 0 4px 32px rgba(0,0,0,0.09);
      padding: 48px; width: 100%; max-width: 640px;
    }
    .logo { font-size: 26px; font-weight: 800; color: #1a1a2e; margin-bottom: 4px; }
    .logo span { color: #6c47ff; }
    .subtitle { color: #777; font-size: 14px; margin-bottom: 24px; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px;
      background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0;
      border-radius: 20px; font-size: 11px; font-weight: 700;
      padding: 3px 12px; margin-bottom: 28px;
    }
    label { display: block; font-size: 13px; font-weight: 600; color: #333; margin-bottom: 7px; }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
    .tab {
      flex: 1; padding: 9px 12px; border-radius: 8px; border: 1.5px solid #e0e0e0;
      background: #fafafa; font-size: 12px; font-weight: 600; color: #888;
      cursor: pointer; transition: all 0.2s;
    }
    .tab:hover { border-color: #6c47ff; color: #6c47ff; }
    .tab.active { background: #6c47ff; border-color: #6c47ff; color: #fff; }
    .upload-area {
      border: 2px dashed #d5d5d5; border-radius: 12px; padding: 28px;
      text-align: center; cursor: pointer; transition: all 0.2s;
      margin-bottom: 20px; position: relative; background: #fafafa;
    }
    .upload-area:hover, .upload-area.drag-over { border-color: #6c47ff; background: #f5f2ff; }
    .upload-area input[type="file"] {
      position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
    }
    .upload-icon { font-size: 26px; margin-bottom: 6px; }
    .upload-text { font-size: 14px; color: #555; font-weight: 500; }
    .upload-hint { font-size: 12px; color: #aaa; margin-top: 4px; }
    .file-name { font-size: 13px; color: #6c47ff; font-weight: 600; margin-top: 8px; }
    .sitemap-input {
      width: 100%; padding: 12px 14px; border: 1.5px solid #e0e0e0;
      border-radius: 10px; font-size: 14px; outline: none; margin-bottom: 20px;
      transition: border-color 0.2s;
    }
    .sitemap-input:focus { border-color: #6c47ff; }
    .sitemap-hint { font-size: 11px; color: #aaa; margin-top: -16px; margin-bottom: 20px; }
    #submitBtn {
      width: 100%; padding: 14px; background: #6c47ff; color: #fff;
      border: none; border-radius: 10px; font-size: 15px; font-weight: 700;
      cursor: pointer; transition: background 0.2s;
    }
    #submitBtn:hover:not(:disabled) { background: #5735e0; }
    #submitBtn:disabled { background: #b0a0f0; cursor: not-allowed; }
    #progressPanel { display: none; margin-top: 32px; }
    .stages {
      display: flex; margin-bottom: 24px;
      border-radius: 10px; overflow: hidden; border: 1px solid #e5e7eb;
    }
    .stage-step {
      flex: 1; padding: 10px 4px; text-align: center;
      font-size: 10px; font-weight: 600; color: #aaa;
      background: #f9fafb; border-right: 1px solid #e5e7eb; transition: all 0.3s;
    }
    .stage-step:last-child { border-right: none; }
    .step-icon { font-size: 15px; display: block; margin-bottom: 3px; }
    .stage-step.active { background: #6c47ff; color: #fff; }
    .stage-step.done   { background: #ecfdf5; color: #065f46; }
    .progress-wrap { margin-bottom: 20px; }
    .progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .progress-label { font-size: 13px; font-weight: 700; color: #333; display: flex; align-items: center; gap: 8px; }
    .spinner {
      width: 14px; height: 14px; border: 2px solid #e0e0e0;
      border-top-color: #6c47ff; border-radius: 50%; animation: spin 0.7s linear infinite;
    }
    .progress-count { font-size: 13px; color: #6c47ff; font-weight: 700; }
    .progress-track { background: #f0ecff; border-radius: 99px; height: 8px; overflow: hidden; }
    .progress-fill {
      background: linear-gradient(90deg, #6c47ff, #a78bfa);
      height: 100%; border-radius: 99px; transition: width 0.5s ease; width: 0%;
    }
    .progress-sub { font-size: 11px; color: #999; margin-top: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .log-wrap { border: 1px solid #e5e7eb; border-radius: 10px; overflow: hidden; }
    .log-header { background: #1e1e2e; padding: 8px 14px; font-size: 11px; font-weight: 700; color: #6c47ff; letter-spacing: 0.5px; }
    .log-area { background: #1e1e2e; padding: 12px 14px; max-height: 200px; overflow-y: auto; }
    .log-line { font-family: monospace; font-size: 12px; line-height: 1.8; }
    .log-line.info    { color: #94a3b8; }
    .log-line.success { color: #4ade80; }
    .log-line.warning { color: #fb923c; }
    .log-line.stage   { color: #60cdff; font-weight: bold; }
    .log-line.error   { color: #f87171; }
    .log-line.qa      { color: #c084fc; }
    .result-box {
      display: none; margin-top: 20px; padding: 18px 20px;
      background: #f0fdf4; border: 1.5px solid #6ee7b7;
      border-radius: 12px; align-items: center; justify-content: space-between; gap: 12px;
    }
    .result-box.show { display: flex; }
    .result-info { font-size: 13px; color: #065f46; font-weight: 600; }
    .result-info span { display: block; font-size: 12px; font-weight: 400; color: #047857; margin-top: 2px; }
    .download-btn {
      flex-shrink: 0; background: #059669; color: #fff; border: none;
      border-radius: 8px; padding: 10px 20px; font-size: 13px;
      font-weight: 700; cursor: pointer; transition: background 0.2s;
    }
    .download-btn:hover { background: #047857; }
    .error-box {
      display: none; margin-top: 20px; padding: 14px 16px;
      background: #fef2f2; border: 1.5px solid #fca5a5;
      border-radius: 10px; font-size: 13px; color: #991b1b;
    }
    .error-box.show { display: block; }
  </style>
</head>
<body>
<div class="card">
  <div class="logo">llms<span>.txt</span> Generator</div>
  <div class="subtitle">Upload a CSV or sitemap → get a production-ready llms.txt instantly</div>
  <div class="badge">⚡ Mistral · Runs Locally · Zero Cost</div>

  <form id="genForm">
    <label>Input</label>
    <div class="tabs">
      <button type="button" class="tab active" onclick="switchTab(this,'csv')">📄 CSV Upload</button>
      <button type="button" class="tab" onclick="switchTab(this,'sitemap')">🗺 Sitemap URL</button>
      <button type="button" class="tab" onclick="switchTab(this,'sitemapfile')">📁 Sitemap File</button>
    </div>

    <div class="tab-panel" id="panel-csv">
      <div class="upload-area" id="uploadArea">
        <input type="file" id="csv_file" accept=".csv">
        <div class="upload-icon">📄</div>
        <div class="upload-text">Click to upload or drag & drop</div>
        <div class="upload-hint">One URL per row</div>
        <div class="file-name" id="fileName"></div>
      </div>
    </div>

    <div class="tab-panel" id="panel-sitemap" style="display:none">
      <input class="sitemap-input" type="url" id="sitemap_url" placeholder="https://yoursite.com/sitemap.xml">
      <div class="sitemap-hint">Sitemap index files supported — child sitemaps crawled automatically</div>
    </div>

    <div class="tab-panel" id="panel-sitemapfile" style="display:none">
      <div class="upload-area" id="uploadAreaXml">
        <input type="file" id="sitemap_file" accept=".xml">
        <div class="upload-icon">🗺</div>
        <div class="upload-text">Click to upload sitemap.xml</div>
        <div class="upload-hint">Sitemap index supported</div>
        <div class="file-name" id="fileNameXml"></div>
      </div>
    </div>

    <button type="submit" id="submitBtn">Generate llms.txt</button>
  </form>

  <div id="progressPanel">
    <div class="stages">
      <div class="stage-step" id="step-fetch"><span class="step-icon">🌐</span>Fetching</div>
      <div class="stage-step" id="step-summarize"><span class="step-icon">🤖</span>Summarising</div>
      <div class="stage-step" id="step-qa"><span class="step-icon">✅</span>QA & Fix</div>
      <div class="stage-step" id="step-generate"><span class="step-icon">📄</span>Generating</div>
    </div>
    <div class="progress-wrap">
      <div class="progress-header">
        <div class="progress-label">
          <div class="spinner" id="spinner"></div>
          <span id="progressLabel">Starting...</span>
        </div>
        <div class="progress-count" id="progressCount"></div>
      </div>
      <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
      <div class="progress-sub" id="progressSub"></div>
    </div>
    <div class="log-wrap">
      <div class="log-header">LIVE LOG</div>
      <div class="log-area" id="logArea"></div>
    </div>
    <div class="result-box" id="resultBox">
      <div class="result-info">🎉 llms.txt is ready!<span id="resultSub"></span></div>
      <button class="download-btn" id="downloadBtn">⬇ Download llms.txt</button>
    </div>
    <div class="error-box" id="errorBox"></div>
  </div>
</div>

<script>
  let resultBlob = null
  let activeTab  = 'csv'
  const STAGES   = ['fetch', 'summarize', 'qa', 'generate']

  function switchTab(btn, tab) {
    activeTab = tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'))
    btn.classList.add('active')
    document.querySelectorAll('.tab-panel').forEach(p => p.style.display = 'none')
    document.getElementById('panel-' + tab).style.display = 'block'
  }

  function setStage(name) {
    let passed = true
    STAGES.forEach(s => {
      const el = document.getElementById('step-' + s)
      if (!el) return
      el.classList.remove('active', 'done')
      if (s === name) { el.classList.add('active'); passed = false }
      else if (passed) el.classList.add('done')
    })
  }

  function setProgress(label, pct, count='', sub='') {
    document.getElementById('progressLabel').textContent = label
    document.getElementById('progressFill').style.width  = pct + '%'
    document.getElementById('progressCount').textContent = count
    document.getElementById('progressSub').textContent   = sub
  }

  function addLog(msg, type='info') {
    const area = document.getElementById('logArea')
    const line = document.createElement('div')
    line.className   = 'log-line ' + type
    const ts         = new Date().toLocaleTimeString('en-US', {hour12: false})
    line.textContent = `[${ts}]  ${msg}`
    area.appendChild(line)
    area.scrollTop = area.scrollHeight
  }

  function showError(msg) {
    document.getElementById('spinner').style.display = 'none'
    const box = document.getElementById('errorBox')
    box.textContent = '❌ ' + msg
    box.classList.add('show')
    addLog('❌ ' + msg, 'error')
    setProgress('Failed', 0)
  }

  document.getElementById('csv_file')?.addEventListener('change', function() {
    document.getElementById('fileName').textContent = this.files[0] ? '✓ ' + this.files[0].name : ''
  })
  document.getElementById('sitemap_file')?.addEventListener('change', function() {
    document.getElementById('fileNameXml').textContent = this.files[0] ? '✓ ' + this.files[0].name : ''
  })

  const area = document.getElementById('uploadArea')
  if (area) {
    area.addEventListener('dragover', e => { e.preventDefault(); area.classList.add('drag-over') })
    area.addEventListener('dragleave', () => area.classList.remove('drag-over'))
    area.addEventListener('drop', e => {
      e.preventDefault(); area.classList.remove('drag-over')
      const file = e.dataTransfer.files[0]
      if (file) {
        document.getElementById('csv_file').files = e.dataTransfer.files
        document.getElementById('fileName').textContent = '✓ ' + file.name
      }
    })
  }

  document.getElementById('genForm')?.addEventListener('submit', async function(e) {
    e.preventDefault()
    const btn   = document.getElementById('submitBtn')
    const panel = document.getElementById('progressPanel')

    if (activeTab === 'csv' && !document.getElementById('csv_file').files[0]) {
      showError('Please upload a CSV file'); return
    }
    if (activeTab === 'sitemap' && !document.getElementById('sitemap_url').value.trim()) {
      showError('Please enter a sitemap URL'); return
    }
    if (activeTab === 'sitemapfile' && !document.getElementById('sitemap_file').files[0]) {
      showError('Please upload a sitemap file'); return
    }

    document.getElementById('logArea').innerHTML = ''
    document.getElementById('resultBox').classList.remove('show')
    document.getElementById('errorBox').classList.remove('show')
    document.getElementById('progressFill').style.width = '0%'
    document.getElementById('spinner').style.display    = ''
    resultBlob = null

    btn.disabled = true; btn.textContent = 'Processing...'
    panel.style.display = 'block'
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' })

    setStage('fetch')
    setProgress('Loading...', 2)
    addLog('Pipeline started', 'stage')

    const formData = new FormData()
    formData.set('input_mode', activeTab)
    if (activeTab === 'csv')         formData.set('csv_file', document.getElementById('csv_file').files[0])
    if (activeTab === 'sitemap')     formData.set('sitemap_url', document.getElementById('sitemap_url').value.trim())
    if (activeTab === 'sitemapfile') formData.set('sitemap_file', document.getElementById('sitemap_file').files[0])

    let jobId
    try {
      const res = await fetch('/start', { method: 'POST', body: formData })
      if (!res.ok) { const err = await res.json(); throw new Error(err.error) }
      jobId = (await res.json()).job_id
    } catch (err) {
      showError(err.message)
      btn.disabled = false; btn.textContent = 'Generate llms.txt'; return
    }

    const evtSource = new EventSource('/progress/' + jobId)
    evtSource.onmessage = function(e) {
      const d = JSON.parse(e.data)
      if (d.type === 'ping') return

      if (d.type === 'stage') {
        addLog('▶ ' + d.msg, 'stage')
        setProgress(d.msg, d.pct || 0)
        if (d.msg.toLowerCase().includes('fetch'))   setStage('fetch')
        if (d.msg.toLowerCase().includes('summari')) setStage('summarize')
        if (d.msg.toLowerCase().includes('qa') || d.msg.toLowerCase().includes('quality')) setStage('qa')
        if (d.msg.toLowerCase().includes('generat')) setStage('generate')

      } else if (d.type === 'fetch') {
        setProgress('Fetching pages', Math.round((d.current/d.total)*28)+4, `${d.current} / ${d.total}`, d.url)
        addLog(`${d.ok?'✓':'✗'} [${d.current}/${d.total}] ${d.url}`, d.ok?'success':'warning')

      } else if (d.type === 'summarize') {
        setProgress('Summarising with Mistral', Math.round((d.current/d.total)*46)+33, `${d.current} / ${d.total}`, d.url)
        addLog(`${d.ok?'✓':'✗'} [${d.current}/${d.total}] ${d.url}`, d.ok?'success':'warning')

      } else if (d.type === 'qa_result') {
        if (d.fixed > 0 || d.dups > 0) addLog(`  ↳ Fixed ${d.fixed} descriptions · ${d.dups} duplicate titles resolved`, 'qa')

      } else if (d.type === 'qa_rescore') {
        setProgress('LLM Scoring & Rewriting', 90 + Math.round((d.current/d.total)*7), `${d.current} / ${d.total}`)

      } else if (d.type === 'done') {
        evtSource.close()
        STAGES.forEach(s => {
          const el = document.getElementById('step-' + s)
          if (el) { el.classList.remove('active'); el.classList.add('done') }
        })
        setProgress('Complete!', 100, `${d.total} entries`)
        addLog(`✅ Done — ${d.total} entries generated`, 'success')
        document.getElementById('spinner').style.display = 'none'
        fetch('/download/' + jobId).then(r => r.blob()).then(blob => {
          resultBlob = blob
          document.getElementById('resultSub').textContent = `${d.total} pages summarized · ready to deploy`
          document.getElementById('resultBox').classList.add('show')
        })
        btn.disabled = false; btn.textContent = 'Generate llms.txt'

      } else if (d.type === 'error') {
        evtSource.close()
        showError(d.msg)
        btn.disabled = false; btn.textContent = 'Generate llms.txt'
      }
    }
    evtSource.onerror = () => evtSource.close()
  })

  document.getElementById('downloadBtn')?.addEventListener('click', function() {
    if (!resultBlob) return
    const url = URL.createObjectURL(resultBlob)
    const a   = document.createElement('a')
    a.href = url; a.download = 'llms.txt'; a.click()
    URL.revokeObjectURL(url)
  })
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/start", methods=["POST"])
def start():
    input_mode = request.form.get("input_mode", "csv")
    urls_raw   = []

    if input_mode == "csv":
        if "csv_file" not in request.files:
            return jsonify({"error": "No CSV file uploaded"}), 400
        stream   = io.StringIO(request.files["csv_file"].stream.read().decode("utf-8-sig"))
        urls_raw = [cell.strip() for row in csv.reader(stream) for cell in row if cell.strip().startswith("http")]

    elif input_mode == "sitemap":
        sitemap_url = request.form.get("sitemap_url", "").strip()
        if not sitemap_url:
            return jsonify({"error": "No sitemap URL provided"}), 400
        urls_raw, err = parse_sitemap(sitemap_url, is_file=False)
        if err:
            return jsonify({"error": f"Sitemap error: {err}"}), 400

    elif input_mode == "sitemapfile":
        if "sitemap_file" not in request.files:
            return jsonify({"error": "No sitemap file uploaded"}), 400
        urls_raw, err = parse_sitemap(request.files["sitemap_file"].stream.read(), is_file=True)
        if err:
            return jsonify({"error": f"Sitemap parse error: {err}"}), 400

    seen = set()
    urls = [u for u in (clean_url(u) for u in urls_raw) if not (u in seen or seen.add(u))]
    if not urls:
        return jsonify({"error": "No valid URLs found"}), 400

    job_id       = str(int(time.time() * 1000))
    q            = queue.Queue()
    jobs[job_id] = {"queue": q, "result": None, "done": False}

    def run_pipeline():
        try:
            total = len(urls)
            q.put({"type": "stage", "msg": f"{total} URLs loaded", "pct": 2})

            # Fetch
            q.put({"type": "stage", "msg": "Fetching pages", "pct": 5})
            pages = []
            for i, url in enumerate(urls, 1):
                content = fetch_page(url)
                ok      = content is not None
                if ok:
                    pages.append({"url": url, "content": content})
                q.put({"type": "fetch", "current": i, "total": total, "url": url, "ok": ok})
                time.sleep(0.2)

            if not pages:
                q.put({"type": "error", "msg": "Could not fetch any pages"}); return

            page_map = {p["url"]: p["content"] for p in pages}

            # Summarize
            q.put({"type": "stage", "msg": "Summarising with Mistral", "pct": 33})
            summaries, failed = [], []
            for i, page in enumerate(pages, 1):
                result = summarize(page["url"], page["content"])
                if result:
                    summaries.append({"url": page["url"], "title": result.get("title", ""), "description": result.get("description", "")})
                else:
                    failed.append(page)
                q.put({"type": "summarize", "current": i, "total": len(pages), "url": page["url"], "ok": result is not None})

            if failed:
                q.put({"type": "stage", "msg": f"Retrying {len(failed)} failed pages", "pct": 80})
                for page in failed:
                    result = summarize(page["url"], page["content"])
                    if result:
                        summaries.append({"url": page["url"], "title": result.get("title", ""), "description": result.get("description", "")})

            if not summaries:
                q.put({"type": "error", "msg": "Could not summarize any pages"}); return

            # QA
            q.put({"type": "stage", "msg": "Quality Assurance & Auto-fix", "pct": 85})
            summaries = fix_quality(summaries, page_map, q)

            # Generate
            q.put({"type": "stage", "msg": "Generating llms.txt", "pct": 97})
            jobs[job_id]["result"] = generate_llms_txt(summaries).encode("utf-8")
            q.put({"type": "done", "total": len(set(s["url"] for s in summaries))})

        except Exception as ex:
            q.put({"type": "error", "msg": str(ex)})
        finally:
            jobs[job_id]["done"] = True

    threading.Thread(target=run_pipeline, daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/progress/<job_id>")
def progress(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    def event_stream():
        job = jobs[job_id]
        q   = job["queue"]
        while True:
            try:
                msg = q.get(timeout=60)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                if job["done"]:
                    break

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs or jobs[job_id]["result"] is None:
        return jsonify({"error": "Result not ready"}), 404
    buf = io.BytesIO(jobs[job_id]["result"])
    buf.seek(0)
    return send_file(buf, mimetype="text/plain", as_attachment=True, download_name="llms.txt")

if __name__ == "__main__":
    print("\n✅ llms.txt Generator running!")
    print("👉 Open: http://localhost:5000\n")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
