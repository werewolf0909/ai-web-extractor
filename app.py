
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
        headers={"Content-Disposition": "attachment; filename=extracted_data.csv"}
    )

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI Web Scraper & Extractor</title>
        <style>
            body { font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #f8fafc; color: #0f172a; }
            .card { background: white; padding: 24px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.06); margin-bottom: 20px; }
            label { display: block; font-weight: 600; margin-top: 14px; margin-bottom: 6px; }
            input { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 14px; }
            .row { display: flex; gap: 16px; }
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
            <h2>Universal AI Web Extractor</h2>
            <p>Enter your API access key, target URL, and fields to extract.</p>
            <label>App API Key (X-API-Key)</label>
            <input id="api_key" type="password" placeholder="Enter your APP_API_KEY" />
            <label>Target URL</label>
            <input id="url" placeholder="https://example.com" />
            <label>Fields to Extract (comma-separated)</label>
            <input id="fields" value="title, price, location, phone_number, email_address, summary" />
            <div class="row">
                <div>
                    <label>Crawl Sub-Links Containing (optional)</label>
                    <input id="link_filter" placeholder="Leave blank for single page, or enter e.g. /d/" />
                </div>
                <div>
                    <label>Max Pages to Crawl</label>
                    <input id="max_items" type="number" value="5" min="1" max="50" />
                </div>
            </div>
            <button onclick="runExtract()">Start Extraction</button>
            <button class="secondary" id="dlBtn" style="display:none;" onclick="downloadCSV()">Download CSV</button>
            <div id="status"></div>
        </div>
        <div id="tableContainer"></div>
        <script>
            let lastResults = [];
            async function runExtract() {
                const status = document.getElementById('status');
                const dlBtn = document.getElementById('dlBtn');
                status.innerText = "Running Playwright + AI extraction... please wait...";
                dlBtn.style.display = "none";
                const res = await fetch('/api/extract', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-API-Key': document.getElementById('api_key').value
                    },
                    body: JSON.stringify({
                        url: document.getElementById('url').value,
                        fields: document.getElementById('fields').value,
                        link_filter: document.getElementById('link_filter').value,
                        max_items: parseInt(document.getElementById('max_items').value)
                    })
                });
                const data = await res.json();
                if (!res.ok) { status.innerText = "Auth Error: " + (data.detail || "Unauthorized"); return; }
                if (data.error) { status.innerText = "Error: " + data.error; return; }
                lastResults = data.results || [];
                status.innerText = `Extracted ${lastResults.length} item(s).`;
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
                a.download = 'extracted_data.csv';
                a.click();
            }
        </script>
    </body>
    </html>
    """
