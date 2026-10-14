"""A /health endpoint so the bot can run as a free Render web service.

Render will not deploy a web service that never binds $PORT, and it spins a free one
down after 15 minutes without inbound HTTP. The bot's Telegram polling is all outbound,
so it never counts — an external cron hitting /health every ~10 minutes keeps it up.

Only started when PORT is set, which Render does and a local run does not.
"""

from __future__ import annotations

import threading
import json
import logging
import secrets
import time
from html import escape
from typing import Any
from urllib.parse import parse_qsl
import hashlib
import hmac

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import config
import replay
import httpx

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Telegram opens the editor shortly after a pitch. Keep just the replay inputs, keyed by
# an unguessable link token and bound to the Telegram user who requested the pitch.
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_SESSION_TTL = 6 * 60 * 60
log = logging.getLogger(__name__)


def register_assumptions(replay_path, user_id: int) -> str:
    saved = replay.load(replay_path)
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _sessions_lock:
        for key in list(_sessions):
            if now - _sessions[key]["created"] > _SESSION_TTL:
                del _sessions[key]
        while len(_sessions) >= 200:
            oldest = min(_sessions, key=lambda key: _sessions[key]["created"])
            del _sessions[oldest]
        _sessions[token] = {"created": now, "user_id": int(user_id), "replay": saved}
    return token


def _session(token: str) -> dict[str, Any]:
    with _sessions_lock:
        item = _sessions.get(token)
        if not item or time.time() - item["created"] > _SESSION_TTL:
            _sessions.pop(token, None)
            raise HTTPException(status_code=404, detail="This editor link has expired. Run a new pitch.")
        return item


def _valid_webapp_user(init_data: str) -> int | None:
    """Validate Telegram Web App initData with the bot token (Telegram's HMAC scheme)."""
    if not config.TELEGRAM_BOT_TOKEN or not init_data:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if time.time() - auth_date > 24 * 60 * 60 or auth_date > time.time() + 60:
        return None
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", config.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return None
    return user_id


def _editor_html(token: str, saved: dict) -> str:
    _, result, model = replay._revalue_source(saved)
    model = dict(model)
    model["wacc"] = result.valuation.wacc
    initial = json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    symbol = escape(str(saved.get("symbol", "")))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light"><title>{symbol} assumptions</title>
