"""Canonical stage ids. The server reports the current `stage`; the UI uses it for the
(secondary) stepper. The per-variant flowcharts carry the primary live narrative.

Fully Orchestrated walks the reference flow:

    intake -> qualify -> scoring -> (review) -> (enrich) -> (verify) -> route -> done

The agent-driven variants (Agent as Workflow, Agent Steps) free-run, so they report just
`running` then `done`.
"""

INTAKE = "intake"          # fetch the lead + check the CRM for an existing contact
QUALIFY = "qualify"        # is there enough here to act on? (model judgement)
SCORING = "scoring"        # deterministic lead score
REVIEW = "review"          # human lead review (Fully Orchestrated only)
ENRICH = "enrich"          # enrichment provider: company data
VERIFY = "verify"          # email-verification provider (the slow dependency)
ROUTE = "route"            # route + update CRM + notify Slack
DONE = "done"

RUNNING = "running"  # agent-driven variants (no workflow-visible stages)

# Full ordered path the UI renders for Fully Orchestrated; branches mark skipped stages.
ORDER = [INTAKE, QUALIFY, SCORING, REVIEW, ENRICH, VERIFY, ROUTE, DONE]
START = INTAKE
