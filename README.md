# Durable Lead Intake — Demo

An inbound-lead qualification agent (**real `gpt-5.5`** via the OpenAI Agents SDK) built
**four ways**, so you can show the observability and durability trade-offs live — a
progression from *no Temporal at all* to *the workflow fully orchestrating the agent*:

1. **Standalone Agent — no Temporal.** The plain Agents SDK drives the whole loop in an ordinary
   process. It works… until the process dies, at which point the run is **gone**: nothing
   resumes, no history, nothing to inspect. This is the "before".
2. **Simple Agent Loop — the agent inside one Activity.** One lead, one workflow, one
   opaque step. Crash mid-run → Temporal retries and it completes, but the agent restarts
   **from the beginning** and **re-spends every token** ("Wasted" jumps). Durable, but the
   agent is still a black box.
3. **Agent SDK Powered — each step is an Activity.** The `OpenAIAgentsPlugin` turns every
   model call and every tool call into its own named Activity, with inputs and outputs in
   the event history. Crash mid-run → resumes from the **last completed step**; the
   enrichment and verification calls already made are **not** repeated.
4. **Temporal-Native Loop — the workflow owns control flow, plus human review.** No agent
   framework. The workflow scores the lead, applies the thresholds itself, and **parks for
   a human** when a lead falls below the qualifying score (a Temporal **Update**). Fewest
   activities, lowest spend, full audit history, and the wait costs nothing.

Every variant calls the real model, and the only credential you supply is `OPENAI_API_KEY`
(a deployed instance also gets Temporal Cloud credentials from its platform). The tool
surfaces — **CRM**, **lead scoring**, **contact enrichment**, **email verification**,
**Slack** — are in-process and fixture-backed, so the demo runs fully offline apart from
the model. No vendor APIs, no vendor names in the code.

---

## Setup

### Prerequisites
- **Python 3.11+** (built and verified on 3.14)
- **Temporal CLI** (`temporal`) — https://docs.temporal.io/cli
- An **OpenAI API key**

```bash
python3 -m venv .venv
source .venv/bin/activate          # so `python` resolves to the venv interpreter
pip install -r requirements.txt    # or: pip install -e .
```

Then create `.env` with at least the key:

```bash
cat > .env <<'EOF'
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.5

# Local Temporal dev server
TEMPORAL_ADDRESS=localhost:7233
TEMPORAL_NAMESPACE=default
TEMPORAL_UI=http://localhost:8233
TASK_QUEUE_PREFIX=lead-intake
WEB_PORT=8000

# Temporal Cloud instead of the dev server: set both, and point TEMPORAL_ADDRESS at the
# namespace endpoint, TEMPORAL_NAMESPACE at the <namespace>.<account> form, and
# TEMPORAL_UI at https://cloud.temporal.io. Unset = plaintext local, the default.
# TEMPORAL_API_KEY=
# TEMPORAL_TLS=true

# Crash detection: a killed worker is noticed in ~2s
HEARTBEAT_TIMEOUT_SECONDS=2.5
HEARTBEAT_INTERVAL_SECONDS=1.0

# Routing thresholds (shown in the UI)
SALES_THRESHOLD=75
MARKETING_THRESHOLD=50
QUALIFY_THRESHOLD=25

# The lead that always parks for human review (see "Seeded leads")
ESCALATION_LEAD_ID=LEAD-4105
REVIEW_TIMEOUT_SECONDS=604800     # a week — so it never fires on stage

# Simulated latency of the email-verification provider
EMAIL_VERIFY_DELAY_SECONDS=3

# "Time Spend" in the UI is elapsed time x this. Display only — nothing simulated is
# written to the durable record. 20x makes a 30s run read as the ~10 minutes one lead
# really takes end to end against live vendors.
TIME_MULTIPLIER=20
EOF
```

> **Important:** run every process from the activated venv (or via `./.venv/bin/python`).
> The web server's supervisor launches the worker with `sys.executable`, so the worker
> inherits whichever interpreter started the web server.

## Run (two terminals + the UI-managed worker)

```bash
# 1) Temporal dev server (UI at http://localhost:8233)
temporal server start-dev

# 2) Web server — serves the UI + /api and OWNS the worker supervisor
python -m app.web.server          # http://localhost:8000
```

