#!/usr/bin/env python3
"""Prior-Authorization Checklist Agent — proof of concept.

A visible multi-step agent loop for an MBA agentic-AI course:

  Step 1  Retrieve the matching payer policy (keyword search over /policies)
  Step 2  LLM inspects the policy and DECIDES whether it references an
          addendum; if yes, the agent issues a second targeted retrieval
  Step 3  Deterministic completeness_check() tool (plain Python, no LLM)
  Step 4  LLM reasons over the tool output -> final checklist + risk flag

  Ambiguous procedure names (multiple plausible policy matches) are
  escalated to human review instead of guessed at.

Usage:
  python agent.py scenarios/scenario-1-acme-cabg-complete.json
  python agent.py --all
"""

import argparse
import json
import re
import sys
from pathlib import Path

import anthropic

MODEL = "claude-opus-4-8"
BASE_DIR = Path(__file__).resolve().parent
POLICY_DIR = BASE_DIR / "policies"
SCENARIO_DIR = BASE_DIR / "scenarios"

# Optional provider config written by the app's setup screen. When absent,
# the agent uses the Anthropic API via ANTHROPIC_API_KEY (the default).
# Shape: {"provider": "anthropic"|"openai", "api_key": "...",
#         "base_url": "...", "model": "..."}   (base_url/model for openai only)
CONFIG_FILE = BASE_DIR / ".llm_config.json"

STOPWORDS = {"a", "an", "and", "the", "of", "or", "to", "in", "for", "with",
             "on", "by", "at", "per", "including"}

_backend = None


def load_llm_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            # utf-8-sig tolerates the BOM that Notepad/PowerShell often write
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARNING: ignoring unreadable {CONFIG_FILE.name}: {e}", file=sys.stderr)
    return {"provider": "anthropic"}


def llm_label() -> str:
    cfg = load_llm_config()
    if cfg.get("provider") == "openai":
        return cfg.get("model", "LLM")
    return "Claude"


class AnthropicBackend:
    """Anthropic API with server-enforced structured outputs."""

    def __init__(self, api_key: str = None, model: str = MODEL):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model

    def json(self, system: str, prompt: str, schema: dict) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Model declined the request (stop_reason=refusal).")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)


class OpenAICompatBackend:
    """Any OpenAI-spec endpoint (e.g. IU's Kelley hub). Open models may not
    support server-side JSON schemas, so the schema goes in the prompt and
    the reply is parsed leniently with one automatic repair retry."""

    def __init__(self, api_key: str, base_url: str, model: str):
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("The 'openai' package is required for this provider: pip install openai")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def _chat(self, messages):
        try:  # some servers support JSON mode; fall back cleanly if not
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=4000,
                response_format={"type": "json_object"})
        except Exception:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, max_tokens=4000)
        return resp.choices[0].message.content or ""

    @staticmethod
    def _parse(text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object in reply")
        return json.loads(text[start:end + 1])

    def json(self, system: str, prompt: str, schema: dict) -> dict:
        sys_full = (system + "\n\nRespond with ONLY one JSON object that validates "
                    "against this JSON Schema — no prose, no markdown fences:\n"
                    + json.dumps(schema))
        messages = [{"role": "system", "content": sys_full},
                    {"role": "user", "content": prompt}]
        reply = self._chat(messages)
        for attempt in range(2):
            try:
                data = self._parse(reply)
                missing = [k for k in schema.get("required", []) if k not in data]
                if missing:
                    raise ValueError(f"missing required keys: {missing}")
                return data
            except (ValueError, json.JSONDecodeError) as e:
                if attempt == 1:
                    raise RuntimeError(f"Model did not return valid JSON: {e}")
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content":
                              f"That was not valid ({e}). Reply again with ONLY the "
                              "corrected JSON object, nothing else."}]
                reply = self._chat(messages)


def get_backend():
    global _backend
    if _backend is None:
        cfg = load_llm_config()
        if cfg.get("provider") == "openai":
            _backend = OpenAICompatBackend(cfg["api_key"], cfg["base_url"], cfg["model"])
        else:
            _backend = AnthropicBackend(cfg.get("api_key") or None,
                                        cfg.get("model", MODEL))
    return _backend


