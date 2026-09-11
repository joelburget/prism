"""Local model-blind review server, with review recorded before explicit reveal."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from urllib.parse import parse_qs, urlsplit

try:
    from .results import ResultStore, anonymous_id
except ImportError:
    from results import ResultStore, anonymous_id


PAGE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prism rollout review</title>
<style nonce="NONCE_VALUE">
body{font:16px system-ui,sans-serif;background:#f6f8fa;color:#17212b;max-width:1150px;margin:2rem auto;padding:0 1rem}
button,select,input,textarea{font:inherit;padding:.5rem;margin:.3rem 0;border:1px solid #a8b3bd;border-radius:5px}
button{cursor:pointer;background:#fff}button:disabled{cursor:default;opacity:.55}
header,nav,.row{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}header{justify-content:space-between}
pre{background:#fff;padding:1rem;border:1px solid #d1d9e0;overflow:auto;font:13px/1.55 ui-monospace,monospace;tab-size:4}
label{display:block;margin:.7rem 0}textarea{display:block;width:95%;min-height:5rem}input[type=text]{width:min(28rem,90%)}
fieldset{border:1px solid #cbd5df;border-radius:5px;margin:1rem 0}#status{white-space:pre-wrap;color:#9b2c2c}
.muted{color:#596773}#reveal{border:2px solid #7557a7}#findings>div{padding:.7rem;border-bottom:1px solid #ddd}
</style>
<header><h1>Prism rollout review</h1><span class="muted">Model blind · Language visible</span></header>
<p>Review the frozen source change. Record your findings and comprehension answer before explicitly revealing model identity or correctness results.</p>
<nav><label>Run <select id="runs" aria-label="Run"></select></label><button id="load">Open run</button><span id="progress"></span></nav>
<p id="status" role="status"></p>
<main hidden id="main"><h2 id="title"></h2>
<div class="row"><span id="timer">Active review: 0 s</span><button id="pause">Pause timer</button><span class="muted">Timer pauses while this tab is hidden.</span></div>
<label>Review context <select id="viewer"><option value="diff">Source diff</option></select></label>
<pre id="diff" aria-label="Source diff"></pre>
<form id="form"><fieldset id="fields"><legend>Independent human review</legend>
<label>Reviewer <input id="reviewer" type="text" required maxlength="200"></label>
<label>Decision <select id="decision"><option value="accept">Accept</option><option value="request_changes">Request changes</option></select></label>
<h3>Concrete findings</h3><p class="muted">Give a file/line or symbol location and explain the defect. An empty list is valid for accept.</p>
<div id="findings"></div><button type="button" id="add">Add finding</button>
<label id="question" for="answer"></label><textarea id="answer" required></textarea>
<label>Confidence <select id="confidence"><option value="1">1 — Very low</option><option value="2">2 — Low</option><option value="3" selected>3 — Moderate</option><option value="4">4 — High</option><option value="5">5 — Very high</option></select></label>
<button type="submit">Record review</button><p class="muted">The saved review is immutable. Reveal is a separate action.</p>
</fieldset></form>
<button hidden id="reveal">Reveal model identity and correctness</button><pre hidden id="revealed"></pre>
</main>
<script nonce="NONCE_VALUE">
'use strict';
const token='TOKEN_VALUE';
history.replaceState(null,'',location.pathname);
const $=id=>document.getElementById(id);
let current=null, elapsed=0, started=null, paused=false, reviewed=false, sourceDiff='';
function tick(){if(started!==null){elapsed+=(performance.now()-started)/1000;started=performance.now();}$('timer').textContent='Active review: '+Math.round(elapsed)+' s';}
function syncTimer(){tick();started=current&&!reviewed&&!paused&&!document.hidden?performance.now():null;$('pause').textContent=paused?'Resume timer':'Pause timer';}
document.addEventListener('visibilitychange',syncTimer);
setInterval(tick,1000);
async function api(path,body){const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Review-Token':token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'Request failed');return data;}
function fail(error){$('status').textContent=error.message;}
function finding(){const row=document.createElement('div');const location=document.createElement('input');location.type='text';location.placeholder='File:line or symbol';location.required=true;location.setAttribute('aria-label','Finding location');const severity=document.createElement('select');severity.setAttribute('aria-label','Finding severity');for(const value of ['critical','high','medium','low']){const option=document.createElement('option');option.value=value;option.textContent=value;severity.append(option);}severity.value='medium';const text=document.createElement('textarea');text.placeholder='Concrete defect and its effect';text.required=true;text.setAttribute('aria-label','Finding text');const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.onclick=()=>row.remove();row.append(location,severity,text,remove);$('findings').append(row);}
async function load(){if(current&&!reviewed&&elapsed>0&&!confirm('Discard this unsaved review and open another run?'))return;const data=await api('/api/run/'+encodeURIComponent($('runs').value));const context=await api('/api/context/'+encodeURIComponent(data.run_id));current=data.run_id;elapsed=0;started=null;paused=false;reviewed=data.reviewed;sourceDiff=data.source_diff;$('main').hidden=false;$('title').textContent=data.run_id+' · '+data.task+' · '+data.language;$('diff').textContent=data.source_diff;$('viewer').replaceChildren();const options=[{value:'diff',label:'Source diff'}];if(context.has_spec)options.push({value:JSON.stringify({view:'spec'}),label:'Task specification'});for(const file of context.files)options.push({value:JSON.stringify(file),label:file.view+' · '+file.file});for(const item of options){const option=document.createElement('option');option.value=item.value;option.textContent=item.label;$('viewer').append(option);}$('question').textContent=data.comprehension_prompt;$('findings').replaceChildren();$('answer').value='';$('decision').value='accept';$('confidence').value='3';$('fields').disabled=reviewed;$('pause').disabled=reviewed;$('reveal').hidden=!reviewed;$('revealed').hidden=true;$('revealed').textContent='';$('status').textContent=reviewed?'Review already recorded. Results remain hidden until you reveal them.':'';syncTimer();}
$('viewer').onchange=async()=>{const selected=$('viewer').value, run=current;if(selected==='diff'){$('diff').textContent=sourceDiff;return;}try{const choice=JSON.parse(selected);const query=new URLSearchParams(choice);const data=await api('/api/context/'+encodeURIComponent(run)+'?'+query);if(current===run&&$('viewer').value===selected)$('diff').textContent=data.text;}catch(error){fail(error);}};
$('load').onclick=()=>load().catch(fail);
$('pause').onclick=()=>{paused=!paused;syncTimer();};
$('add').onclick=finding;
$('form').onsubmit=async event=>{event.preventDefault();tick();const findings=[...$('findings').children].map(row=>({location:row.children[0].value,severity:row.children[1].value,text:row.children[2].value}));try{await api('/api/review/'+encodeURIComponent(current),{reviewer:$('reviewer').value,decision:$('decision').value,active_elapsed_seconds:elapsed,findings,comprehension_answer:$('answer').value,confidence:Number($('confidence').value)});reviewed=true;syncTimer();$('fields').disabled=true;$('pause').disabled=true;$('reveal').hidden=false;$('status').textContent='Review saved. You may now explicitly reveal results.';}catch(error){fail(error);}};
$('reveal').onclick=async()=>{try{const data=await api('/api/reveal/'+encodeURIComponent(current),{});$('revealed').textContent=JSON.stringify(data,null,2);$('revealed').hidden=false;}catch(error){fail(error);}};
api('/api/runs').then(data=>{for(const run of data.runs){const option=document.createElement('option');option.value=run.run_id;option.textContent=run.run_id+' · '+run.task+' · '+run.language+(run.reviewed?' · reviewed':'');$('runs').append(option);}$('progress').textContent=data.runs.length+' finished runs in seeded order';$('load').disabled=!data.runs.length;}).catch(fail);
</script></html>'''


def make_server(store, port=0, seed=0, subset=None):
    """Bind only IPv4 loopback; every route requires an unguessable session token."""
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    runs = [run for run in store.list_runs(seed=seed, subset=subset) if run["finished"]]
    aliases = {anonymous_id(run["run_id"]): run["run_id"] for run in runs}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # Avoid writing the URL token or evaluator artifacts to terminal logs.
            pass

        def _respond(self, code, data, html=False):
            body = data.encode("utf-8") if html else json.dumps(data, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, page=False):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                self._respond(403, {"error": "invalid host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + expected:
                self._respond(403, {"error": "invalid origin"})
                return False
            supplied = self.headers.get("X-Review-Token", "")
            if page:
                supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("ascii")):
                self._respond(403, {"error": "invalid session token"})
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self._authorized(page=path == "/"):
                return
            if path == "/":
                self._respond(200, PAGE.replace("TOKEN_VALUE", token).replace("NONCE_VALUE", nonce), html=True)
                return
            if path == "/api/runs":
                views = []
                for alias, run_id in aliases.items():
                    views.append({**store.get_run(run_id), "run_id": alias})
                self._respond(200, {"runs": views})
                return
            if path.startswith("/api/context/"):
                alias = path.removeprefix("/api/context/")
                if alias in aliases:
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        context = store.review_context(aliases[alias], query.get("view", [None])[0],
                                                       query.get("file", [None])[0])
                        self._respond(200, context)
                    except (OSError, ValueError, TypeError):
                        self._respond(400, {"error": "unknown review context file"})
                    return
            if path.startswith("/api/run/"):
                alias = path.removeprefix("/api/run/")
                if alias in aliases:
                    try:
                        view = store.get_run(aliases[alias])
                        self._respond(200, {**view, "run_id": alias, "source_diff": store.source_diff(aliases[alias])})
                    except (OSError, ValueError, TypeError):
                        self._respond(400, {"error": "unable to read run artifacts"})
                    return
            self._respond(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "api" or parts[1] not in ("review", "reveal") or parts[2] not in aliases:
                self._respond(404, {"error": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._respond(415, {"error": "JSON content type required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("request body must be between 1 byte and 1 MiB")
                body = json.loads(self.rfile.read(length))
                run_id = aliases[parts[2]]
                if parts[1] == "review":
                    result = store.save_review(run_id, body)
                else:
                    result = store.reveal_run(run_id)
                self._respond(200, result)
            except FileExistsError:
                self._respond(409, {"error": "review already recorded"})
            except (ValueError, TypeError) as error:
                self._respond(400, {"error": str(error)})
            except OSError:
                self._respond(400, {"error": "unable to read or save run artifacts"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.review_url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    server.review_token = token
    return server


def serve(root, port=0, seed=0, subset=None):
    server = make_server(ResultStore(root), port=port, seed=seed, subset=subset)
    print(f"Open the private review session: {server.review_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Evaluator-only results directory outside Git")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses an available port")
    parser.add_argument("--seed", default="0", help="Deterministic review ordering seed")
    parser.add_argument("--run", action="append", dest="subset", help="Select a stored run ID; repeatable")
    args = parser.parse_args(argv)
    serve(args.root, args.port, args.seed, args.subset)


if __name__ == "__main__":
    main()
