import asyncio
import csv
import io
import json
import os
import re
import urllib.request
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Parallel & Paginated AI Web Extractor")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
APP_API_KEY = os.getenv("APP_API_KEY", "")

def verify_api_key(x_api_key: str = Header(default="")):
    if APP_API_KEY and x_api_key != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

class BatchExtractRequest(BaseModel):
    urls: list[str]
    fields: str = "title, price, location, phone_number, email_address, summary"
    link_filters: str = ""
    max_items_per_url: int = 5
    pages_per_url: int = 1
    concurrency: int = 3
    model: str = "openrouter/free"

def ask_openrouter_sync(page_text: str, fields: str, model: str) -> dict:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set.")
    endpoint = "https://" + "openrouter" + ".ai/api/v1/chat/completions"
    prompt = (
        f"Extract these fields from the webpage text below: {fields}.\n"
        "Convert spelled-out phone numbers (like 'two five zero') into digits. "
        "Return ONLY valid raw JSON (a single JSON object with those keys, no markdown or backticks).\n\n"
        f"Webpage text:\n{page_text[:5000]}"
    )
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE).strip()
        return json.loads(content)

async def extract_single_page(context, sem, link: str, seed_url: str, fields: str, model: str) -> dict:
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=35000)
            text = await page.inner_text("body")
            # Run blocking HTTP call in a thread so parallel tabs don't block each other
            extracted = await asyncio.to_thread(ask_openrouter_sync, text, fields, model)
            extracted["seed_url"] = seed_url
            extracted["source_url"] = link
            return extracted
        except Exception as e:
            return {"seed_url": seed_url, "source_url": link, "error": str(e)}
        finally:
            await page.close()

@app.post("/api/extract", dependencies=[Depends(verify_api_key)])
async def api_extract(req: BatchExtractRequest):
    filters = [f.strip() for f in req.link_filters.split(",") if f.strip()]
    sem = asyncio.Semaphore(max(1, min(req.concurrency, 5)))
    tasks = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        for raw_url in req.urls:
            target_url = raw_url.strip()
            if not target_url:
                continue
            if not target_url.startswith(("http://", "https://")):
                target_url = "https://" + target_url

            if not filters:
                tasks.append(extract_single_page(context, sem, target_url, target_url, req.fields, req.model))
                continue

            # Crawl pagination pages to gather matching sub-links
            nav_page = await context.new_page()
            current_url = target_url
            seen_links = set()
            collected_links = []

            try:
                for _ in range(max(1, min(req.pages_per_url, 5))):
                    if not current_url:
                        break
                    await nav_page.goto(current_url, wait_until="networkidle", timeout=45000)
                    raw_links = await nav_page.eval_on_selector_all(
                        "a",
                        "els => els.map(e => ({text: e.innerText.trim(), href: e.href, cls: e.className, rel: e.rel}))"
                    )
                    for item in raw_links:
                        if any(f in item["href"] for f in filters) and item["href"] not in seen_links and item["text"].lower() != "no image":
                            seen_links.add(item["href"])
                            collected_links.append(item["href"])
                        if len(collected_links) >= req.max_items_per_url:
                            break

                    if len(collected_links) >= req.max_items_per_url:
                        break

                    # Find Next Page link if pagination is enabled
                    next_link = next(
                        (
                            item["href"] for item in raw_links
                            if item["href"].startswith("http")
                            and item["href"] != current_url
                            and (
                                "next" in item["cls"].lower()
                                or "next" in item["rel"].lower()
                                or item["text"].lower() in ("next", "next >", "next »", ">")
                            )
                        ),
                        None
                    )
                    current_url = next_link
            except Exception as e:
                tasks.append(asyncio.sleep(0, result={"seed_url": target_url, "error": str(e)}))
            finally:
                await nav_page.close()

            for link in collected_links[:req.max_items_per_url]:
                tasks.append(extract_single_page(context, sem, link, target_url, req.fields, req.model))

        results = await asyncio.gather(*tasks) if tasks else []
        await browser.close()

    return {"count": len(results), "results": list(results)}