<style>
:root{{--bg:#f3f5f8;--card:#fff;--ink:#17202d;--muted:#637083;--line:#dce2ea;--accent:#1769e0;--ok:#146c43;--bad:#a12622}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:760px;margin:auto;padding:20px 14px 44px}}h1{{font-size:24px;margin:0 0 4px}}.sub,.hint{{color:var(--muted);font-size:14px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
details{{border-top:1px solid var(--line);padding:12px 0}}details:first-child{{border-top:0}}summary{{font-weight:650;cursor:pointer;padding:4px 0}}.field{{margin:12px 0}}label{{display:block;font-weight:600;margin-bottom:5px}}input,textarea{{display:block;width:100%;min-height:46px;border:1px solid #b8c3d1;border-radius:9px;background:white;color:var(--ink);font:inherit;padding:10px 12px}}textarea{{min-height:88px;font:14px/1.4 ui-monospace,monospace;resize:vertical}}.hint{{margin-top:4px}}
button{{width:100%;min-height:50px;border:0;border-radius:11px;background:var(--accent);color:#fff;font:inherit;font-weight:700;padding:12px;margin-top:10px}}button:disabled{{opacity:.6}}#result{{white-space:pre-wrap;overflow-wrap:anywhere}}.good{{color:var(--ok)}}.error{{color:var(--bad)}}.fixed{{font-size:13px;color:var(--muted)}}
</style></head><body><main>
<h1>{symbol} valuation assumptions</h1><div class="sub">Edit the forecast inputs, then calculate a comparison from the original pitch data.</div>
<div class="card"><div class="fixed">Market price, shares, cash, debt, FX and peer data stay fixed to the saved pitch. Revenue and segment amounts are in reporting-currency millions. Rates use decimals: 0.10 means 10%.</div></div>
<form id="form"><div id="fields" class="card"></div><button id="submit" type="submit">Calculate updated valuation</button></form>
<div id="output" class="card" hidden><strong id="heading">Result</strong><div id="result"></div></div>
<script>
const token={json.dumps(token)}, original={initial};
const fields=document.getElementById('fields');
const human=s=>String(s).replace(/([a-z0-9])([A-Z])/g,'$1 $2').replaceAll('_',' ').replace(/\\b\\w/g,c=>c.toUpperCase());
function scalar(parent,key,value,path){{
 const wrap=document.createElement('div');wrap.className='field';
 const label=document.createElement('label');label.textContent=key.toLowerCase()==='wacc'?'WACC (%)':human(key);wrap.append(label);
 if(Array.isArray(value)||value===null||typeof value==='object'){{
  const area=document.createElement('textarea');area.value=JSON.stringify(value,null,2);area.dataset.path=JSON.stringify(path);area.dataset.kind='json';area.setAttribute('aria-label',human(key)+' JSON');wrap.append(area);
  const hint=document.createElement('div');hint.className='hint';hint.textContent='Edit this list or structured value as JSON.';wrap.append(hint);
 }} else {{
  const input=document.createElement('input');input.dataset.path=JSON.stringify(path);input.dataset.kind=key.toLowerCase()==='wacc'?'percent':typeof value;
  if(typeof value==='number'){{input.type='number';input.step=key.toLowerCase()==='wacc'?'0.01':'any';input.inputMode='decimal';input.value=key.toLowerCase()==='wacc'?String(Number((value*100).toFixed(4))):String(value)}}
  else if(typeof value==='boolean'){{input.type='checkbox';input.checked=value}}
  else {{input.type='text';input.value=value??''}}
  wrap.append(input);
 }}
 parent.append(wrap);
}}
function walk(parent,obj,path=[]){{for(const [key,value] of Object.entries(obj)){{
 if(value&&typeof value==='object'&&!Array.isArray(value)){{const box=document.createElement('details');box.open=path.length<1;const title=document.createElement('summary');title.textContent=human(key);box.append(title);walk(box,value,[...path,key]);parent.append(box)}}
 else scalar(parent,key,value,[...path,key]);
}}}}
walk(fields,original);
function readModel(){{const model=JSON.parse(JSON.stringify(original));for(const el of fields.querySelectorAll('[data-path]')){{const path=JSON.parse(el.dataset.path);let v;
 if(el.dataset.kind==='json'){{try{{v=JSON.parse(el.value)}}catch{{throw new Error('Invalid JSON in '+human(path.at(-1)))}}}}
 else if(el.type==='checkbox')v=el.checked;else if(el.dataset.kind==='number')v=Number(el.value);else if(el.dataset.kind==='percent')v=Number(el.value)/100;else v=el.value;
 let at=model;for(const part of path.slice(0,-1))at=at[part];at[path.at(-1)]=v;
}}return model}}
document.getElementById('form').addEventListener('submit',async e=>{{e.preventDefault();const btn=document.getElementById('submit'),out=document.getElementById('output'),res=document.getElementById('result'),head=document.getElementById('heading');btn.disabled=true;btn.textContent='Calculating…';out.hidden=false;res.className='';res.textContent='';
 try{{const model=readModel();const response=await fetch('/api/assumptions/'+encodeURIComponent(token),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{init_data:window.Telegram?.WebApp?.initData||'',model}})}});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'Could not calculate this valuation.');head.textContent='Updated valuation';res.textContent=payload.result;res.className='good';window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred('success')}}
 catch(err){{head.textContent='Could not calculate';res.textContent=err.message;res.className='error'}}finally{{btn.disabled=false;btn.textContent='Calculate updated valuation'}}
}});
if(window.Telegram?.WebApp){{window.Telegram.WebApp.ready();window.Telegram.WebApp.expand()}}
</script></main></body></html>'''


@app.get("/assumptions/{token}", response_class=HTMLResponse)
def assumptions_editor(token: str):
    item = _session(token)
    return HTMLResponse(
        _editor_html(token, item["replay"]),
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )


@app.post("/api/assumptions/{token}")
async def calculate_assumptions(token: str, request: Request):
    item = _session(token)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Expected JSON input.") from exc
    init_data = str(body.get("init_data", "")) if isinstance(body, dict) else ""
    if init_data:
        user_id = _valid_webapp_user(init_data)
        if user_id is None or user_id != item["user_id"]:
            raise HTTPException(status_code=403, detail="Telegram could not verify this account. Reopen the editor from the bot button.")
    # The unguessable, short-lived URL token is a bearer capability. Some Telegram
    # clients open Web App URLs without exposing initData; the token still binds the
    # calculation to the original session and its Telegram recipient.
    model = body.get("model")
    if not isinstance(model, dict):
        raise HTTPException(status_code=400, detail="Model inputs must be an object.")
    try:
        result = replay.revalue_saved(item["replay"], {"model": model})
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": user_id, "text": result},
            )
            response.raise_for_status()
    except Exception:
        log.exception("could not send the assumptions comparison to Telegram")
    return {"result": result}


# HEAD as well as GET: some uptime pingers default to HEAD, and FastAPI answers HEAD on
# a GET-only route with 405, which the pinger reports as the service being down.
@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict:
    return {"status": "ok"}


def serve_in_background(port: int, host: str = "0.0.0.0") -> None:
    """Serve on a daemon thread alongside polling; it exits with the main thread.

    uvicorn skips installing signal handlers off the main thread, so SIGTERM from
    Render still reaches python-telegram-bot and shuts the bot down cleanly.
    """
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False,
    ))
    threading.Thread(target=server.run, name="health", daemon=True).start()