Open **http://localhost:8000**. The variant toggle (top-left) switches mode; for the three
Temporal variants the web server restarts the worker in that mode and on that variant's own
task queue. The web server stays up the whole time — the **worker** is the process the demo
kills and restarts.

```bash
# (optional) run a worker manually for debugging:
python -m app.worker.worker_main --variant agent_steps
#   choices: agent | agent_steps | orchestrated
```

> Restarting `temporal server start-dev` before the presentation gives you an empty
> workflow list, which makes the runs easier to find on screen.

## Killing and restarting the worker — one step

- **In the UI:** **Crash worker** (SIGKILLs the worker subprocess; it stays down) and
  **Restart worker**. That is the whole gesture; nothing else to type.
- On **Standalone Agent** the same button becomes **Kill process**, which cancels the in-flight
  run and discards its record — the point being that there is nothing to restart.
- Equivalent from a shell, if you would rather do it by hand:
  ```bash
  pkill -9 -f "app.worker.worker_main"                     # crash
  python -m app.worker.worker_main --variant orchestrated  # restart
  ```

---

## Seeded leads

| Lead | Source | What it is | Score | Outcome |
|---|---|---|---|---|
| **LEAD-4102** | inbound email | Northwind Retail — 41 sites, 850 staff, asking about multi-site licensing and rollout. **The primary demo lead.** | 87 | `route_to_sales` (ae.tan) |
| **LEAD-4103** | web form | Brightlark Software — engineer downloaded a guide, not evaluating yet | 62 | `route_to_marketing` |
| **LEAD-4104** | event CSV | Raman Consulting — badge scan, 2-person consultancy, no project | 38 | `route_to_nurture` |
| **LEAD-4105** | inbound email | 60 outpatient clinics, contract ends in January, asking about pricing — from a gmail address with no company name. **The review beat.** | 18 | **parks for human review** → archive, or override → `route_to_sales` |
| **LEAD-4101** | inbound email | "hi saw your website. can you send me info?" | 8 | `needs_info` |
| **LEAD-4106** | inbound email | Harborline Logistics — 320-vehicle RFP. **The automatic-retry beat.** | 81 | `route_to_sales`, after an injected retryable failure |

The score comes from a **deterministic** fixture-backed scoring service, and in Fully
Orchestrated the **workflow** (not the model) compares it to the thresholds. So the
escalation fires on every run — that beat does not depend on how the model feels today.
The model still does the real judgement work: whether there is enough to act on, and the
CRM write-up.

**LEAD-4105 vs LEAD-4102 for the review beat.** By default LEAD-4105 is the
below-threshold lead, so LEAD-4102 runs clean through variants 1–3 exactly as the talk
track describes. Set `ESCALATION_LEAD_ID=LEAD-4102` to switch to the tighter arc where the
*primary* demo lead is the one being mishandled — then variants 1–3 archive it as junk and
variant 4 catches it. Exactly one seeded lead ever escalates either way.

---

## Live demo script

### Act 1 — Standalone Agent: no safety net
1. Toggle to **Standalone Agent**. Submit **LEAD-4102**. It works — steps tick by in a plain
   in-memory list, and it routes to sales.
2. Run it again and hit **Kill process** mid-run. The run is gone: no workflow, no history,
   no checkpoint. Nothing to resume, and the tokens already spent are lost.
3. *Point at the Temporal UI:* there is nothing there, because nothing was ever recorded.

### Act 2 — Simple Agent Loop: durable, but a black box
1. Toggle to **Simple Agent Loop**. Submit **LEAD-4102**. In the Temporal UI, filter by
   task queue `lead-intake-agent` and open `lead-4102-agent`.
2. *Point at the history:* **one activity** — `run_lead_intake_agent`. Everything the agent
   does inside it is invisible.
3. Mid-run, **Crash worker**. Within ~2s Temporal notices the heartbeat stop and marks the
   activity for retry; the pending-activity panel shows the attempt count climbing.
4. **Restart worker**. The activity starts over **from zero** — watch **Wasted** jump. It
   still completes and routes to sales.