@app.post("/api/export-csv")
async def export_csv(payload: dict):
    rows = payload.get("results", [])
    if not rows:
        return {"error": "No data to export"}
    output = io.StringIO()
    fieldnames = list(dict.fromkeys(k for r in rows for k in r.keys()))
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=parallel_extracted.csv"}
    )

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Parallel & Paginated AI Web Extractor</title>
        <style>
            body { font-family: system-ui, sans-serif; max-width: 980px; margin: 40px auto; padding: 0 20px; background: #f8fafc; color: #0f172a; }
            .card { background: white; padding: 24px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 20px; }
            label { display: block; font-weight: 600; margin-top: 14px; margin-bottom: 6px; }
            input, textarea { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 14px; font-family: inherit; }
            textarea { height: 105px; resize: vertical; }
            .row { display: flex; gap: 14px; }
            .row > div { flex: 1; }
            button { background: #2563eb; color: white; border: none; padding: 12px 20px; border-radius: 6px; font-weight: 600; cursor: pointer; margin-top: 18px; font-size: 15px; }
            button:hover { background: #1d4ed8; }
            button.secondary { background: #059669; margin-left: 10px; }
            table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; background: white; }
            th, td { border: 1px solid #e2e8f0; padding: 10px; text-align: left; }
            th { background: #f1f5f9; }
            #status { margin-top: 12px; font-weight: 500; color: #475569; }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Parallel & Paginated AI Web Extractor</h2>
            <p>Crawl multiple sites/locations in parallel with automatic pagination and CSV export.</p>
            <label>App API Key (X-API-Key)</label>
            <input id="api_key" type="password" placeholder="Enter your APP_API_KEY (e.g. test1234)" />
            <label>Target URLs (One URL per line)</label>
            <textarea id="urls" placeholder="https://craigslist.org"></textarea>
            <label>Fields to Extract (comma-separated)</label>
            <input id="fields" value="title, price, location, phone_number, email_address, summary" />
            <div class="row">
                <div>
                    <label>Sub-Link Filters (optional)</label>
                    <input id="link_filters" placeholder="e.g. /d/, /product/" />
                </div>
                <div>
                    <label>Max Items / URL</label>
                    <input id="max_items" type="number" value="5" min="1" max="30" />
                </div>
                <div>
                    <label>Pages to Follow</label>
                    <input id="pages_per_url" type="number" value="2" min="1" max="5" />
                </div>
                <div>
                    <label>Parallel Tabs</label>
                    <input id="concurrency" type="number" value="3" min="1" max="5" />
                </div>
            </div>
            <button onclick="runExtract()">Start Parallel Extraction</button>
            <button class="secondary" id="dlBtn" style="display:none;" onclick="downloadCSV()">Download CSV</button>
            <div id="status"></div>
        </div>
        <div id="tableContainer"></div>
        <script>
            let lastResults = [];
            async function runExtract() {
                const status = document.getElementById('status');
                const dlBtn = document.getElementById('dlBtn');
                const urlList = document.getElementById('urls').value.split('\\n').map(u => u.trim()).filter(Boolean);
                if (!urlList.length) { status.innerText = "Please enter at least one URL."; return; }

                status.innerText = `Running parallel extraction across ${urlList.length} site(s)... please wait...`;
                dlBtn.style.display = "none";

                const res = await fetch('/api/extract', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': document.getElementById('api_key').value
                    },
                    body: JSON.stringify({
                        urls: urlList,
                        fields: document.getElementById('fields').value,
                        link_filters: document.getElementById('link_filters').value,
                        max_items_per_url: parseInt(document.getElementById('max_items').value),
                        pages_per_url: parseInt(document.getElementById('pages_per_url').value),
                        concurrency: parseInt(document.getElementById('concurrency').value)
                    })
                });
                const data = await res.json();
                if (!res.ok) { status.innerText = "Auth Error: " + (data.detail || "Unauthorized"); return; }
                lastResults = data.results || [];
                status.innerText = `Extracted ${lastResults.length} total item(s).`;
                if (lastResults.length > 0) { dlBtn.style.display = "inline-block"; renderTable(lastResults); }
            }
            function renderTable(rows) {
                const keys = [...new Set(rows.flatMap(Object.keys))];
                let html = '<table><thead><tr>' + keys.map(k => `<th>${k}</th>`).join('') + '</tr></thead><tbody>';
                for (const r of rows) { html += '<tr>' + keys.map(k => `<td>${r[k] ?? ''}</td>`).join('') + '</tr>'; }
                html += '</tbody></table>';
                document.getElementById('tableContainer').innerHTML = html;
            }
            async function downloadCSV() {
                const res = await fetch('/api/export-csv', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({results: lastResults})
                });
                const blob = await res.blob();
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = 'parallel_extracted.csv';
                a.click();
            }
        </script>
    </body>
    </html>
    """