def trace(msg: str) -> None:
    print(f"  -> {msg}")


# ---------------------------------------------------------------------------
# Document loading (markdown files with simple key: value frontmatter)
# ---------------------------------------------------------------------------

def load_documents():
    docs = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        key, val = line.split(":", 1)
                        meta[key.strip()] = val.strip()
                body = parts[2].strip()
        meta["keywords"] = [k.strip() for k in meta.get("keywords", "").split(",") if k.strip()]
        docs.append({"path": path, "meta": meta, "body": body})
    return docs


# ---------------------------------------------------------------------------
# Step 1 — keyword retrieval with ambiguity detection
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def token_set(s: str) -> set:
    return {t for t in normalize(s).split() if t and t not in STOPWORDS}


def tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 4 and shorter in longer


def score_policy(procedure: str, keywords: list) -> int:
    """Phrase hits are worth 2, single-token hits worth 1."""
    proc_norm = normalize(procedure)
    proc_tokens = token_set(procedure)
    score = 0
    for kw in keywords:
        if normalize(kw) in proc_norm:
            score += 2
    kw_tokens = set()
    for kw in keywords:
        kw_tokens |= token_set(kw)
    for t in proc_tokens:
        if any(tokens_match(t, k) for k in kw_tokens):
            score += 1
    return score


def retrieve_policy(payer: str, procedure: str, docs: list):
    """Returns (status, matches) where status is 'ok'|'ambiguous'|'none'."""
    candidates = [d for d in docs
                  if d["meta"].get("doc_type") == "policy"
                  and normalize(d["meta"].get("payer", "")) == normalize(payer)]
    scored = sorted(((score_policy(procedure, d["meta"]["keywords"]), d)
                     for d in candidates), key=lambda x: -x[0])
    # require at least 2 points (a phrase hit or two token hits) so a single
    # generic shared token like 'replacement' can't select a policy alone
    scored = [(s, d) for s, d in scored if s >= 2]
    if not scored:
        return "none", []
    top_score = scored[0][0]
    close = [(s, d) for s, d in scored if s >= 0.75 * top_score]
    if len(close) > 1:
        return "ambiguous", close
    return "ok", [scored[0]]


def retrieve_addendum(addendum_id: str, docs: list):
    for d in docs:
        if d["meta"].get("doc_type") == "addendum" and \
           normalize(d["meta"].get("addendum_id", "")) == normalize(addendum_id):
            return d
    return None


# ---------------------------------------------------------------------------
# LLM helper — structured JSON output, provider-agnostic
# ---------------------------------------------------------------------------

def llm_json(system: str, prompt: str, schema: dict) -> dict:
    return get_backend().json(system, prompt, schema)


# ---------------------------------------------------------------------------
# Step 2 — LLM inspects the policy and decides about addenda
# ---------------------------------------------------------------------------

POLICY_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "references_addendum": {"type": "boolean"},
        "addendum_ids": {"type": "array", "items": {"type": "string"}},
        "required_documents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["references_addendum", "addendum_ids", "required_documents"],
    "additionalProperties": False,
}

