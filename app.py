import asyncio
import csv
import io
import json
import os
import re
import urllib.parse
import urllib.request
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Smart AI B2B Lead & Search Extractor")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
APP_API_KEY = os.getenv("APP_API_KEY", "")

SKIP_DOMAINS = ("duckduckgo.com", "bing.com", "microsoft.com", "google.com", "youtube.com", "facebook.com", "linkedin.com", "wikipedia.org", "amazon.", "ebay.", "reddit.com", "instagram.com", "x.com", "twitter.com")

def verify_api_key(x_api_key: str = Header(default="")):
    if APP_API_KEY and x_api_key != APP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

class SmartExtractRequest(BaseModel):
    search_query: str = ""
    max_search_results: int = 5
    urls: list[str] = []
    fields: str = "company_name, phone_number, general_or_sales_email, accounts_payable_or_purchasing, accounts_receivable_or_billing, location, pump_types_offered, b2b_fit_score, opportunity_notes"
    find_contact_pages: bool = True
    concurrency: int = 3
    model: str = "openrouter/free"

def extract_regex_contacts(text: str):
    emails = set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text))
    clean_emails = [e for e in emails if not e.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"))]
    phones = set(re.findall(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}", text))
    clean_phones = [p.strip() for p in phones if len(re.sub(r"\D", "", p)) >= 8]
    return ", ".join(clean_emails), ", ".join(list(clean_phones)[:4])

def ask_openrouter_sync(page_text: str, fields: str, model: str) -> dict:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set.")
    endpoint = "https://" + "openrouter" + ".ai/api/v1/chat/completions"
    prompt = (
        "You are a B2B industrial researcher analysing a company website for a slurry pump designer.\n"
        f"Extract these fields as flat JSON keys: {fields}.\n"
        "Rules:\n"
        "- Look closely for department emails or contacts such as accounts payable, purchasing, procurement, supplier onboarding, accounts receivable, billing, or sales.\n"
        "- If an accounts payable or receivable email is not explicitly listed, state 'Not listed (use general/sales contact)' rather than guessing.\n"
        "- For b2b_fit_score, rate High, Medium, or Low based on whether they manufacture, distribute, or service slurry/abrasive/mining pumps.\n"
        "- Return ONLY valid raw JSON (no markdown, no backticks).\n\n"
        f"Website text:\n{page_text[:6500]}"
    )
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", data["choices"][0]["message"]["content"].strip(), flags=re.IGNORECASE).strip()
        return json.loads(content)

def search_ddgs_sync(query: str, limit: int) -> list[str]:
    from ddgs import DDGS
    discovered, seen = [], set()
    for r in DDGS().text(query, max_results=limit * 3):
        href = r.get("href", "")
        if not href.startswith("http"):
            continue
        parsed = urllib.parse.urlparse(href)
        domain = parsed.netloc.lower().removeprefix("www.").removeprefix("www.")
        if any(s in domain for s in SKIP_DOMAINS) or domain in seen:
            continue
        seen.add(domain)
        discovered.append(f"{parsed.scheme}://{parsed.netloc}")
        if len(discovered) >= limit:
            break
    return discovered

async def discover_urls_from_search(context, query: str, limit: int) -> list[str]:
    if not query.strip():
        return []
    try:
        return await asyncio.to_thread(search_ddgs_sync, query.strip(), limit)
    except Exception:
        return []

async def _unused_old_search(context, query: str, limit: int) -> list[str]:
    if not query.strip():
        return []
    page = await context.new_page()
    discovered = []
    try:
        search_url = "https://" + "www.bing" + ".com/search?q=" + urllib.parse.quote_plus(query.strip())
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)
        hrefs = await page.eval_on_selector_all("li.b_algo h2 a", "els => els.map(e => e.href)")
        seen_domains = set()
        for href in hrefs:
            if "uddg=" in href:
                parsed_q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = parsed_q.get("uddg", [href])
            if not href.startswith("http"):
                continue
            parsed = urllib.parse.urlparse(href)
            domain = parsed.netloc.lower().removeprefix("www.").removeprefix("www.")
            if any(skip in domain for skip in SKIP_DOMAINS) or domain in seen_domains:
                continue
            seen_domains.add(domain)
            discovered.append(f"{parsed.scheme}://{parsed.netloc}")
            if len(discovered) >= limit:
                break
    except Exception:
        pass
    finally:
        await page.close()
    return discovered

async def extract_company(context, sem, link: str, fields: str, model: str, find_contact: bool) -> dict:
    async with sem:
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}", lambda route: route.abort())
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            combined_text = await page.inner_text("body")
            if any(w in combined_text.lower() for w in ("just a moment", "being verified", "checking your browser")):
                await page.wait_for_timeout(6000)
                combined_text = await page.inner_text("body")

            pages_checked = [link]
            if find_contact:
                domain = urllib.parse.urlparse(link).netloc
                anchors = await page.eval_on_selector_all("a", "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))")
                sub_targets = []
                for kw in (("contact", "enquir", "get-in-touch"), ("supplier", "about", "accounts", "procurement")):
                    match = next((a["href"] for a in anchors if a["href"].startswith("http") and urllib.parse.urlparse(a["href"]).netloc == domain and a["href"] not in pages_checked and any(k in (a["text"] + a["href"]).lower() for k in kw)), None)
                    if match:
                        sub_targets.append(match)
                        pages_checked.append(match)

                for sub in sub_targets[:2]:
                    try:
                        await page.goto(sub, wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(1200)
                        combined_text += f"\n\n--- SUBPAGE ({sub}) ---\n" + await page.inner_text("body")
                    except Exception:
                        pass

            r_emails, r_phones = extract_regex_contacts(combined_text)
            extracted = await asyncio.to_thread(ask_openrouter_sync, combined_text, fields, model)
            if r_emails:
                extracted["all_found_emails"] = r_emails
            if r_phones and (not extracted.get("phone_number") or extracted.get("phone_number") in ("None", "N/A")):
                extracted["phone_number"] = r_phones
            extracted["pages_scanned"] = ", ".join(pages_checked)
            extracted["source_url"] = link
            return extracted
        except Exception as e:
            return {"source_url": link, "error": str(e)}
        finally:
            await page.close()

@app.post("/api/extract", dependencies=[Depends(verify_api_key)])
async def api_extract(req: SmartExtractRequest):
    sem = asyncio.Semaphore(max(1, min(req.concurrency, 5)))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-http2"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

        found_urls = await discover_urls_from_search(context, req.search_query, max(1, min(req.max_search_results, 15)))
        manual_urls = [("https://" + u.strip() if not u.strip().startswith(("http://", "https://")) else u.strip()) for u in req.urls if u.strip()]
        all_targets = list(dict.fromkeys(found_urls + manual_urls))

        tasks = [extract_company(context, sem, url, req.fields, req.model, req.find_contact_pages) for url in all_targets]
        results = await asyncio.gather(*tasks) if tasks else []
        await browser.close()

    return {"discovered_count": len(found_urls), "count": len(results), "results": list(results)}

@app.post("/api/export-csv")
async def export_csv(payload: dict):
    rows = payload.get("results", [])
    if not rows:
        return {"error": "No data"}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(dict.fromkeys(k for r in rows for k in r.keys())))
    writer.writeheader()
    writer.writerows(rows)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=smart_b2b_pump_leads.csv"})

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><title>Smart AI B2B Lead & Search Extractor</title><style>body{font-family:sans-serif;max-width:1050px;margin:30px auto;padding:0 15px;background:#f8fafc;color:#0f172a}.card{background:#fff;padding:24px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.06)}label{display:block;font-weight:600;margin-top:14px}input[type=text],input[type=password],input[type=number],textarea{width:100%;padding:10px;margin-top:5px;border:1px solid #cbd5e1;border-radius:6px;box-sizing:border-box;font-size:14px}textarea{height:85px}.row{display:flex;gap:14px}.row>div{flex:1}button{background:#2563eb;color:#fff;border:none;padding:12px 20px;border-radius:6px;font-weight:600;cursor:pointer;margin-top:18px;font-size:15px}button.sec{background:#059669;margin-left:10px}table{width:100%;border-collapse:collapse;margin-top:18px;font-size:12px;background:#fff}th,td{border:1px solid #e2e8f0;padding:8px;text-align:left;vertical-align:top}th{background:#f1f5f9}</style></head><body><div class="card"><h2>Smart AI B2B Lead & Search Extractor</h2><p>Search automatically by keyword (e.g. <i>slurry pump distributors UK</i>) or paste URLs to extract Sales, Accounts Payable/Receivable, and B2B Fit Score.</p><label>App API Key (X-API-Key)</label><input id="api_key" type="password" placeholder="test1234"/><div class="row"><div style="flex:3"><label>Auto-Discover Companies via Search Query (optional)</label><input id="search_query" type="text" placeholder="e.g. slurry pump manufacturers UK"/></div><div><label>Max Search Results</label><input id="max_search" type="number" value="5" min="1" max="15"/></div></div><label>Or Paste Specific Target URLs (One per line, optional)</label><textarea id="urls" placeholder="https://northridgepumps.com&#10;https://atlanticpumps.co.uk"></textarea><label>AI Fields to Extract</label><input id="fields" type="text" value="company_name, phone_number, general_or_sales_email, accounts_payable_or_purchasing, accounts_receivable_or_billing, location, pump_types_offered, b2b_fit_score, opportunity_notes"/><div style="margin-top:14px"><input id="find_contact" type="checkbox" checked/> <b>Auto-scan Contact, About & Supplier pages for department emails</b></div><button onclick="runExtract()">Discover & Extract Smart Leads</button><button class="sec" id="dlBtn" style="display:none" onclick="downloadCSV()">Download Smart Leads CSV</button><div id="status" style="margin-top:12px;font-weight:600;color:#334155"></div></div><div id="tbl"></div><script>let last=[];async function runExtract(){const st=document.getElementById('status'),dl=document.getElementById('dlBtn'),q=document.getElementById('search_query').value.trim(),urls=document.getElementById('urls').value.split('\\n').map(u=>u.trim()).filter(Boolean);if(!q&&!urls.length){st.innerText='Enter a search query or at least one URL.';return;}st.innerText='Searching & scanning company pages with AI... please wait...';dl.style.display='none';const res=await fetch('/api/extract',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':document.getElementById('api_key').value},body:JSON.stringify({search_query:q,max_search_results:parseInt(document.getElementById('max_search').value),urls,fields:document.getElementById('fields').value,find_contact_pages:document.getElementById('find_contact').checked})});const data=await res.json();if(!res.ok){st.innerText='Auth Error: '+(data.detail||'Unauthorized');return;}last=data.results||[];st.innerText='Discovered '+data.discovered_count+' site(s) & extracted '+last.length+' lead(s).';if(last.length){dl.style.display='inline-block';const keys=[...new Set(last.flatMap(Object.keys))];document.getElementById('tbl').innerHTML='<table><thead><tr>'+keys.map(k=>'<th>'+k+'</th>').join('')+'</tr></thead><tbody>'+last.map(r=>'<tr>'+keys.map(k=>'<td>'+(r[k]??'')+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}}async function downloadCSV(){const res=await fetch('/api/export-csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({results:last})});const a=document.createElement('a');a.href=URL.createObjectURL(await res.blob());a.download='smart_b2b_pump_leads.csv';a.click();}</script></body></html>"""
