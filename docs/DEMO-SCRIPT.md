# 5-Minute Presentation — Demo Script & Run-of-Show

**Deck:** `Presentation-Prior-Auth-Agent.pptx` (speaker notes carry the same timings)
**Demo URL:** https://kilgorec1024.github.io/prior-auth-agent-demo/

## Before class (10 minutes, do not skip)

1. Open the demo URL in a browser tab **and run S1 once** — this confirms the token,
   VPN (if using IU), and API are all working *today*, and warms you up.
2. Leave the tab open, zoomed to ~125% so the back row can read the trace.
3. Record the backup video (below) if you haven't already.
4. Presenter view on; phone timer at 5:00.

## Run of show

| Clock | Slide | What you do / say |
|---|---|---|
| 0:00–0:30 | 1 — Title | Hook: "Every late insurance approval started as a paperwork problem. We built an agent that catches it — and prices the mistake it just prevented." |
| 0:30–1:15 | 2 — Problem | AMA numbers. Land on: a quarter of denials are *administrative* — rule-bound, high-volume, expensive when wrong. Perfect agent territory. |
| 1:15–2:00 | 3 — Loop | Walk left to right. Punch: step 2 is a *decision*, not a lookup — and the off-ramp refuses to guess, at zero token cost. |
| 2:00–4:00 | 4 — DEMO | Switch to browser. **Beat 1:** click preset **S3** → Run. While the yellow line pauses: "that pause is the model reading the policy — right now." Point at the addendum detection. Land on **HIGH / $4,684 vs $58,000**. **Beat 2:** check the two missing boxes → Run → **LOW / $0**. **Beat 3:** type *"Why is the frailty assessment required?"* → read the grounded answer. |
| 4:00–4:30 | 5 — Design | "The LLM never grades its own homework — and never invents its own numbers." Scaling line: policy #7,000 costs a data file, not a parser. |
| 4:30–5:00 | 6 — Close | $44 vs $4,684. Free on the IU hub. Name the next steps. Stop. |

## Timing discipline

- If the demo runs long, **cut Beat 2** (the re-run) — Beats 1 and 3 carry the wow.
- If an LLM call is slow, keep talking over the pause — the yellow trace line IS the content.
- Hard rule: at 4:30 you are on slide 6 no matter what.

## Backup video (already on the Desktop)

Two versions exist, both real recorded runs against the live site:

- **`demo-backup-narrated.mp4`** (102 s, AI voice-over) — fully self-running: plays
  all three beats with synchronized narration. Use this if the live demo fails, or
  even show 20 seconds of it as a flex ("the agent also narrates its own demo").
- **`demo-backup.mp4`** (68 s, silent) — narrate over it yourself, matching the
  beats above.

If the live demo fails twice, say "let me show you this morning's run" and play one —
do not debug on stage. Test audio output in the room beforehand if using the narrated cut.

## 90-second backup recording — beat sheet (silent; team narrates live)

Record with Snipping Tool video mode (Win+Shift+S → camera icon → drag region around the
browser content). Normal window (not incognito — token lives in localStorage), one warm-up
S1 run first, bookmarks bar hidden (Ctrl+Shift+B), zoom ~110–125%, Do Not Disturb ON.
Move the cursor slowly — it is the narrators' pointer.

| Time | On screen | Narrator beat |
|---|---|---|
| 0:00–0:12 | Slow scroll of left panel: payer → procedure → doc checkboxes | The controls |
| 0:12–0:20 | Click preset S3; hover the 4 pre-checked boxes | A real TAVR submission |
| 0:20–0:50 | Click Run; cursor near the yellow trace lines during pauses | "Yellow = the model reading; it found the addendum on its own" |
| 0:50–1:10 | Scroll result: missing items → HOLD on financial panel | "$4,684 caught on $58,000 at risk — computed, not guessed" |
| 1:10–1:25 | Preset S5 → Run → instant escalation card | "Not sure? It refuses to guess — zero AI cost" |
| 1:25–1:30 | Hold on URL | "Live at this link" |

Skip the fix-and-rerun beat — a second LLM run doesn't fit in 90s; S5 is instant.
Rehearse the click-path twice, record aiming ~80s, trim in Clipchamp. Narrators watch
the final cut once so their beats match the real pacing.

## Likely Q&A (after the 5:00)

**Meta-move available:** the page now has an "Ask about this project" chatbot at the
bottom — grounded in a fact sheet about the architecture, file layout, testing, and
security. If a question stumps you, type it into the page live ("let's ask the project
itself") — it's a memorable close. It answers from facts, and says so when it doesn't know.

- **"How do you know the model isn't hallucinating the verdict?"** — It can't. The
  missing-document list and every dollar come from deterministic code; the model only
  explains them. That's the core design decision.
- **"What about real policies — they're PDFs, not markdown?"** — Reading messy prose
  is exactly what the LLM step is for; PDF ingestion is our first named next step. The
  loop doesn't change.
- **"Is patient data safe?"** — There is none: the corpus is 100% synthetic. In
  production the corpus is *policies* (payer documents), not patient records; the
  submission checklist is document *names* only.
- **"Why not n8n / no-code?"** — Auditability and control of the loop; and the
  serverless browser design (required by the IU VPN) isn't expressible in hosted
  workflow tools.
- **"What did the LLM get wrong along the way?"** — Great question for us: our fuzzy
  matcher once mis-paired two documents, and the *model caught it* in its explanation
  before we fixed the code. Deterministic core + narrating model = errors get noticed.