ADDENDUM_SCHEMA = {
    "type": "object",
    "properties": {
        "required_documents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["required_documents"],
    "additionalProperties": False,
}

ANALYST_SYSTEM = (
    "You are a prior-authorization policy analyst. You read synthetic payer "
    "policy documents and extract their documentation requirements exactly as "
    "written. Return each required document as a short canonical name of 2-6 "
    "words (e.g. 'Coronary angiography report') with no parenthetical "
    "qualifiers and no timeframes."
)


def analyze_policy(policy_body: str) -> dict:
    prompt = (
        "Read the payer policy below.\n\n"
        "1. Does it cross-reference a separate ADDENDUM document that carries "
        "additional documentation requirements? If yes, list each addendum's "
        "identifier (e.g. 'MN-201') in addendum_ids.\n"
        "2. List the required documents stated in THIS policy's Required "
        "Documentation section only (do NOT invent addendum requirements you "
        "have not read).\n\n"
        "--- POLICY DOCUMENT ---\n" + policy_body
    )
    return llm_json(ANALYST_SYSTEM, prompt, POLICY_ANALYSIS_SCHEMA)


def analyze_addendum(addendum_body: str) -> dict:
    prompt = (
        "Read the policy ADDENDUM below and list the additional required "
        "documents it imposes.\n\n"
        "--- ADDENDUM DOCUMENT ---\n" + addendum_body
    )
    return llm_json(ANALYST_SYSTEM, prompt, ADDENDUM_SCHEMA)


# ---------------------------------------------------------------------------
# Step 3 — deterministic completeness check (plain Python tool, no LLM)
# ---------------------------------------------------------------------------

def completeness_check(required_docs: list, submitted_docs: list) -> dict:
    """Deterministic tool: matches each required document against the
    submitted documents using normalized containment / token overlap.
    Picks the BEST match across all submissions (containment outranks
    token overlap) so a generic shared word like 'report' cannot steal
    a match from the correct document."""
    present, missing = [], []
    for req in required_docs:
        req_norm, req_tokens = normalize(req), token_set(req)
        matched, best_rank = None, (0, 0.0)
        for sub in submitted_docs:
            sub_norm, sub_tokens = normalize(sub), token_set(sub)
            overlap = (len(req_tokens & sub_tokens) / len(req_tokens)
                       if req_tokens else 0.0)
            if req_norm == sub_norm or req_norm in sub_norm or sub_norm in req_norm:
                rank = (2, overlap)
            elif overlap > 0.5:
                rank = (1, overlap)
            else:
                continue
            if rank > best_rank:
                matched, best_rank = sub, rank
        if matched:
            present.append({"required": req, "matched_submission": matched})
        else:
            missing.append(req)
    return {
        "present": present,
        "missing": missing,
        "missing_count": len(missing),
        "required_count": len(required_docs),
    }


# ---------------------------------------------------------------------------
# Step 3b — deterministic financial-impact tool (plain Python, no LLM)
# ---------------------------------------------------------------------------

FINANCIALS_FILE = BASE_DIR / "financials.json"


def load_financials() -> dict:
    return json.loads(FINANCIALS_FILE.read_text(encoding="utf-8-sig"))


def financial_impact(policy_meta: dict, check_result: dict) -> dict:
    """Deterministic tool: translates the completeness verdict into dollars.
    All figures are synthetic coursework benchmarks (see financials.json);
    the LLM narrates these numbers but never computes or invents them."""
    fin = load_financials()
    reimbursement = int(policy_meta.get("reimbursement_usd", 0))
    pend_days = int(policy_meta.get("pend_window_days", 14))
    incomplete = check_result["missing_count"] > 0

    rework = fin["rework_cost_per_pend_usd"] if incomplete else 0
    denial_rate = fin["admin_denial_rate_after_pend"]
    expected_denial_loss = round(reimbursement * denial_rate) if incomplete else 0
    review_cost = round(fin["manual_review_minutes"] / 60 * fin["staff_hourly_cost_usd"], 2)

    return {
        "submission_status": "incomplete" if incomplete else "complete",
        "reimbursement_at_risk_usd": reimbursement,
        "pend_window_days": pend_days,
        "rework_cost_usd": rework,
        "admin_denial_rate_after_pend": denial_rate,
        "expected_admin_denial_loss_usd": expected_denial_loss,
        "expected_cost_if_submitted_usd": rework + expected_denial_loss,
        "manual_review_cost_replaced_usd": review_cost,
        "benchmark_note": "synthetic coursework benchmarks -- see financials.json",
    }


# ---------------------------------------------------------------------------
# Step 4 — LLM drafts the final checklist with a risk flag
# ---------------------------------------------------------------------------

CHECKLIST_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_flag": {"type": "string", "enum": ["LOW", "HIGH"]},
        "explanation": {"type": "string"},
        "checklist": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "status": {"type": "string", "enum": ["present", "missing"]},
                    "note": {"type": "string"},
                },
                "required": ["item", "status", "note"],
                "additionalProperties": False,
            },
        },
        "recommended_next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["risk_flag", "explanation", "checklist", "recommended_next_steps"],
    "additionalProperties": False,
}

