import csv
import re
import json
import time
import os
import requests
import io
import threading
import queue
from flask import Flask, request, render_template_string, send_file, jsonify, Response, stream_with_context, session
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "llms-txt-secret-2024")
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

# ── Config from environment ──
APP_PASSWORD    = os.environ.get("APP_PASSWORD", "")          # If set, require login
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")        # Can also be supplied per-request
MODEL           = "gpt-4o-mini"
HEADERS         = {"User-Agent": "Mozilla/5.0 (compatible; llms-txt-generator/1.0)"}
jobs            = {}

# ─────────────────────────────────────────────────────
# OPENAI CALL  (replaces ollama)
# ─────────────────────────────────────────────────────
def call_llm(prompt, api_key):
    import logging
    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=600,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI API error: {e}")
        raise

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

def dedupe_urls(urls):
    seen = set()
    out  = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

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
# PRODUCT / GROUP DETECTION  (site-agnostic)
# ─────────────────────────────────────────────────────
def find_divergence_depth(urls):
    parsed_paths = []
    for url in urls:
        parts = [p for p in urlparse(url).path.strip('/').split('/') if p]
        parsed_paths.append(parts)

    if not parsed_paths:
        return 1

    min_depth = min(len(p) for p in parsed_paths)
    if min_depth == 0:
        return 1

    common_depth = 0
    for depth in range(min_depth):
        vals = set(p[depth] for p in parsed_paths if len(p) > depth)
        if len(vals) == 1:
            common_depth = depth + 1
        else:
            break

    # Always go at least one level deeper than the common prefix
    # so we group by the actual product/section, not the shared parent
    return max(common_depth + 1, 1)

def build_product_map(urls):
    if not urls:
        return {}

    domain_groups = defaultdict(list)
    for url in urls:
        domain_groups[urlparse(url).netloc].append(url)

    product_map = {}

    for domain, domain_urls in domain_groups.items():
        depth = find_divergence_depth(domain_urls)

        groups = defaultdict(list)
        for url in domain_urls:
            parsed = urlparse(url)
            parts  = [p for p in parsed.path.strip('/').split('/') if p]
            key    = '/'.join(parts[:depth]) if len(parts) >= depth else '/'.join(parts)
            groups[key].append(url)

        scheme = urlparse(domain_urls[0]).scheme

        for key, group_urls in groups.items():
            base_url = f"{scheme}://{domain}/{key}/" if key else f"{scheme}://{domain}/"
            name     = fetch_group_name(base_url, key, domain)
            for url in group_urls:
                parsed = urlparse(url)
                parts  = [p for p in parsed.path.strip('/').split('/') if p]
                prefix = f"{parsed.scheme}://{parsed.netloc}/{'/'.join(parts[:-1])}/" if parts else base_url
                product_map[prefix] = name

    return product_map

def fetch_group_name(base_url, path_key, domain):
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            html = r.text
            m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if m:
                title = m.group(1).strip()
                title = re.split(r'\s*[|\-\u2013\u2014]\s*', title)[0].strip()
                title = re.sub(r'&amp;', '&', title)
                title = re.sub(r'&#\d+;', '', title).strip()
                if title and 2 < len(title) < 80:
                    return title
            h = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.IGNORECASE | re.DOTALL)
            if h:
                h1 = re.sub(r'<[^>]+>', '', h.group(1)).strip()
                if h1 and 2 < len(h1) < 80:
                    return h1
    except:
        pass
    slug = path_key.split('/')[-1] if path_key else domain
    return slug.replace('-', ' ').replace('_', ' ').title() if slug else domain

def detect_product(url, product_map):
    parsed = urlparse(url)
    parts  = [p for p in parsed.path.strip('/').split('/') if p]
    prefix = f"{parsed.scheme}://{parsed.netloc}/{'/'.join(parts[:-1])}/" if parts else \
             f"{parsed.scheme}://{parsed.netloc}/"
    return product_map.get(prefix, urlparse(url).netloc)

