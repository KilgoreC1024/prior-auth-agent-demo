#!/usr/bin/env python3
"""Live web UI for the Prior-Authorization Checklist Agent.

Runs the REAL agent (agent.py) — including the Claude API calls — and streams
each step of the loop to the browser as it happens.

Usage:
  pip install flask anthropic
  python app.py            # then open http://localhost:5001
"""

import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

import anthropic
from flask import Flask, Response, request

import agent

# A key pasted into the setup screen is stored here (local machine only).
KEY_FILE = Path(__file__).resolve().parent / ".anthropic_api_key"


IU_BASE_URL = "https://hub.kelley.iu.edu/llmapi/v1"
IU_MODELS = ["gpt-oss-20b", "llama-4-scout"]


def resolve_llm() -> bool:
    """True when some usable LLM config exists: a provider config file, or an
    Anthropic key in the env, Windows user env, or the legacy key file."""
    if agent.CONFIG_FILE.exists():
        return True
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                os.environ["ANTHROPIC_API_KEY"] = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
            return True
        except OSError:
            pass
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
    return False


def validate_anthropic(key: str):
    """Cheap live check: count_tokens is free and fails fast on a bad key."""
    try:
        anthropic.Anthropic(api_key=key).messages.count_tokens(
            model=agent.MODEL, messages=[{"role": "user", "content": "ping"}])
        return True, ""
    except anthropic.AuthenticationError:
        return False, ("Anthropic rejected that key. Check you copied the whole thing "
                       "(it starts with sk-ant-) and that the key hasn't been revoked.")
    except anthropic.APIConnectionError:
        return False, "Could not reach the Anthropic API. Check your internet connection and try again."
    except anthropic.APIStatusError as e:
        return False, f"API error while testing the key: {e.message}"


