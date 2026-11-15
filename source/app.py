"""FastAPI app: serves the web UI and a small JSON API over the engine."""
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SCENARIOS = ROOT / "scenarios"
GROUPS = ("public", "edge")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

app = FastAPI(title="StackSense")
app.mount("/static", StaticFiles(directory=WEB), name="static")


def _unavailable(what):
    return JSONResponse({"error": f"{what} is not available"}, status_code=503)


def _read_sample(path):
    """Parsed JSON when valid, otherwise the raw text (some edge samples are broken on purpose)."""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/schema")
def schema():
    try:
        from stacksense.schema import schema_for_ui
    except ImportError:
        return _unavailable("schema")
    return schema_for_ui()


@app.get("/api/samples")
def samples():
    items = []
    for group in GROUPS:
        for path in sorted((SCENARIOS / group).glob("*.json")):
            data = _read_sample(path)
            label = ""
            if isinstance(data, dict):
                name = (data.get("project") or {}).get("name") if isinstance(data.get("project"), dict) else None
                label = name or ""
            items.append({"id": f"{group}/{path.stem}", "group": group,
                          "name": path.stem, "label": label})
    return items


@app.get("/api/samples/{group}/{name}")
def sample(group: str, name: str):
    if group not in GROUPS or not NAME_RE.match(name):
        return JSONResponse({"error": "unknown sample"}, status_code=404)
    path = SCENARIOS / group / f"{name}.json"
    if not path.is_file():
        return JSONResponse({"error": "unknown sample"}, status_code=404)
    return JSONResponse({"id": f"{group}/{name}", "scenario": _read_sample(path)})


async def _body(request: Request):
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")


@app.post("/api/recommend")
async def recommend(request: Request):
    try:
        from stacksense.engine import run
    except ImportError:
        return _unavailable("engine")
    payload = await _body(request)
    llm = os.environ.get("STACKSENSE_LLM", "off")
    return JSONResponse(run(payload, llm=llm))


@app.post("/api/describe")
async def describe(request: Request):
    try:
        from stacksense.describe import suggest
    except ImportError:
        return _unavailable("describe")
    payload = await _body(request)
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    if not isinstance(text, str):
        text = ""
    return {"suggestions": suggest(text)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