# ─────────────────────────────────────────────────────
# PAGE FETCHING
# ─────────────────────────────────────────────────────
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

    for id_val in ["content", "main", "main-content", "page-content", "article", "primary", "wrapper", "body-content"]:
        pat = re.compile(r'<(?:div|section)[^>]+id="' + id_val + r'"[^>]*>(.*?)</(?:div|section)>', re.DOTALL | re.IGNORECASE)
        m = pat.search(html)
        if m and len(m.group(1)) > 200:
            return m.group(1)

    for cls_val in ["content", "main", "article", "entry-content", "post-content", "page-body", "main-body"]:
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
        html = r.text

        for tag in ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "svg", "form"]:
            html = re.sub(r"<" + tag + r"[^>]*>.*?</" + tag + r">", " ", html, flags=re.DOTALL | re.IGNORECASE)

        noise_ids_classes = [
            "sidebar", "widget", "banner", "promo", "advertisement",
            "breadcrumb", "pagination", "related", "social", "share",
            "cookie", "popup", "modal", "overlay", "newsletter",
            "contact-bar", "sticky", "floating", "phone-bar",
        ]
        for noise in noise_ids_classes:
            pat = re.compile(
                r'<(?:div|section|ul|span)[^>]+(?:class|id)="[^"]*' + noise + r'[^"]*"[^>]*>.*?</(?:div|section|ul|span)>',
                re.DOTALL | re.IGNORECASE
            )
            html = pat.sub(" ", html)

        main_html = extract_main_content(html)
        text = re.sub(r"<[^>]+>", " ", main_html)
        text = re.sub(r"\+\d[\d\s\-(). ]{7,20}", " ", text)
        text = re.sub(r"\b[A-Fa-f0-9]{40,}\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text[:4000] if len(text) > 100 else None
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
  Ask yourself: what would make an AI crawler choose THIS page over 10 similar ones?
  Lead with that. It could be: a specific audience, a unique capability, a named tool, a version range, a concrete outcome.
- Weave the audience in naturally: "IT administrators can...", "End users who need to...", "Developers integrating..."
- Maximum 3 sentences. Every sentence must add new information — no repetition.
- No comma-separated lists of more than 3 items.
- No filler openers: never start with "This page", "The page", "A guide", "A list", "Users to", "Covers"
- No nav links, sidebar content, phone numbers, or footer content.
- Active voice, present tense.

JSON FORMAT:
- Double quotes only, no apostrophes in values
- No trailing commas, no line breaks inside values
- Return raw JSON only — no markdown, no explanation

Return exactly:
{{"title": "...", "description": "..."}}

URL: {url}
Page content:
{content}"""

RESCORE_PROMPT = """You are evaluating an llms.txt description. These are read by AI crawlers — the goal is precision and distinctiveness, not marketing copy.

Score the description from 1 to 5:
1 = Generic filler, comma-list dump, or starts with "This page / The page / A guide"
2 = Vague — could describe any page in this category
3 = Adequate — accurate but lacks the most distinctive fact
4 = Good — specific, accurate, audience-aware
5 = Excellent — leads with the most distinctive fact, audience clear, zero padding

IF score <= 2: rewrite following these rules:
- First sentence must lead with the single most distinctive, specific fact about this URL
- Weave audience in naturally ("IT administrators can...", "End users who need to...")
- Max 3 sentences, no comma lists of more than 3 items
- No filler openers, no nav/sidebar content
- Active voice, present tense

IF score >= 3: return description unchanged.

URL: {url}
Page content (first 1500 chars): {content}
Current description: {description}

Return ONLY JSON: {{"score": <1-5>, "description": "<final description>"}}
No markdown, no explanation."""

def summarize(url, content, api_key, progress_q=None):
    snippet = content[:3500].strip()
    if not snippet:
        return None
    last_error = None
    for attempt in range(3):
        try:
            raw    = call_llm(SUMMARIZE_PROMPT.format(url=url, content=snippet), api_key)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw.strip())
            if "title" in result and "description" in result:
                return result
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(0.5)
            else:
                if progress_q:
                    progress_q.put({"type": "stage", "msg": f"API error: {last_error[:120]}", "pct": 0})
    return None

def rescore_and_fix(url, content, description, api_key):
    snippet = content[:1500].strip()
    try:
        raw = call_llm(RESCORE_PROMPT.format(url=url, content=snippet, description=description), api_key)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        score  = int(result.get("score", 3))
        desc   = result.get("description", description).strip()
        return score, desc if desc else description
    except:
        return 3, description

# ─────────────────────────────────────────────────────
# QUALITY CHECKING
# ─────────────────────────────────────────────────────
FILLER_STARTERS = [
    "this page ", "the page ", "this guide ", "this tool ", "this api ",
    "this document ", "this article ", "this section ",
    "a quick overview", "an overview of", "a guide to", "a guide for",
    "a list of", "a collection of", "detailed information",
    "users to ", "users the ", "covers ",
]

def strip_filler_opener(desc):
    d     = desc.strip()
    lower = d.lower()
    for starter in FILLER_STARTERS:
        if lower.startswith(starter):
            remainder = d[len(starter):].strip()
            if remainder:
                return remainder[0].upper() + remainder[1:]
    return d

def score_description(desc):
    issues = []
    d      = desc.strip()
    words  = d.split()
    lower  = d.lower()

    for starter in FILLER_STARTERS:
        if lower.startswith(starter):
            issues.append("filler_opener")
            break

    if len(d) < 60:
        issues.append("too_short")

    if re.match(r'^(to |for |by )', lower):
        issues.append("fragment")

    if len(words) > 65:
        issues.append("too_long")

    sentences = [s.strip() for s in d.split('.') if s.strip()]
    for sent in sentences:
        if sent.count(',') >= 4:
            issues.append("comma_list")
            break

    if lower.rstrip().endswith('and more') or lower.rstrip().endswith('and more.'):
        issues.append("and_more_ending")

    if "filler_opener" in issues or "too_short" in issues or "fragment" in issues:
        score = 1
    elif "too_long" in issues or "comma_list" in issues or "and_more_ending" in issues:
        score = 2
    else:
        score = 4

    return score, issues

DIFFERENTIATE_PROMPT = """Two pages on the same website have descriptions that are too similar.
An AI crawler cannot tell them apart. Rewrite the description for Page B so it focuses ONLY
on what is unique to Page B — what Page A does NOT cover.

Do not mention Page A. Just make Page B description distinctive on its own.
Max 3 sentences. Lead with the most distinctive fact about Page B.
Active voice, present tense. No filler openers.

Page A URL: {url_a}
Page A description: {desc_a}

Page B URL: {url_b}
Page B content: {content_b}
Page B current description: {desc_b}

Return ONLY JSON: {{"description": "<rewritten description for Page B>"}}
No markdown, no explanation."""

def description_similarity(a, b):
    """Word overlap ratio — site-agnostic. Returns 0.0 to 1.0."""
    words_a = set(re.findall(r'\b\w{4,}\b', a.lower()))
    words_b = set(re.findall(r'\b\w{4,}\b', b.lower()))
    if not words_a or not words_b:
        return 0.0
    overlap = len(words_a & words_b)
    return overlap / min(len(words_a), len(words_b))

def fix_sibling_duplicates(summaries, page_map, api_key, progress_q=None):
    """
    Phase 3 — Sibling dedup:
    Find pairs on the same domain with >70% description similarity.
    Re-summarize the second with explicit differentiation instruction.
    Zero hardcoding — works for any site.
    """
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 3 — Sibling differentiation", "pct": 94})

    domain_groups = defaultdict(list)
    for item in summaries:
        domain = urlparse(item["url"]).netloc
        domain_groups[domain].append(item)

    fixed = 0
    for domain, items in domain_groups.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a = items[i]
                b = items[j]
                sim = description_similarity(a["description"], b["description"])
                if sim >= 0.70:
                    content_b = page_map.get(b["url"], "")
                    if not content_b:
                        continue
                    try:
                        raw = call_llm(DIFFERENTIATE_PROMPT.format(
                            url_a=a["url"], desc_a=a["description"],
                            url_b=b["url"], content_b=content_b[:2000],
                            desc_b=b["description"]
                        ), api_key)
                        if raw.startswith("```"):
                            raw = raw.split("```")[1]
                            if raw.startswith("json"):
                                raw = raw[4:]
                        result  = json.loads(raw.strip())
                        new_desc = result.get("description", "").strip()
                        if new_desc and len(new_desc) > 60:
                            b["description"] = new_desc
                            fixed += 1
                    except:
                        pass

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": fixed, "dups": 0})

    return summaries


def fix_quality(summaries, page_map, api_key, progress_q=None):
    total = len(summaries)

    # Phase 1: structural fixes
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 1 — Structural fixes", "pct": 86})

    stripped = 0
    for item in summaries:
        original = item["description"]
        fixed    = strip_filler_opener(original)
        if fixed != original:
            item["description"] = fixed
            stripped += 1

    title_count = Counter(item["title"] for item in summaries)
    dup_fixed   = 0
    for item in summaries:
        if title_count[item["title"]] > 1:
            slug = item["url"].rstrip("/").split("/")[-1]
            slug = re.sub(r'\.html?$', '', slug)
            slug = slug.replace("-", " ").replace("_", " ").title()
            if slug:
                item["title"] = slug
                dup_fixed    += 1

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": stripped, "dups": dup_fixed})

    # Phase 2: LLM rescore — only for low-quality entries
    if progress_q:
        progress_q.put({"type": "stage", "msg": "QA Phase 2 — LLM scoring & rewrite", "pct": 90})

    rescore_fixed = 0
    for i, item in enumerate(summaries):
        score, issues = score_description(item["description"])
        if score <= 2:
            content = page_map.get(item["url"], "")
            if content:
                new_score, new_desc = rescore_and_fix(item["url"], content, item["description"], api_key)
                if new_desc != item["description"]:
                    item["description"] = new_desc
                    rescore_fixed      += 1
            if progress_q and i % 5 == 0:
                progress_q.put({"type": "qa_rescore", "current": i + 1, "total": total})

    if progress_q:
        progress_q.put({"type": "qa_result", "fixed": rescore_fixed, "dups": 0})

    # ── Phase 3: Sibling deduplication ──
    summaries = fix_sibling_duplicates(summaries, page_map, api_key, progress_q)

    return summaries

# ─────────────────────────────────────────────────────
# GENERATE llms.txt
# ─────────────────────────────────────────────────────
def generate_llms_txt(summaries, product_map):
    # Deduplicate by URL
    seen_urls = set()
    deduped   = []
    for item in summaries:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)

    # Flat list — no header block, no category sections
    lines = []
    for item in deduped:
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

    .api-key-row {
      display: flex; gap: 8px; margin-bottom: 20px; align-items: flex-start;
    }
    .api-key-row input {
      flex: 1; padding: 11px 14px; border: 1.5px solid #e0e0e0;
      border-radius: 10px; font-size: 13px; color: #333; outline: none;
      font-family: monospace; transition: border-color 0.2s;
    }
    .api-key-row input:focus { border-color: #6c47ff; }
    .api-key-note { font-size: 11px; color: #aaa; margin-top: 4px; }

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

    /* Login page */
    .login-card {
      background: #fff; border-radius: 20px;
      box-shadow: 0 4px 32px rgba(0,0,0,0.09);
      padding: 48px; width: 100%; max-width: 380px;
      text-align: center;
    }
    .login-card h2 { font-size: 20px; color: #1a1a2e; margin-bottom: 8px; }
    .login-card p  { color: #777; font-size: 13px; margin-bottom: 24px; }
    .login-input {
      width: 100%; padding: 12px 14px; border: 1.5px solid #e0e0e0;
      border-radius: 10px; font-size: 14px; outline: none; margin-bottom: 12px;
      transition: border-color 0.2s; text-align: center; letter-spacing: 2px;
    }
    .login-input:focus { border-color: #6c47ff; }
    .login-btn {
      width: 100%; padding: 13px; background: #6c47ff; color: #fff;
      border: none; border-radius: 10px; font-size: 14px; font-weight: 700;
      cursor: pointer; transition: background 0.2s;
    }
    .login-btn:hover { background: #5735e0; }
    .login-error { color: #dc2626; font-size: 12px; margin-top: 8px; }
  </style>
</head>
<body>

{% if needs_login %}
<div class="login-card">
  <div style="font-size:32px;margin-bottom:12px">🔐</div>
  <h2>llms<span style="color:#6c47ff">.txt</span> Generator</h2>
  <p>Enter the app password to continue</p>
  <form method="POST" action="/login">
    <input class="login-input" type="password" name="password" placeholder="Password" autofocus>
    {% if login_error %}<div class="login-error">Incorrect password</div>{% endif %}
    <br><br>
    <button class="login-btn" type="submit">Unlock</button>
  </form>
</div>

{% else %}
<div class="card">
  <div class="logo">llms<span>.txt</span> Generator</div>
  <div class="subtitle">Upload a CSV or sitemap → get a production-ready llms.txt instantly</div>
  <div class="badge">⚡ GPT-4o-mini · Works on any website · Zero setup</div>

  <form id="genForm">

    {% if not server_has_key %}
    <label>OpenAI API Key</label>
    <div class="api-key-row">
      <div style="flex:1">
        <input type="password" id="api_key" placeholder="sk-..." autocomplete="off">
        <div class="api-key-note">Your key is sent directly to OpenAI and never stored</div>
      </div>
    </div>
    {% endif %}

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
        <div class="upload-hint">One URL per row · products auto-detected</div>
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
        <div class="upload-hint">Local file · sitemap index supported</div>
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
{% endif %}

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

    // Validate inputs
    const apiKeyInput = document.getElementById('api_key')
    const apiKey = apiKeyInput ? apiKeyInput.value.trim() : ''
    if (apiKeyInput && !apiKey) { showError('Please enter your OpenAI API key'); return }

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

    btn.disabled    = true
    btn.textContent = 'Processing...'
    panel.style.display = 'block'
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' })

    setStage('fetch')
    setProgress('Loading...', 2)
    addLog('Pipeline started', 'stage')

    const formData = new FormData()
    formData.set('input_mode', activeTab)
    if (apiKey) formData.set('api_key', apiKey)
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
        setProgress('Summarising with GPT-4o-mini', Math.round((d.current/d.total)*46)+33, `${d.current} / ${d.total}`, d.url)
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
# AUTH
# ─────────────────────────────────────────────────────
def is_authenticated():
    if not APP_PASSWORD:
        return True
    return session.get("authenticated") is True

@app.route("/login", methods=["GET", "POST"])
def login():
    error = False
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return jsonify({"ok": True}) if request.is_json else \
                   __import__('flask').redirect("/")
        error = True
    return render_template_string(HTML, needs_login=True, login_error=error, server_has_key=bool(OPENAI_API_KEY))

@app.route("/logout")
def logout():
    session.clear()
    return __import__('flask').redirect("/")

# ─────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────
@app.route("/")
def index():
    if not is_authenticated():
        return render_template_string(HTML, needs_login=True, login_error=False, server_has_key=bool(OPENAI_API_KEY))
    return render_template_string(HTML, needs_login=False, login_error=False, server_has_key=bool(OPENAI_API_KEY))

@app.route("/start", methods=["POST"])
def start():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    # Resolve API key: form field > env var
    api_key = request.form.get("api_key", "").strip() or OPENAI_API_KEY
    if not api_key:
        return jsonify({"error": "No OpenAI API key provided"}), 400

    input_mode = request.form.get("input_mode", "csv")
    urls_raw   = []

    if input_mode == "csv":
        if "csv_file" not in request.files:
            return jsonify({"error": "No CSV file uploaded"}), 400
        file   = request.files["csv_file"]
        stream = io.StringIO(file.stream.read().decode("utf-8"))
        reader = csv.reader(stream)
        urls_raw = [cell.strip() for row in reader for cell in row if cell.strip().startswith("http")]

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

    urls_cleaned = [clean_url(u) for u in urls_raw]
    seen         = set()
    urls         = [u for u in urls_cleaned if not (u in seen or seen.add(u))]

    if not urls:
        return jsonify({"error": "No valid URLs found"}), 400

    job_id       = str(int(time.time() * 1000))
    q            = queue.Queue()
    jobs[job_id] = {"queue": q, "result": None, "done": False}

    def run_pipeline():
        try:
            total = len(urls)
            q.put({"type": "stage", "msg": f"{total} unique URLs loaded", "pct": 2})

            # Product detection
            q.put({"type": "stage", "msg": "Detecting products from URLs...", "pct": 3})
            product_map    = build_product_map(urls)
            if not product_map:
                parsed      = urlparse(urls[0])
                base_url    = f"{parsed.scheme}://{parsed.netloc}/"
                product_map = {base_url: parsed.netloc}
            products_found = list(dict.fromkeys(product_map.values()))
            q.put({"type": "stage", "msg": f"Products: {', '.join(products_found)}", "pct": 4})

            # Fetch
            q.put({"type": "stage", "msg": "Web Fetching", "pct": 5})
            pages = []
            for i, url in enumerate(urls, 1):
                content = fetch_page(url)
                ok      = content is not None
                if ok:
                    pages.append({"url": url, "content": content})
                q.put({"type": "fetch", "current": i, "total": total, "url": url, "ok": ok})
                time.sleep(0.1)

            if not pages:
                q.put({"type": "error", "msg": "Could not fetch any pages"})
                return

            page_map = {p["url"]: p["content"] for p in pages}
            fetched  = len(pages)

            # Summarize
            q.put({"type": "stage", "msg": "Summarising with GPT-4o-mini", "pct": 33})
            summaries    = []
            failed_pages = []
            for i, page in enumerate(pages, 1):
                result = summarize(page["url"], page["content"], api_key, q)
                ok     = result is not None
                if ok:
                    summaries.append({
                        "url"        : page["url"],
                        "title"      : result.get("title", ""),
                        "description": result.get("description", ""),
                        "product"    : detect_product(page["url"], product_map)
                    })
                else:
                    failed_pages.append(page)
                q.put({"type": "summarize", "current": i, "total": fetched, "url": page["url"], "ok": ok})

            if failed_pages:
                q.put({"type": "stage", "msg": f"Retrying {len(failed_pages)} failed pages", "pct": 80})
                for page in failed_pages:
                    result = summarize(page["url"], page["content"], api_key, q)
                    if result:
                        summaries.append({
                            "url"        : page["url"],
                            "title"      : result.get("title", ""),
                            "description": result.get("description", ""),
                            "product"    : detect_product(page["url"], product_map)
                        })

            if not summaries:
                q.put({"type": "error", "msg": "Could not summarize any pages"})
                return

            # QA
            q.put({"type": "stage", "msg": "Quality Assurance & Auto-fix", "pct": 85})
            summaries = fix_quality(summaries, page_map, api_key, q)

            # Generate
            q.put({"type": "stage", "msg": "Generating llms.txt", "pct": 97})
            output = generate_llms_txt(summaries, product_map)
            jobs[job_id]["result"] = output.encode("utf-8")
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
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ llms.txt Generator running on port {port}")
    print(f"   APP_PASSWORD set: {'yes' if APP_PASSWORD else 'no (open access)'}")
    print(f"   OPENAI_API_KEY set: {'yes' if OPENAI_API_KEY else 'no (users must provide key)'}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