def validate_openai_compat(key: str, base_url: str, model: str):
    """Live check against an OpenAI-spec endpoint (e.g. the IU Kelley hub).
    Returns (ok, message, available_models)."""
    try:
        from openai import OpenAI
    except ImportError:
        return False, "The 'openai' package is missing on this machine — run: pip install openai", []
    client = OpenAI(api_key=key, base_url=base_url, timeout=30)
    try:
        client.chat.completions.create(
            model=model, max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the word ok."}])
        return True, "", []
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg or "invalid_api_key" in msg:
            return False, "The endpoint rejected that token — check you copied the whole thing.", []
        # Model rejected or not served: ask the endpoint what this token CAN use
        try:
            available = [m.id for m in client.models.list()]
        except Exception:
            available = []
        if available:
            return False, (f"That token can't use model '{model}'. Models it can access: "
                           + ", ".join(available)
                           + " — pick one from the list (I've filled in the first)."), available
        return False, f"Could not complete a test call with model '{model}': {msg[:250]}", []


app = Flask(__name__)
app.config["HAS_KEY"] = resolve_llm()

DOC_POOL = [
    "Cardiology consultation note",
    "Cardiothoracic surgery consultation note",
    "Coronary angiography report",
    "Transthoracic echocardiogram report (with LVEF)",
    "Heart team conference / evaluation note",
    "STS risk score worksheet",
    "Surgical risk assessment (STS)",
    "Smoking cessation counseling documentation",
    "Referring physician order",
    "Clinical notes documenting symptoms (angina / ischemia)",
    "Noninvasive stress test results",
    "Current medication list",
    "Guideline-directed medical therapy documentation",
    "Cardiac CT angiography (TAVR planning)",
    "Frailty assessment documentation",
]


def load_presets():
    presets = []
    for path in sorted(agent.SCENARIO_DIR.glob("*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        presets.append({
            "id": s.get("scenario_id", path.stem),
            "label": s.get("scenario_id", "?") + " — " + s.get("procedure", "?"),
            "payer": s["payer"],
            "procedure": s["procedure"],
            "documents": s["documents_submitted"],
            "description": s.get("description", ""),
        })
    return presets


def run_agent_events(payer: str, procedure: str, submitted: list):
    """Generator: runs the real agent loop, yielding one JSON event per step."""
    def ev(**kw):
        return json.dumps(kw) + "\n"

    docs = agent.load_documents()
    yield ev(type="trace", step="STEP 1", text=f"searching /policies for payer='{payer}' procedure='{procedure}'")
    status, matches = agent.retrieve_policy(payer, procedure, docs)

    if status == "none":
        yield ev(type="trace", text=f"no {payer} policy matched procedure '{procedure}'", hl=True)
        yield ev(type="trace", text="ESCALATING to human review: no governing policy found", hl=True)
        yield ev(type="escalation", reason="none", payer=payer, procedure=procedure, candidates=[])
        return

    if status == "ambiguous":
        cands = [{"id": d["meta"]["policy_id"], "proc": d["meta"]["procedure"], "score": s}
                 for s, d in matches]
        yield ev(type="trace", text=f"retrieval returned {len(matches)} near-tied policy matches", hl=True)
        for c in cands:
            yield ev(type="trace", text=f"   {c['id']} ({c['proc']}, score {c['score']})")
        yield ev(type="trace", text="ambiguity detected -- the agent will NOT guess", hl=True)
        yield ev(type="trace", text="ESCALATING to human review", hl=True)
        yield ev(type="escalation", reason="ambiguous", payer=payer, procedure=procedure, candidates=cands)
        return

    score, policy = matches[0]
    meta = policy["meta"]
    yield ev(type="trace", text=f"retrieved {meta['payer']} policy {meta['policy_id']} "
                                f"'{meta['procedure']}' (score {score})")

    label = agent.llm_label()
    yield ev(type="trace", step="STEP 2", text=f"[{label}] reading the policy for addendum cross-references...", llm=True)
    analysis = agent.analyze_policy(policy["body"])
    required_docs = list(analysis["required_documents"])
    addenda_used = []

    if analysis["references_addendum"] and analysis["addendum_ids"]:
        for add_id in analysis["addendum_ids"]:
            yield ev(type="trace", text=f"detected addendum reference {add_id} -> issuing second retrieval", hl=True)
            addendum = agent.retrieve_addendum(add_id, docs)
            if addendum is None:
                yield ev(type="trace", text=f"WARNING: addendum {add_id} not found in /policies", hl=True)
                continue
            yield ev(type="trace", text=f"retrieved addendum {addendum['meta']['policy_id']} "
                                        f"({addendum['path'].name})")
            yield ev(type="trace", text=f"[{label}] extracting requirements from addendum {add_id}...", llm=True)
            add_reqs = agent.analyze_addendum(addendum["body"])["required_documents"]
            yield ev(type="trace", text=f"addendum adds {len(add_reqs)} required document(s)")
            required_docs.extend(add_reqs)
            addenda_used.append(add_id)
    else:
        yield ev(type="trace", text="no addendum referenced -- proceeding with base policy requirements")

    yield ev(type="trace", text=f"combined requirement list: {len(required_docs)} document(s)")

    yield ev(type="trace", step="STEP 3", text="completeness_check(required, submitted)  [deterministic Python -- no LLM]")
    check = agent.completeness_check(required_docs, submitted)
    yield ev(type="trace", text=f"completeness check: {check['missing_count']} missing of "
                                f"{check['required_count']} required", hl=check["missing_count"] > 0)

    fin = agent.financial_impact(meta, check)
    if fin["submission_status"] == "incomplete":
        yield ev(type="trace", text=f"financial impact: ${fin['expected_cost_if_submitted_usd']:,} expected "
                                    f"cost if submitted as-is (${fin['reimbursement_at_risk_usd']:,} at risk)", hl=True)
    else:
        yield ev(type="trace", text=f"financial impact: clean claim protects "
                                    f"${fin['reimbursement_at_risk_usd']:,} reimbursement")

    yield ev(type="trace", step="STEP 4", text=f"[{label}] reasoning over tool output -> drafting checklist + risk flag...", llm=True)
    scenario = {"payer": payer, "procedure": procedure, "documents_submitted": submitted}
    result = agent.draft_checklist(scenario, meta, addenda_used, check, fin)
    yield ev(type="trace", text=f"final risk flag: {result['risk_flag']}", hl=result["risk_flag"] == "HIGH")

    yield ev(type="result", policy_id=meta["policy_id"], policy_name=meta["procedure"],
             addenda=addenda_used, financial=fin, **result)


@app.post("/setup")
def setup():
    body = request.get_json(force=True)
    provider = body.get("provider", "anthropic")
    key = (body.get("key") or "").strip()

    if provider == "anthropic":
        if not key.startswith("sk-ant-"):
            return {"ok": False, "message": "That doesn't look like an Anthropic API key — it should start with sk-ant-."}
        ok, msg = validate_anthropic(key)
        if not ok:
            return {"ok": False, "message": msg}
        config = {"provider": "anthropic", "api_key": key}
        os.environ["ANTHROPIC_API_KEY"] = key
    else:
        base_url = (body.get("base_url") or "").strip() or IU_BASE_URL
        model = (body.get("model") or "").strip()
        if not key:
            return {"ok": False, "message": "Paste the API token for the endpoint."}
        if not model:
            return {"ok": False, "message": "Enter the model name to use (e.g. gpt-oss-20b)."}
        ok, msg, available = validate_openai_compat(key, base_url, model)
        if not ok:
            return {"ok": False, "message": msg, "models": available}
        config = {"provider": "openai", "api_key": key, "base_url": base_url, "model": model}

    agent.CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    agent._backend = None  # rebuild the cached client with the new settings
    app.config["HAS_KEY"] = True
    return {"ok": True, "label": agent.llm_label()}


@app.post("/run")
def run():
    body = request.get_json(force=True)
    payer = body.get("payer", "")
    procedure = body.get("procedure", "").strip()
    submitted = body.get("documents", [])

    def stream():
        if not app.config["HAS_KEY"]:
            yield json.dumps({"type": "needkey",
                              "message": "No API key is set yet — paste yours in the setup panel above."}) + "\n"
            return
        try:
            yield from run_agent_events(payer, procedure, submitted)
        except anthropic.AuthenticationError:
            app.config["HAS_KEY"] = False
            yield json.dumps({"type": "needkey",
                              "message": "Anthropic rejected the stored API key — enter a valid one above."}) + "\n"
        except Exception as e:  # surface API problems to the UI
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return Response(stream(), mimetype="application/x-ndjson")


@app.get("/")
def index():
    presets = load_presets()
    page = (PAGE
            .replace("__PRESETS__", json.dumps(presets))
            .replace("__DOCS__", json.dumps(DOC_POOL))
            .replace("__HAS_KEY__", "true" if app.config["HAS_KEY"] else "false")
            .replace("__LLM_LABEL__", agent.llm_label() if app.config["HAS_KEY"] else "not configured yet"))
    return Response(page, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prior-Auth Checklist Agent — Live</title>
<style>
  :root {
    --bg:#F6F8F8; --surface:#FFFFFF; --ink:#17272D; --muted:#5A6E75; --line:#DCE4E6;
    --accent:#0E7C86; --accent-deep:#0A5A62;
    --ok-bg:#E2F1E7; --ok-ink:#256B40; --hi-bg:#F9E6E2; --hi-ink:#A93A2C;
    --esc-bg:#F4EDDB; --esc-ink:#8A6A1F;
    --term-bg:#101B1F; --term-ink:#C8DADF; --term-accent:#5FD3DD; --code-bg:#EDF2F3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0E181C; --surface:#152329; --ink:#E3ECEE; --muted:#8FA5AC; --line:#263A42;
      --accent:#4CC4CE; --accent-deep:#7ED8DF;
      --ok-bg:#1C3325; --ok-ink:#85D3A0; --hi-bg:#3A211D; --hi-ink:#F0998C;
      --esc-bg:#332B18; --esc-ink:#E0C070;
      --term-bg:#0A1316; --code-bg:#1D2E35;
    }
  }
  * { box-sizing: border-box; }
  body { background:var(--bg); color:var(--ink); font-family:system-ui,"Segoe UI",Roboto,sans-serif;
         line-height:1.5; margin:0; padding:24px 20px 60px; }
  main { max-width:1120px; margin:0 auto; }
  h1 { font-family:Charter,"Bitstream Charter",Cambria,Georgia,serif; font-size:1.5rem; margin:0; }
  .tag { display:inline-block; font-size:.68rem; font-weight:700; letter-spacing:.1em;
         color:var(--accent); border:1px solid var(--accent); border-radius:999px;
         padding:1px 10px; vertical-align:3px; margin-left:10px; }
  .sub { color:var(--muted); font-size:.88rem; margin:6px 0 20px; }
  .cols { display:grid; grid-template-columns: 420px 1fr; gap:18px; align-items:start; }
  @media (max-width: 900px) { .cols { grid-template-columns: 1fr; } }
  .panel { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:18px 20px; }
  legend, .lbl { font-size:.7rem; letter-spacing:.12em; text-transform:uppercase;
                 color:var(--muted); font-weight:600; margin:0 0 8px; padding:0; }
  fieldset { border:none; margin:0 0 16px; padding:0; }
  input[type=radio], input[type=checkbox] { accent-color:var(--accent); }
  .radio-row { display:flex; gap:20px; font-size:.9rem; }
  label { display:flex; gap:8px; align-items:baseline; cursor:pointer; }
  input[type=text], input[type=password] { width:100%; font:inherit; font-size:.9rem; color:var(--ink);
                     background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px 12px; }
  input[type=text]:focus-visible, input[type=password]:focus-visible,
  button:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .keycard { border-color:var(--accent); margin-bottom:18px; }
  .keyrow { display:flex; gap:10px; flex-wrap:wrap; }
  .keyrow input { flex:1; min-width:260px; }
  .keyrow .runbtn { width:auto; }
  .keyok { color:var(--ok-ink); font-size:.88rem; }
  a { color:var(--accent-deep); }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .chip { font:inherit; font-size:.76rem; background:var(--code-bg); color:var(--ink);
          border:1px solid var(--line); border-radius:999px; padding:3px 11px; cursor:pointer; }
  .chip:hover { border-color:var(--accent); color:var(--accent-deep); }
  .docgrid { display:grid; gap:5px; font-size:.85rem; }
  .runbtn { font:inherit; font-size:.95rem; font-weight:600; background:var(--accent); color:#fff;
            border:none; border-radius:6px; padding:10px 24px; cursor:pointer; width:100%; }
  .runbtn:disabled { opacity:.55; cursor:wait; }
  .presets { display:flex; flex-wrap:wrap; gap:6px; }
  .term { background:var(--term-bg); color:var(--term-ink);
          font-family:Consolas,"Cascadia Code",ui-monospace,monospace; font-size:.8rem; line-height:1.65;
          border-radius:6px; padding:14px 16px; overflow-x:auto; min-height:120px; }
  .term .t-label { color:var(--term-accent); font-size:.66rem; letter-spacing:.14em;
                   text-transform:uppercase; display:block; margin-bottom:8px; }
  .term pre { margin:0; font-family:inherit; white-space:pre-wrap; }
  .term .hl { color:var(--term-accent); }
  .term .step { color:#8FB6BF; font-weight:700; }
  .term .llm { color:#E8C87E; }
  .cursor { display:inline-block; width:8px; height:14px; background:var(--term-accent);
            vertical-align:-2px; animation:blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity:0; } }
  @media (prefers-reduced-motion: reduce) { .cursor { animation:none; } }
  .pill { display:inline-block; font-size:.72rem; font-weight:700; letter-spacing:.07em;
          padding:2px 10px; border-radius:999px; white-space:nowrap; }
  .pill.low { background:var(--ok-bg); color:var(--ok-ink); }
  .pill.high { background:var(--hi-bg); color:var(--hi-ink); }
  .pill.esc { background:var(--esc-bg); color:var(--esc-ink); }
  .verdict { display:flex; align-items:center; gap:12px; margin:16px 0 8px; flex-wrap:wrap; }
  .verdict h3 { font-size:1rem; margin:0; }
  ul.checklist { list-style:none; padding:0; margin:10px 0; font-size:.88rem; }
  ul.checklist li { display:flex; gap:10px; padding:3px 0; align-items:baseline; }
  ul.checklist .box { font-family:Consolas,ui-monospace,monospace; font-weight:700; }
  ul.checklist li.ok .box { color:var(--ok-ink); }
  ul.checklist li.miss { color:var(--hi-ink); } ul.checklist li.miss .box { color:var(--hi-ink); }
  ul.checklist .note { color:var(--muted); font-size:.82rem; }
  ul.checklist li.miss .note { color:inherit; opacity:.85; }
  .finbox { background:var(--code-bg); border-radius:6px; padding:12px 16px; margin:12px 0; font-size:.85rem; max-width:560px; }
  .finrow { display:flex; justify-content:space-between; gap:12px; padding:2px 0; }
  .finrow .v { font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; }
  .finrow.total { border-top:1px solid var(--line); margin-top:4px; padding-top:6px; }
  .expl { font-size:.88rem; color:var(--muted); max-width:70ch; }
  .next { font-size:.86rem; padding-left:20px; }
  .next li { margin:3px 0; }
  .err { color:var(--hi-ink); font-size:.88rem; background:var(--hi-bg); border-radius:6px; padding:10px 14px; }
  footer { color:var(--muted); font-size:.78rem; margin-top:28px; }
</style>
</head>
<body>
<main>
  <h1>Prior-Authorization Checklist Agent <span class="tag">LIVE — real Claude calls</span></h1>
  <p class="sub">Every run below executes the actual agent loop: keyword retrieval, the model reading the
  policy and deciding on addenda, a deterministic completeness check, and the model drafting the final
  checklist. Yellow trace lines are live LLM calls — the pause is the model reading.</p>

  <div class="panel keycard" id="keycard" hidden>
    <p class="lbl">One-time setup — connect an AI provider</p>
    <div class="radio-row" style="margin-bottom:10px">
      <label><input type="radio" name="provider" value="openai" checked> IU Kelley LLM hub (class token — free)</label>
      <label><input type="radio" name="provider" value="anthropic"> Anthropic (your own key)</label>
    </div>
    <div id="prov-openai">
      <p class="sub" style="margin-bottom:10px">Use the IU-hosted models with the API token shared for the
      course. The token is saved to a file next to <code>app.py</code> on this computer only.</p>
      <div class="keyrow" style="margin-bottom:8px">
        <input type="password" id="iukey" placeholder="IU API token" autocomplete="off">
      </div>
      <div class="keyrow" style="margin-bottom:8px">
        <input type="text" id="iubase" value="https://hub.kelley.iu.edu/llmapi/v1" title="Endpoint base URL">
        <input type="text" id="iumodel" value="gpt-oss-20b" list="iumodels" title="Model name" style="max-width:200px">
        <datalist id="iumodels"><option value="gpt-oss-20b"><option value="llama-4-scout"></datalist>
      </div>
    </div>
    <div id="prov-anthropic" hidden>
      <p class="sub" style="margin-bottom:10px">Create a key at
      <a href="https://platform.claude.com" target="_blank" rel="noopener">platform.claude.com</a>
      (Settings &rarr; API Keys; billing must be enabled &mdash; each full run costs a few cents).
      The key is saved to a file next to <code>app.py</code> on this computer only.</p>
      <div class="keyrow" style="margin-bottom:8px">
        <input type="password" id="keyinput" placeholder="sk-ant-..." autocomplete="off">
      </div>
    </div>
    <div class="keyrow">
      <button type="button" class="runbtn" id="keysave">Save &amp; test connection</button>
    </div>
    <p class="err" id="keyerr" hidden></p>
    <p class="keyok" id="keyok" hidden>Verified and saved &mdash; you're ready to run the agent.</p>
  </div>

  <div class="cols">
    <div class="panel">
      <fieldset>
        <legend>Preset scenarios</legend>
        <div class="presets" id="presets"></div>
      </fieldset>
      <fieldset>
        <legend>1 · Payer</legend>
        <div class="radio-row">
          <label><input type="radio" name="payer" value="Acme Health" checked> Acme Health</label>
          <label><input type="radio" name="payer" value="Beacon Insurance"> Beacon Insurance</label>
        </div>
      </fieldset>
      <fieldset>
        <legend>2 · Procedure requested</legend>
        <input type="text" id="proc" value="Coronary artery bypass graft (CABG)">
        <div class="chips">
          <button type="button" class="chip">Coronary artery bypass graft (CABG)</button>
          <button type="button" class="chip">Aortic valve replacement (TAVR)</button>
          <button type="button" class="chip">Cardiac catheterization</button>
          <button type="button" class="chip">Transcatheter valve intervention</button>
          <button type="button" class="chip">Total knee replacement</button>
        </div>
      </fieldset>
      <fieldset>
        <legend>3 · Documents in the submission</legend>
        <div class="docgrid" id="docs"></div>
      </fieldset>
      <button type="button" class="runbtn" id="run">Run the agent</button>
    </div>

    <div>
      <div class="term" id="trace"><span class="t-label">Execution trace</span><pre id="tracepre">Waiting for a run...</pre></div>
      <div id="result" aria-live="polite"></div>
    </div>
  </div>

  <footer>Synthetic payers, policies and patients — coursework demo, not medical or coverage advice.
  Model: __LLM_LABEL__ &middot; <a href="#" id="changekey">change AI provider / key</a></footer>
</main>

<script>
  var PRESETS = __PRESETS__;
  var DOCS = __DOCS__;
  var HAS_KEY = __HAS_KEY__;

  var keycard = document.getElementById("keycard");
  var keyerr = document.getElementById("keyerr");
  var keyok = document.getElementById("keyok");
  if (!HAS_KEY) keycard.hidden = false;

  document.getElementById("changekey").addEventListener("click", function (e) {
    e.preventDefault();
    keycard.hidden = false;
    keycard.scrollIntoView({ behavior: "smooth" });
  });

  document.querySelectorAll('input[name=provider]').forEach(function (radio) {
    radio.addEventListener("change", function () {
      var openai = document.querySelector('input[name=provider]:checked').value === "openai";
      document.getElementById("prov-openai").hidden = !openai;
      document.getElementById("prov-anthropic").hidden = openai;
    });
  });

  document.getElementById("keysave").addEventListener("click", async function () {
    var provider = document.querySelector('input[name=provider]:checked').value;
    var payload = { provider: provider };
    if (provider === "anthropic") {
      payload.key = document.getElementById("keyinput").value.trim();
    } else {
      payload.key = document.getElementById("iukey").value.trim();
      payload.base_url = document.getElementById("iubase").value.trim();
      payload.model = document.getElementById("iumodel").value.trim();
    }
    keyerr.hidden = true; keyok.hidden = true;
    var btn = this; btn.disabled = true; btn.textContent = "Testing connection...";
    try {
      var resp = await fetch("/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (data.ok) {
        HAS_KEY = true;
        keyok.hidden = false;
        setTimeout(function () { keycard.hidden = true; }, 1800);
      } else {
        keyerr.textContent = data.message;
        keyerr.hidden = false;
        if (data.models && data.models.length) {
          var dl = document.getElementById("iumodels");
          dl.innerHTML = "";
          data.models.forEach(function (m) {
            var o = document.createElement("option");
            o.value = m; dl.appendChild(o);
          });
          document.getElementById("iumodel").value = data.models[0];
        }
      }
    } catch (e) {
      keyerr.textContent = "Could not reach the local server: " + e.message;
      keyerr.hidden = false;
    } finally {
      btn.disabled = false; btn.textContent = "Save & test connection";
    }
  });

  var docsEl = document.getElementById("docs");
  DOCS.forEach(function (d, i) {
    var l = document.createElement("label");
    var c = document.createElement("input");
    c.type = "checkbox"; c.value = d;
    l.appendChild(c); l.appendChild(document.createTextNode(" " + d));
    docsEl.appendChild(l);
  });

  var procEl = document.getElementById("proc");
  document.querySelectorAll(".chip").forEach(function (ch) {
    ch.addEventListener("click", function () { procEl.value = ch.textContent; });
  });

  // Preset buttons fill the whole form (fuzzy-match their docs onto the pool checkboxes)
  var presetEl = document.getElementById("presets");
  PRESETS.forEach(function (p) {
    var b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = p.label; b.title = p.description;
    b.addEventListener("click", function () {
      document.querySelectorAll('input[name=payer]').forEach(function (r) { r.checked = r.value === p.payer; });
      procEl.value = p.procedure;
      var boxes = docsEl.querySelectorAll("input");
      boxes.forEach(function (cb) { cb.checked = false; });
      p.documents.forEach(function (doc) {
        var dn = doc.toLowerCase(); var best = null, bestScore = 0;
        boxes.forEach(function (cb) {
          var pn = cb.value.toLowerCase();
          var dw = dn.replace(/[^a-z0-9 ]+/g, " ").split(/\s+/).filter(Boolean);
          var hits = dw.filter(function (w) { return pn.indexOf(w) !== -1; }).length;
          var score = hits / dw.length;
          if (score > bestScore) { bestScore = score; best = cb; }
        });
        if (best && bestScore >= 0.4) best.checked = true;
      });
    });
    presetEl.appendChild(b);
  });

  var runBtn = document.getElementById("run");
  var pre = document.getElementById("tracepre");
  var resultEl = document.getElementById("result");

  function esc(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
  function pill(kind, text) { return '<span class="pill ' + kind + '">' + text + '</span>'; }

  function addTraceLine(evt) {
    var cls = evt.llm ? "llm" : (evt.hl ? "hl" : "");
    var stepTag = evt.step ? '<span class="step">' + esc(evt.step) + '</span>  ' : "       ";
    var line = document.createElement("span");
    line.innerHTML = stepTag + (cls ? '<span class="' + cls + '">' : "") + "-&gt; " + esc(evt.text) +
                     (cls ? "</span>" : "") + "\n";
    pre.appendChild(line);
  }

  function renderResult(evt) {
    var risky = evt.risk_flag === "HIGH";
    var h = '<div class="panel" style="margin-top:14px">';
    h += '<div class="verdict">' + pill(risky ? "high" : "low", "RISK: " + evt.risk_flag) +
         "<h3>" + esc(evt.policy_id) + " · " + esc(evt.policy_name) +
         (evt.addenda.length ? " + Addendum " + evt.addenda.join(", ") : "") + "</h3></div>";
    h += '<ul class="checklist">';
    evt.checklist.forEach(function (it) {
      var ok = it.status === "present";
      h += '<li class="' + (ok ? "ok" : "miss") + '"><span class="box">' + (ok ? "[x]" : "[ ]") +
           "</span><span>" + esc(it.item) +
           (it.note ? ' <span class="note">— ' + esc(it.note) + "</span>" : "") + "</span></li>";
    });
    h += "</ul>";
    if (evt.financial) {
      var f = evt.financial;
      var fmt = function (n) { return "$" + Number(n).toLocaleString(); };
      h += '<div class="finbox"><p class="lbl">Financial impact — deterministic tool (synthetic benchmarks)</p>';
      h += '<div class="finrow"><span>Reimbursement at risk</span><span class="v">' + fmt(f.reimbursement_at_risk_usd) + '</span></div>';
      if (f.submission_status === "incomplete") {
        h += '<div class="finrow"><span>Rework cost if pended</span><span class="v">' + fmt(f.rework_cost_usd) + '</span></div>';
        h += '<div class="finrow"><span>Expected admin-denial loss (' + Math.round(f.admin_denial_rate_after_pend * 100) + '% of reimbursement)</span><span class="v">' + fmt(f.expected_admin_denial_loss_usd) + '</span></div>';
        h += '<div class="finrow total"><span>Expected cost if submitted as-is (+ ' + f.pend_window_days + '-day pend delay)</span><span class="v">' + fmt(f.expected_cost_if_submitted_usd) + '</span></div>';
      } else {
        h += '<div class="finrow total"><span>Pend/denial exposure — submission complete</span><span class="v">$0</span></div>';
      }
      h += "</div>";
    }
    h += '<p class="expl">' + esc(evt.explanation) + "</p>";
    if (evt.recommended_next_steps && evt.recommended_next_steps.length) {
      h += '<p class="lbl" style="margin-top:14px">Next steps</p><ul class="next">';
      evt.recommended_next_steps.forEach(function (s) { h += "<li>" + esc(s) + "</li>"; });
      h += "</ul>";
    }
    h += "</div>";
    resultEl.innerHTML = h;
  }

  function renderEscalation(evt) {
    var h = '<div class="panel" style="margin-top:14px">';
    h += '<div class="verdict">' + pill("esc", "ESCALATED") + "<h3>Routed to human review</h3></div>";
    if (evt.reason === "ambiguous") {
      h += '<p class="expl">“' + esc(evt.procedure) + "” plausibly matches " + evt.candidates.length +
           " different " + esc(evt.payer) + " policies (" +
           evt.candidates.map(function (c) { return c.id; }).join(", ") +
           "). A confident wrong pick would apply the wrong requirements, so the agent hands off " +
           "to a human intake specialist instead of guessing. No LLM tokens were spent.</p>";
    } else {
      h += '<p class="expl">' + esc(evt.payer) + " has no prior-authorization policy matching “" +
           esc(evt.procedure) + "”. The agent escalates rather than inventing requirements.</p>";
    }
    h += "</div>";
    resultEl.innerHTML = h;
  }

  runBtn.addEventListener("click", async function () {
    var payer = document.querySelector('input[name=payer]:checked').value;
    var documents = Array.prototype.filter.call(docsEl.querySelectorAll("input"), function (c) { return c.checked; })
                    .map(function (c) { return c.value; });
    runBtn.disabled = true;
    resultEl.innerHTML = "";
    pre.innerHTML = "";
    var cur = document.createElement("span"); cur.className = "cursor";
    pre.parentNode.appendChild(cur);

    try {
      var resp = await fetch("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ payer: payer, procedure: procEl.value, documents: documents })
      });
      var reader = resp.body.getReader();
      var dec = new TextDecoder(); var buf = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var lines = buf.split("\n"); buf = lines.pop();
        lines.forEach(function (ln) {
          if (!ln.trim()) return;
          var evt = JSON.parse(ln);
          if (evt.type === "trace") addTraceLine(evt);
          else if (evt.type === "result") renderResult(evt);
          else if (evt.type === "escalation") renderEscalation(evt);
          else if (evt.type === "needkey") {
            HAS_KEY = false;
            keycard.hidden = false;
            keyok.hidden = true;
            keyerr.textContent = evt.message;
            keyerr.hidden = false;
            keycard.scrollIntoView({ behavior: "smooth" });
          }
          else if (evt.type === "error") {
            resultEl.innerHTML = '<p class="err">Agent error: ' + esc(evt.message) +
              "<br>Check that ANTHROPIC_API_KEY is set and the machine is online.</p>";
          }
        });
      }
    } catch (e) {
      resultEl.innerHTML = '<p class="err">Could not reach the agent server: ' + esc(e.message) + "</p>";
    } finally {
      cur.remove();
      runBtn.disabled = false;
    }
  });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("Prior-Auth Agent UI ->  http://localhost:5001")
    if not app.config["HAS_KEY"]:
        print("No API key found yet -- the page will ask for one (one-time setup).")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5001")).start()
    app.run(host="127.0.0.1", port=5001, debug=False)
