---
title: Dashboard
---


# MyThingsLab fleet dashboard

## Development harness

| Tool | Purpose | CI | Issues | PRs | Last activity |
| --- | --- | --- | --- | --- | --- |
| [fleet-dispatch](https://github.com/MyThingsLab/fleet-dispatch) | _(no purpose seam found)_ | ✅ | 12 | 1 | — |
| [my-dashboard](https://github.com/MyThingsLab/my-dashboard) | Renders the fleet's status two ways — an org-wide front page | ✅ | 0 | 0 | build: implemented org-wide render (shelves.toml, gh status, staleness-hash PR) and single-repo status card (local mode) (2026-07-09T02:10:04Z) |
| [my-orchestrator](https://github.com/MyThingsLab/my-orchestrator) | reads every backlog across the fleet — open issues per repo, and | ✅ | 0 | 0 | build: wire MyPlanner kind=plan signal into ranking (horizon boosts + pause-new-tools penalty); add my-projector/my-planner to manifest (2026-07-07T19:04:39Z) |
| [my-things-core](https://github.com/MyThingsLab/my-things-core) | _(no purpose seam found)_ | ✅ | 4 | 0 | build: append-only JSONL ledger — the shared-memory contract every tool writes to (2026-07-06T11:57:24Z) |
| [my-projector](https://github.com/MyThingsLab/my-projector) | keep the fleet's GitHub Project board (and any linked org-wide | ✅ | 0 | 0 | build: MyProjector v0: board sync, drift detection, checklist, tiered policy (2026-07-07T18:52:44Z) |
| [my-docs](https://github.com/MyThingsLab/my-docs) | keeps `MyThingsLab/mythingslab.github.io` — the fleet's | ✅ | 0 | 0 | scaffold: copied my-template for MyDocs (2026-07-07T23:03:31Z) |
| [my-todo](https://github.com/MyThingsLab/my-todo) | curate a `TODO.md` — a repo's own (`mytodo repo`) or an org-wide | ✅ | 0 | 0 | build: implemented plan/sources/render/prioritize/curator/cli; 23 tests green (ruff+pytest, incl GITHUB_ACTIONS=true); dogfooded org roll-up over live org issues (2026-07-08T09:15:49Z) |
| [my-changelogger](https://github.com/MyThingsLab/my-changelogger) | reads a target repo's `dev-ledger/` entries since the last | ✅ | 0 | 0 | ship: pushed to github.com/MyThingsLab/my-changelogger; PR #1 opened, CI green on first push (2026-07-07T02:57:36Z) |
| [my-reporter](https://github.com/MyThingsLab/my-reporter) | reads the shared `Ledger` and each repo's `dev-ledger/`, and | ✅ | 0 | 0 | build: Add --engine-model flag: build_engine(name, model) factory replaces the type[Engine] map so --engine claude-cli can pick a model (issue #7) (2026-07-07T13:06:53Z) |
| [my-tester](https://github.com/MyThingsLab/my-tester) | runs pytest with coverage, finds one uncovered unit, and opens a | ✅ | 0 | 0 | fix: live-verified --engine claude-cli against a real scratch repo: the model's reply arrived wrapped in a markdown code fence (```python ... ```), which failed ast.parse. Added an explicit no-fences instruction to _build_prompt plus a defensive _strip_code_fence() applied to any non-empty reply before appending -- verified end-to-end afterward: a real generated test (test_multiply) landed unfenced and valid (2026-07-07T04:35:39Z) |
| [my-guard](https://github.com/MyThingsLab/my-guard) | the rule engine — evaluates an `Action` to allow / ask / deny, | ✅ | 0 | 0 | build: local-first gate: pre-commit (ruff + pytest-fast) + slow marker (2026-07-06T13:08:45Z) |
| [my-template](https://github.com/MyThingsLab/my-template) | <one line — what this tool does> | ✅ | 0 | 0 | scaffold: scaffold my-template: canonical My[X] tool template — verbatim-shared seams (pyproject, ci.yml, gitignore, LICENSE, pre-commit, vendored HARNESS.md, drift test, dev-ledger) with 'template' placeholder; installs and passes ruff+pytest green out of the box (2026-07-06T16:52:05Z) |
| [my-planner](https://github.com/MyThingsLab/my-planner) | produce and maintain a priority-ordered, multi-item plan — "here's | ✅ | 0 | 0 | build: MyPlanner v0: context assembly + truncation, required Engine call, kind=plan ledger, tracking-issue section upsert (2026-07-07T18:58:19Z) |

## Services

| Tool | Purpose | CI | Issues | PRs | Last activity |
| --- | --- | --- | --- | --- | --- |
| [my-server](https://github.com/MyThingsLab/my-server) | a small HTTP server that surfaces the fleet over HTTP — starting | ✅ | 0 | 0 | feature: HTTP/JSON API over the tools: GET /tools, GET /tools/{name}, POST /tools/{name}/issues (enqueue labeled backlog issue, fail-closed bearer-token gated); + coverage step (stale-template fix). 29 tests green (2026-07-08T00:22:37Z) |
| [my-telegram-bot](https://github.com/MyThingsLab/my-telegram-bot) | bridges the harness to the user over Telegram: pushes ledger | ✅ | 0 | 0 | ship: live end-to-end ask-flow verified: real Telegram message sent, user tapped Allow, TelegramPolicy.evaluate() resolved decision=allow in ~3.5s via manual-ask-test.yml workflow_dispatch run 28840280628. Confirms the getUpdates client-timeout fix (PR #5) actually closed the loop, not just passed unit tests (2026-07-07T03:54:38Z) |
| [mythingslab.github.io](https://github.com/MyThingsLab/mythingslab.github.io) | _(no purpose seam found)_ | ✅ | 0 | 0 | — |

## Casual development

| Tool | Purpose | CI | Issues | PRs | Last activity |
| --- | --- | --- | --- | --- | --- |
| [my-uni](https://github.com/MyThingsLab/my-uni) | given a "field of study" issue, decomposes the field into a | ✅ | 1 | 1 | — |
| [my-idea](https://github.com/MyThingsLab/my-idea) | _(no purpose seam found)_ | — | 0 | 1 | — |
| [my-presentation](https://github.com/MyThingsLab/my-presentation) | given a talk issue labeled `my-presentation` (topic, audience, | ✅ | 0 | 0 | — |
| [my-typster](https://github.com/MyThingsLab/my-typster) | given a document-drafting issue labeled `my-typster` (a report, | ✅ | 0 | 0 | build: Typster: issue-driven Typst drafting, compile gate, personal-kind fence, presentation hand-off (2026-07-07T23:38:23Z) |
| [my-searcher](https://github.com/MyThingsLab/my-searcher) | indexes a repo's files and ranks the ones most relevant to a | ✅ | 0 | 0 | scaffold: copied my-template, implemented v0 per docs/tools/my-searcher.md (2026-07-08T10:02:28Z) |
| [my-site](https://github.com/MyThingsLab/my-site) | given a content or design-change issue (label `my-site`) on a | ✅ | 0 | 0 | scaffold: copied my-template for MySite (2026-07-07T23:03:02Z) |
| [my-librarian](https://github.com/MyThingsLab/my-librarian) | given a task issue ("convert Markdown to HTML", "typeset a | ✅ | 0 | 0 | scaffold: copied my-template; built registries/recommend/librarian/cli (2026-07-08T17:37:15Z) |
| [my-scraper](https://github.com/MyThingsLab/my-scraper) | given a URL and a question, fetch the page deterministically and | ✅ | 0 | 0 | build: implemented extract: fetcher + scraper + cli, 23 tests green (2026-07-08T17:25:20Z) |
| [my-researcher](https://github.com/MyThingsLab/my-researcher) | given a topic issue, discovers external sources **live** (web | ✅ | 0 | 0 | scaffold: Scaffolded my-researcher from my-template and implemented the brief+plan tool (live arXiv/Tavily retrieval, one-Engine-call synthesis, Workspace PR + guarded comment paths, ledger); 23 tests passing, ruff clean. (2026-07-07T19:57:01Z) |
| [my-archivist](https://github.com/MyThingsLab/my-archivist) | maintains a unified catalog of a personal book/materials | ❌ | 0 | 0 | scaffold: copied my-template; built scanner/enrich/catalog/classify/render/archivist/cli (2026-07-08T17:58:02Z) |

## Unshelved

- .github
- my-coder
- study
- typst-personal-docs
- typst-templates