### Act 3 — Agent SDK Powered: the observability payoff
1. Toggle to **Agent SDK Powered**. Submit **LEAD-4102**. Filter by
   `lead-intake-agent-steps`, open `lead-4102-agent-steps`.
2. *This is the payoff if you have ever debugged an orchestrator that only logs its own
   steps:* the history is
   an itemised list of named steps — `fetch_lead_details`, `check_duplicate_contact`,
   `score_lead`, `enrich_contact`, `verify_email`, `update_crm`, `route_to_sales`,
   `notify_slack` — each with its **inputs and outputs**, interleaved with a model-call
   activity per turn. Click into `enrich_contact` and read the company record back.
3. Crash at the same point and **Restart**. It resumes from the **last completed
   activity**: the enrichment and verification calls already made are not repeated, and
   **Wasted** stays small. Same destination.

### Act 4 — Temporal-Native Loop: the human in the loop
1. Toggle to **Temporal-Native Loop**. Submit **LEAD-4105**. Filter by
   `lead-intake-orchestrated`, open `lead-4105-orchestrated`.
2. *Count the activities* — a handful, all business steps, and the model is called only
   where judgement is needed.
3. The lead scores **18** against a qualifying threshold of **25**, so the workflow
   **parks** and the review modal appears with the lead's actual message. Read it out: 60
   clinics, a contract ending in January, asking about pricing. *"The scorer called this
   junk because it came from a gmail address."*
4. *Point at the Temporal UI:* the workflow is running and waiting — a timer, no pending
   activity, no polling, nothing burning. It could sit here for a week.
5. **Crash worker** while it is parked. Nothing is lost — it is still waiting. **Restart
   worker**.
