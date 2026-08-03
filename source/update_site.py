#!/usr/bin/env python3
"""Rebuild and (optionally) deploy the hosted browser version of the agent.

  python update_site.py            # rebuild PriorAuthAgent.html from
                                   # site-template.html + /policies + /scenarios
  python update_site.py --deploy   # rebuild, then push to GitHub Pages
                                   # (live ~1 min later at the same URL)

The hosted page is GENERATED — edit site-template.html (UI / JS / prompts),
policies/*.md, or scenarios/*.json, never PriorAuthAgent.html directly.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import agent

BASE = Path(__file__).resolve().parent
TEMPLATE = BASE / "site-template.html"
OUTPUT = BASE / "PriorAuthAgent.html"
REPO = "KilgoreC1024/prior-auth-agent-demo"
SITE_URL = "https://kilgorec1024.github.io/prior-auth-agent-demo/"


def build() -> str:
    docs = agent.load_documents()
    policies, addenda = [], []
    for d in docs:
        m = d["meta"]
        if m.get("doc_type") == "policy":
            policies.append({"payer": m["payer"], "policy_id": m["policy_id"],
                             "procedure": m["procedure"], "keywords": m["keywords"],
                             "reimbursement_usd": int(m.get("reimbursement_usd", 0)),
                             "pend_window_days": int(m.get("pend_window_days", 14)),
                             "body": d["body"]})
        elif m.get("doc_type") == "addendum":
            addenda.append({"addendum_id": m["addendum_id"], "policy_id": m["policy_id"],
                            "filename": d["path"].name, "body": d["body"]})

    presets = []
    for path in sorted(agent.SCENARIO_DIR.glob("*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        presets.append({"id": s.get("scenario_id", path.stem),
                        "label": s.get("scenario_id", "?") + " — " + s.get("procedure", "?"),
                        "payer": s["payer"], "procedure": s["procedure"],
                        "documents": s["documents_submitted"],
                        "description": s.get("description", "")})

    financials = agent.load_financials()
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__CORPUS__", json.dumps({"policies": policies, "addenda": addenda,
                                               "financials": financials}))
            .replace("__PRESETS__", json.dumps(presets)))
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"built {OUTPUT.name}: {len(html) // 1024} KB "
          f"({len(policies)} policies, {len(addenda)} addenda, {len(presets)} presets)")
    return html


def deploy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *cmd, **kw: subprocess.run(cmd, check=True, cwd=kw.pop("cwd", tmp), **kw)
        run("git", "clone", "--depth", "1", f"https://github.com/{REPO}.git", "site")
        clone = Path(tmp) / "site"
        (clone / "index.html").write_text(OUTPUT.read_text(encoding="utf-8"), encoding="utf-8")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=clone,
                                capture_output=True, text=True).stdout.strip()
        if not status:
            print("no changes to deploy — the live site already matches.")
            return
        run("git", "add", "index.html", cwd=clone)
        run("git", "-c", "user.name=KilgoreC1024",
            "-c", "user.email=christiankilgore1024@gmail.com",
            "commit", "-q", "-m", "Update live demo", cwd=clone)
        run("git", "push", "-q", cwd=clone)
        print(f"pushed — live in about a minute at {SITE_URL}")


if __name__ == "__main__":
    build()
    if "--deploy" in sys.argv:
        deploy()