REVIEWER_SYSTEM = (
    "You are a prior-authorization completeness reviewer. You are given the "
    "output of a deterministic completeness-check tool and a deterministic "
    "financial-impact tool, and must produce a final submission checklist. "
    "Flag risk HIGH if any required document is missing (the submission would "
    "be pended or denied), LOW if the submission is complete. Write the "
    "explanation in plain English for a non-clinical operations audience, and "
    "weave in the financial stakes using ONLY the dollar figures provided by "
    "the financial-impact tool -- never invent or recalculate numbers."
)


def draft_checklist(scenario: dict, policy_meta: dict, addenda_used: list,
                    check_result: dict, fin_result: dict = None) -> dict:
    prompt = (
        "Draft the final prior-authorization submission checklist.\n\n"
        f"SUBMISSION:\n{json.dumps(scenario, indent=2)}\n\n"
        f"GOVERNING POLICY: {policy_meta.get('policy_id')} - "
        f"{policy_meta.get('procedure')} ({policy_meta.get('payer')})\n"
        f"ADDENDA APPLIED: {', '.join(addenda_used) if addenda_used else 'none'}\n\n"
        f"COMPLETENESS-CHECK TOOL OUTPUT:\n{json.dumps(check_result, indent=2)}"
    )
    if fin_result:
        prompt += f"\n\nFINANCIAL-IMPACT TOOL OUTPUT:\n{json.dumps(fin_result, indent=2)}"
    return llm_json(REVIEWER_SYSTEM, prompt, CHECKLIST_SCHEMA)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_scenario(scenario_path: Path) -> None:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    payer, procedure = scenario["payer"], scenario["procedure"]
    submitted = scenario["documents_submitted"]

    print("=" * 78)
    print(f"SCENARIO {scenario.get('scenario_id', '?')}: {scenario_path.name}")
    print(f"  Payer: {payer} | Procedure: {procedure}")
    print(f"  Documents submitted: {len(submitted)}")
    print("-" * 78)
    print("EXECUTION TRACE")

    docs = load_documents()

    # ---- Step 1: retrieve the governing policy -------------------------
    status, matches = retrieve_policy(payer, procedure, docs)

    if status == "none":
        trace(f"no {payer} policy matched procedure '{procedure}'")
        trace("ESCALATING to human review: no governing policy found")
        print_escalation(scenario, [])
        return

    if status == "ambiguous":
        names = [f"{d['meta']['policy_id']} ({d['meta']['procedure']}, score {s})"
                 for s, d in matches]
        trace(f"retrieval returned {len(matches)} near-tied policy matches: "
              + "; ".join(names))
        trace("ambiguity detected -- the agent will NOT guess")
        trace("ESCALATING to human review")
        print_escalation(scenario, matches)
        return

    score, policy = matches[0]
    meta = policy["meta"]
    trace(f"retrieved {meta['payer']} policy {meta['policy_id']} "
          f"'{meta['procedure']}' (score {score})")

    # ---- Step 2: LLM decides whether an addendum is referenced ---------
    analysis = analyze_policy(policy["body"])
    required_docs = list(analysis["required_documents"])
    addenda_used = []

    if analysis["references_addendum"] and analysis["addendum_ids"]:
        for add_id in analysis["addendum_ids"]:
            trace(f"detected addendum reference {add_id} -> issuing second retrieval")
            addendum = retrieve_addendum(add_id, docs)
            if addendum is None:
                trace(f"WARNING: addendum {add_id} not found in /policies")
                continue
            trace(f"retrieved addendum {addendum['meta']['policy_id']} "
                  f"({addendum['path'].name})")
            add_reqs = analyze_addendum(addendum["body"])["required_documents"]
            trace(f"addendum adds {len(add_reqs)} required document(s)")
            required_docs.extend(add_reqs)
            addenda_used.append(add_id)
    else:
        trace("no addendum referenced -- proceeding with base policy requirements")

    trace(f"combined requirement list: {len(required_docs)} document(s)")

    # ---- Step 3: deterministic completeness check ----------------------
    check_result = completeness_check(required_docs, submitted)
    trace(f"completeness check: {check_result['missing_count']} missing of "
          f"{check_result['required_count']} required")

    # ---- Step 3b: deterministic financial impact -----------------------
    fin = financial_impact(meta, check_result)
    if fin["submission_status"] == "incomplete":
        trace(f"financial impact: ${fin['expected_cost_if_submitted_usd']:,} expected cost "
              f"if submitted as-is (${fin['reimbursement_at_risk_usd']:,} reimbursement at risk)")
    else:
        trace(f"financial impact: clean claim protects ${fin['reimbursement_at_risk_usd']:,} "
              "reimbursement from pend/denial exposure")

    # ---- Step 4: LLM drafts the final checklist ------------------------
    trace("drafting final checklist with risk assessment")
    result = draft_checklist(scenario, meta, addenda_used, check_result, fin)

    print("-" * 78)
    print(f"FINAL CHECKLIST  |  RISK FLAG: {result['risk_flag']}")
    for item in result["checklist"]:
        mark = "[x]" if item["status"] == "present" else "[ ]"
        note = f" -- {item['note']}" if item["note"] else ""
        print(f"  {mark} {item['item']}{note}")
    print("\nFINANCIAL IMPACT (deterministic tool -- synthetic benchmarks):")
    print(f"  Reimbursement at risk:      ${fin['reimbursement_at_risk_usd']:,}")
    if fin["submission_status"] == "incomplete":
        print(f"  Rework cost if pended:      ${fin['rework_cost_usd']:,}")
        print(f"  Expected admin-denial loss: ${fin['expected_admin_denial_loss_usd']:,} "
              f"({fin['admin_denial_rate_after_pend']:.0%} of reimbursement)")
        print(f"  Expected cost if submitted: ${fin['expected_cost_if_submitted_usd']:,} "
              f"(plus a {fin['pend_window_days']}-day pend delay)")
    else:
        print("  Pend/denial exposure:       $0 (submission is complete)")
    print(f"  Manual review replaced:     ${fin['manual_review_cost_replaced_usd']:,.2f} per submission")
    print(f"\nEXPLANATION: {result['explanation']}")
    if result["recommended_next_steps"]:
        print("NEXT STEPS:")
        for step in result["recommended_next_steps"]:
            print(f"  * {step}")
    print()


def print_escalation(scenario: dict, matches: list) -> None:
    print("-" * 78)
    print("FINAL DETERMINATION  |  ESCALATED TO HUMAN REVIEW")
    print(f"  The procedure '{scenario['procedure']}' could not be uniquely "
          f"matched to a single {scenario['payer']} policy.")
    if matches:
        print("  Candidate policies (near-tied retrieval scores):")
        for s, d in matches:
            print(f"    - {d['meta']['policy_id']}: {d['meta']['procedure']} "
                  f"(score {s})")
    print("  ACTION: route to a human intake specialist to confirm the intended")
    print("  procedure before any completeness determination is made.")
    print()


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Prior-Authorization Checklist Agent")
    parser.add_argument("scenario", nargs="*", help="path(s) to scenario JSON file(s)")
    parser.add_argument("--all", action="store_true",
                        help="run every scenario in /scenarios")
    args = parser.parse_args()

    if args.all:
        paths = sorted(SCENARIO_DIR.glob("*.json"))
    else:
        paths = [Path(p) for p in args.scenario]
    if not paths:
        parser.error("provide a scenario file or --all")

    for path in paths:
        try:
            run_scenario(path)
        except anthropic.APIError as e:
            print(f"\nAPI error while processing {path.name}: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