6. Click **It's a real lead → route to sales**. That is a Temporal **Update**: it is
   accepted synchronously (the workflow's validator would have rejected it if this lead
   weren't awaiting review), and the workflow continues immediately through enrichment,
   verification, the CRM update and the Slack post.
7. Optional second crash: hit **Crash worker** right after the override, while enrichment
   or verification is running, then **Restart**. It picks up where it left off — the human
   decision is durable state, so nobody gets asked twice.

### Act 5 — automatic retry, no human anywhere
- Submit **LEAD-4106** on any Temporal variant. The routing step throws a retryable
  failure on its first attempt (an "enrichment provider 503"). Temporal retries it
  automatically; the history shows two attempts and the lead still routes to sales.
- On **Agent SDK Powered** / **Temporal-Native Loop** the expensive reasoning is already
  banked in completed activities, so the retry costs almost nothing. On **Agent as
  Workflow** the same failure re-runs the entire agent — a second, cheaper way to make the
  same cost point.
- **Do not** crash the worker during a LEAD-4106 run: the injection counter is in-memory
  and would reset, re-injecting the failure. Keep the two beats separate.

### Act 6 — the hour-long third-party call (optional)
Their email-verification provider can take 30–60 minutes. To show what that looks like:

```bash
EMAIL_VERIFY_DELAY_SECONDS=1800 python -m app.web.server
```

On **Temporal-Native Loop** the wait is a **durable workflow timer**: submit the request,
park, collect the result. The Temporal UI shows the workflow simply waiting; there is no
polling loop and no second pipeline stitched to a queue listener. Crash and restart the
worker mid-wait — the timer survives, and the wait does not start over. (In the
agent-driven variants the same call is time spent inside one activity, so a crash there
restarts the wait — another instance of the same trade-off.)

---

## What to expect in the Temporal UI

| | Simple Agent Loop | Agent SDK Powered | Temporal-Native Loop |
|---|---|---|---|
| Task queue | `lead-intake-agent` | `lead-intake-agent-steps` | `lead-intake-orchestrated` |
| Workflow type | `LeadIntakeAgentWorkflow` | `LeadIntakeAgentStepsWorkflow` | `LeadIntakeOrchestratedWorkflow` |
| Workflow id | `lead-4102-agent` | `lead-4102-agent-steps` | `lead-4102-orchestrated` |
| Activities per run | 1 | ~20 (model + tool calls) | **11** on the sales path, 12 through review |
| Crash recovery | whole agent re-runs | last completed step | last completed step |
| Waiting state | — | — | timer + parked update handler |

Re-running the same lead on the same variant while the first run is still open appends
`-2`, `-3`, … so ids stay readable and nothing silently collides.

---

## Verifying it works

```bash
# 1) Model-free: the whole Temporal-Native Loop control flow, the deterministic escalation,
#    the review Update (accept + reject), and the durable timer. Seconds, no tokens.
python -m tests.test_orchestration

# 2) Earliest real-model check (one lead, no Temporal — verifies key, model, usage capture)
python -m tests.check_model LEAD-4102

# 3) Full smoke test against the real model, per variant (spends tokens)
python -m tests.smoke_test orchestrated
#   choices: agent | agent_steps | orchestrated
```

## How it works (one paragraph)

The web server starts a Temporal Workflow per lead (or, for Standalone Agent, runs the agent in
an in-process task) and reads workflow state via Queries and event history via its own
client — so the token meter and the flow diagram keep updating **even while the worker is
dead**, which is what lets the UI narrate the crash. In Agent SDK Powered the
`OpenAIAgentsPlugin` turns every model call into an Activity and each tool call is an
`activity_as_tool` Activity. Temporal-Native Loop uses no agent framework: the workflow calls
plain data activities plus two model activities, applies the score thresholds itself, and
parks on `wait_condition` for a `lead_review` **Update** when a lead is below the
qualifying score. Tokens are counted into **New** (banked by completed work) and **Wasted**
(burned by attempts killed before completing); **Total = New + Wasted** (Standalone Agent has no
Wasted bucket — a dead process loses the whole run).

## Notes & limitations

- Tool calls return canned fixtures; mutations are in-memory and reset when the worker
  restarts. Durable state lives in Temporal — which is the point.
- The **lead score is deterministic per lead**, on purpose: the human-review beat cannot
  depend on the model happening to score low on a given run. The scoring service is a stub;
  the model does the surrounding judgement. Say so if anyone asks — it is the honest
  framing, and it is why the numbers you quote hold across runs.
- The token meter prefers real usage from each model call (`include_usage=True` /
  `response.usage`) and falls back to a `tiktoken` estimate (shown with `~`) if usage is
  missing, so it is never blank on stage.
- **Token Spend is measured. Time Spend is simulated** — elapsed time x `TIME_MULTIPLIER`,
  split New/Wasted in the same proportion as the tokens, and labelled `x20 simulated`
  beside the real stopwatch. Be straightforward about that if asked: the point it makes
  (a crash costs wall-clock, not just tokens) is real, the number is illustrative. Nothing
  simulated reaches the durable record.
- Runs against a local dev server by default: leave `TEMPORAL_API_KEY` and `TEMPORAL_TLS`
  unset and the client connects in plaintext to `TEMPORAL_ADDRESS`, exactly as it always
  has. Set both (the deployed demo does) and the same code connects to Temporal Cloud —
  there is no separate Cloud code path. `TEMPORAL_NAMESPACE` is then the fully-qualified
  `<namespace>.<account>` form, and `TEMPORAL_UI=https://cloud.temporal.io` makes the
  in-app workflow links point at the Cloud UI.
- A Temporal **Update** needs a live worker to be accepted, so if you crash the worker
  while parked at the review gate, restart it before clicking the review button. The UI
  refuses with a directive message rather than leaving you on a spinner. (Measured
  behaviour, if anyone asks: an Update sent while the worker is down is still *admitted* by
  the server and applied the moment a worker returns — it is not lost. What it cannot do
  is acknowledge back to the caller, which is why the UI declines to send it.)

## Repo guide

```
app/config.py            env, variant slugs, task queues, thresholds, owners
app/shared/              lead model, seeded leads, fixtures, prompts, the agent, tokens
app/agent_only/          the no-Temporal baseline runner
app/activities/          the agent activity, the tool activities, the model activities
app/workflows/           one module per Temporal variant
app/worker/              the killable worker + the supervisor that owns it
app/web/                 FastAPI app, /api, the Temporal client, the static UI
tests/                   test_orchestration (no key needed), check_model, smoke_test
```
