# MUST BE AT THE VERY TOP
from gevent import monkey
monkey.patch_all()

import os
import requests
import time
import json
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response, stream_with_context, jsonify
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "llms-txt-secret-2026")

# Initialize OpenAI Client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_page_content(url):
    """Fetches and cleans HTML content from a single page."""
    try:
        response = requests.get(url, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove non-text elements to save tokens
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()
            
        return soup.get_text(separator=' ', strip=True)[:6000]
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return ""

@app.route('/')
def index():
    show_key_input = os.getenv("OPENAI_API_KEY") is None
    requires_auth = os.getenv("APP_PASSWORD") is not None
    return render_template('index.html', show_key_input=show_key_input, requires_auth=requires_auth)

@app.route('/health')
def health_check():
    """Dedicated health check endpoint for Render to prevent deployment errors."""
    return "OK", 200

@app.route('/generate-stream', methods=['POST'])
def generate_stream():
    data = request.json
    target_url = data.get('url')
    user_key = data.get('api_key')
    
    if not os.getenv("OPENAI_API_KEY") and user_key:
        client.api_key = user_key

    # Note: Replace this list with your crawler's actual output of discovered URLs
    urls_to_process = [target_url] 
    batch_size = 40
    total_batches = (len(urls_to_process) + batch_size - 1) // batch_size

    def generate():
        start_time = time.time()
        intermediate_summaries = []
        
        for i in range(0, len(urls_to_process), batch_size):
            batch_num = (i // batch_size) + 1
            
            # Update Progress for the frontend progress bar
            elapsed = round(time.time() - start_time, 1)
            percent = int((batch_num / total_batches) * 100)
            
            yield f"data: {json.dumps({'type': 'progress', 'percent': percent, 'batch': batch_num, 'total': total_batches, 'elapsed': elapsed})}\n\n"

            # Process Batch
            batch = urls_to_process[i:i + batch_size]
            batch_text = ""
            for u in batch:
                content = get_page_content(u)
                if content:
                    batch_text += f"\n---\nSource: {u}\nContent: {content}"
            
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Summarize these documentation pages for an llms.txt file."},
                        {"role": "user", "content": batch_text}
                    ],
                    temperature=0.2
                )
                intermediate_summaries.append(response.choices[0].message.content)
                time.sleep(0.5) # Rate limit protection
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'msg': f'Batch {batch_num} failed: {str(e)}'})}\n\n"

        # Final Consolidation Step
        yield f"data: {json.dumps({'type': 'status', 'msg': 'Consolidating final llms.txt file...'})}\n\n"
        
        try:
            final_response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Create a structured llms.txt file using the provided batch summaries."},
                    {"role": "user", "content": "\n\n".join(intermediate_summaries)}
                ]
            )
            final_txt = final_response.choices[0].message.content
            yield f"data: {json.dumps({'type': 'final', 'content': final_txt, 'elapsed': round(time.time() - start_time, 1)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'msg': f'Final consolidation failed: {str(e)}'})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
