# CLAUDE.md

**team-group-bot** — Telegram bot (MT Messaging Bot) that registers the group chats it is added to, lets admins sort groups into categories from a private-chat menu and broadcast messages to a category, with group/user statistics and broadcast history.

## Agents — Orchestrator Flow

**The main thread is an ORCHESTRATOR. It does NOT edit source code itself — ALL code changes go through the `developer` agent.**

**Rules:**
- **Any change to source files (backend, frontend, CSS, configs, migrations, tests) → `developer`.** Regardless of size — even a one-line fix. Frontend is NOT an exception.
- **Orchestrator inline only:** plans, reviews, verification commands, docs it owns (`docs/*.md`, `.claude/**`, CLAUDE.md), and prompts/reports.
- **`architect` first** for features, schema/migration changes, multi-module refactors. Skip architect for small bounded edits — go straight to developer with exact instructions.
- **`tester` after developer** for backend/logic changes. Verify: `.venv/bin/python -m pytest -q tests/<module>.py` (real Postgres: `docker run -d --name tgb-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=tgb_test postgres:16-alpine`, `TEST_DATABASE_URL` overrides, db name must contain `test`; deps: `.venv/bin/python -m pip install -r requirements-dev.txt`) — **only the affected modules**.
- **Full test suite — only with the user's explicit OK:** ask each time; suggest it when changes are many and overlapping, or right before a commit. Mass DB-connection errors are infrastructure: say it in one line, don't re-run the suite in a loop.
- **Plans** go to `.claude/plans/`; before implementing a non-trivial plan run `/review-plan` — it calls `plan-reviewer`, whose report is written for the product owner (plain language, no internal names; only critical `.env` variable names).

| Agent | Role |
|-------|------|
| `architect` | Technical plan BEFORE implementation: affected files, schema/contract changes, implementation order, risks. For features/schema/multi-module only. |
| `developer` | Implements against the plan. Stack: Python 3.11, aiogram 3.4.1 (MemoryStorage FSM), FastAPI 0.115 + uvicorn 0.30 (webhook only), SQLAlchemy 2.0 async + asyncpg 0.30 (PostgreSQL 16 container in the same compose; aiomysql/PyMySQL only for the one-off `scripts/mysql_to_pg.py`), Alembic 1.15, python-dotenv |
| `tester` | After every developer pass: writes/runs tests, verifies. On failure — reports exactly what broke; orchestrator sends it BACK to developer. Loop until green. |
| `plan-reviewer` | Independent review of a plan BEFORE implementation: verifies claims against code, money, data, risks. Reports in plain Ukrainian for the product owner — no abbreviations, no file/function names, only critical `.env` variable names. |
| `Explore` | Read-only codebase research when scope is unclear. |

**Flow:** task → `architect` (plan) → `plan-reviewer` (non-trivial plans) → `developer` (code) → `tester` (verify) → if failed: `developer` again with tester's findings → repeat until green → orchestrator reports.

## The Project Map Is the Graph — Keep It Fresh

No `docs/ARCHITECTURE.md`: the map is the graph in `graphify-out/` (query/update rules — global CLAUDE.md). After deleting code — `graphify update . --force`. Non-trivial decisions — `impl-*` skills.

Facts the graph does not carry — keep them accurate in `.claude/agents/*.md` when they shift:
- current scope: auto-registration of groups when the bot joins/leaves, private-chat admin menu (admins, groups and categories, bulk move of unspecified groups, show, broadcast to a category, statistics), group-side admin update menu, periodic bot-membership health check, capture of all group messages (text + type, 180 d / keep last 100 per chat), remote MCP endpoint for Claude Code with per-key category scope, audit log and admin key UI (see `impl-mcp-chat-archive` skill); new things only on the user's explicit request, nothing on top;
- the set of env params (source of truth: `os.getenv` + python-dotenv in `bot/main.py`, `bot/data/db_connection.py`, `alembic/env.py`, examples in `.env.example`);
- how the project is run / deployed: Docker compose service `team-group-bot` (image `artemqq5/team-group-bot:latest`, port 8019, external `nginx-network`, PostgreSQL 16 service `postgres` / container `team-group-bot-db`, data in `./pgdata`, runbook `docs/postgres-cutover.md`); `entrypoint.sh` runs `alembic upgrade head` then `uvicorn bot.main:app --host 0.0.0.0 --port 8019`; GitHub Actions builds and pushes `:latest` + `:<sha>` on push to main only;
- the verification commands (test runner, current test count — see `tester.md`).

## Implementation notes — document the interesting parts as skills

After every non-trivial implementation (novel algorithm, non-obvious design decision, tricky integration, important tradeoff), the orchestrator creates `.claude/skills/impl-<topic>/SKILL.md` in the SAME session, following the template at `.claude/templates/impl-note.md`.

- Notes are in English; code and identifiers stay as-is.
- The `description` frontmatter must carry the trigger keywords (file names, concepts, related tasks) so the skill auto-loads when relevant.
- One topic = one skill. If a note for the topic already exists — update it; never create `impl-<topic>-2`.
- Trivial changes (CRUD, config edits, renames) do NOT get a note — a note that restates the code is noise.
- The user can also explicitly ask to document anything.

## Communication

`.caveman.json` pins caveman mode `full` (rules come from the caveman plugin). Commits, PRs and security warnings — normal prose.
