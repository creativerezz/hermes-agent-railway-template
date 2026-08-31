# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **Railway deployment template** for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research). It contains no agent code of its own. It is a thin, opinionated shim
around a *pinned upstream release*: a Dockerfile that installs Hermes at
`ARG HERMES_REF`, a boot script that prepares the persistent volume, and a single
Python process (`server.py`) that supervises two Hermes subprocesses and puts one
password-protected front door in front of both.

Practically every non-obvious line in this repo exists because of a specific upstream
behaviour at a specific Hermes version. That framing drives everything below.

Whole repo, in full:

| File | Role |
|---|---|
| `server.py` (~3.1k lines) | The entire application. Admin API, auth, reverse proxy, subprocess supervision. |
| `templates/index.html` (~2.6k lines) | The whole admin UI — one Jinja template, Alpine.js from CDN, inline CSS/JS. No build step. |
| `Dockerfile` | Pins `HERMES_REF`, installs Hermes + extras, pre-builds its React dashboard and TUI bundle. |
| `start.sh` | Volume prep, install-method stamp, stale-lock cleanup, then `exec python /app/server.py`. |
| `CHANGELOG.md` | Per-release notes; the source of truth mirrored into the UI's *What's New* panel. |
| `railway.toml` | Dockerfile builder, `/health` healthcheck, `tini -g -- /app/start.sh`. |

## Commands

There is **no test suite, no linter, no formatter, and no CI**. Verification is: does the
image build, and does the container come up. Do not invent `pytest`/`ruff` invocations.

```bash
# Syntax smoke check (fast, the only thing runnable without Docker)
python3 -m py_compile server.py

# Full build — clones Hermes at HERMES_REF, npm-builds web/ and ui-tui/. Slow (~5-15 min cold).
docker build -t hermes-agent .

# Build against a different upstream release without editing the Dockerfile
docker build --build-arg HERMES_REF=v2026.8.13 -t hermes-agent .

# Run locally — needs a volume, or config vanishes between runs
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme \
  -v hermes-data:/data hermes-agent
# → http://localhost:8080, log in as admin / changeme
```

`server.py` and `templates/` are the **last** `COPY` layers, so edits to them rebuild in
seconds against a warm cache. Editing the Dockerfile above that line costs a full rebuild.

Running `python server.py` on the host does not work in practice — it shells out to a
`hermes` CLI that only exists inside the image.

## Architecture

One container, one public listener, two managed children:

```
$PORT (0.0.0.0)  ──  server.py   Starlette + Uvicorn, the ONLY public surface
                       ├── /health, /login, /logout        public / auth
                       ├── /setup, /setup/api/*            this template's wizard + API
                       └── / and /*                        reverse proxy ───┐
                                                                            │
                       ├── `hermes dashboard`  127.0.0.1:9119  ◄────────────┘  (never public)
                       └── `hermes gateway run`  the agent itself (Telegram, Discord, …)
```

Both children are spawned by `server.py` and both get their environment from
`build_hermes_env()`. State lives on the Railway volume at `HERMES_HOME=/data/.hermes`
(`.env`, `config.yaml`, `auth.json`, sessions, pairing, backups). The container image
itself is immutable and rebuilt on every deploy — nothing installed at runtime survives.

### Invariants

These are load-bearing; code comments cite them **by number** (grep `invariant` in
`server.py`), so keep the numbering stable. Numbers 3, 4, 6 and 8 are cited directly.

1. **`server.py` is the only thing bound to `0.0.0.0`.** Everything else is loopback.
2. **All durable state is on `/data`, none in the image.** Any runtime install
   (Hermes' memory-provider / toolset setup endpoints) is temporary — hence the
   `IMMUTABLE_INSTALL_WARNING_JS` confirm dialog injected into proxied pages.
3. **The Hermes dashboard is never exposed directly** — loopback bind, reached only
   through our proxy, so one login covers both UIs.
4. **The install method is stamped `docker` in two places** (`/opt/hermes-agent/.install_method`
   in the Dockerfile, `$HERMES_HOME/.install_method` in `start.sh`) so Hermes' own
   "Update" button correctly refuses. Upgrades happen by bumping `HERMES_REF` and redeploying.
5. **`config.yaml` is deep-merged, never overwritten** (`write_config_yaml`). It carries
   user- and cron-managed sections — most importantly `mcp_servers`, which Hermes reads
   *only* from this file. Clobbering it silently deletes people's MCP servers.
6. **The gateway is supervised, and stale runtime files are cleared before it starts.**
   `start.sh` removes `gateway.pid` / `.lock` / `.sock` (they survive on the volume);
   `Gateway` respawns with a crash-loop budget (`RESPAWN_MAX_IN_WIN` in
   `RESPAWN_WINDOW_S`) and starts with `--replace --external-supervisor`. Railway will
   never restart the container for a dead gateway, because `server.py` stays healthy.
7. **Every Hermes subprocess is spawned with `build_hermes_env()`.** `.env` values win
   over Railway service variables. A subprocess started with an inherited env only sees
   boot-time state and will never see a key saved later through the wizard — which is
   why `Dashboard.restart()` is called on every provider save, not just the gateway's.
8. **Hermes' own dashboard auth gate is satisfied, not dodged.** Since v2026.8.27 a
   non-loopback `dashboard.public_url` engages that gate, and an unsatisfied gate makes
   the dashboard `SystemExit` at boot (with no respawn supervisor — every proxied page
   503s while `/setup` and `/health` stay green). `build_hermes_env()` therefore sets
   `HERMES_DASHBOARD_PUBLIC_URL` *only* together with the basic-auth credentials, and
   `HermesSession` signs in on the user's behalf so no second login is ever shown.
   The public URL is also what MCP OAuth `redirect_uri`s are built from — suppressing
   it breaks every OAuth MCP server, silently.

### Auth model

Cookie-based, HMAC-signed, 7-day, httponly, enforced **per-handler via `guard()`** — no
middleware, so `/health` and `/login` stay genuinely open. Basic auth was dropped because
the Hermes React SPA's bare `fetch()` calls don't reliably carry it. The signing secret is
regenerated each process start, so changing `ADMIN_PASSWORD` (which redeploys) signs
everyone out. Our sign-in lives at **`/setup/login`**, not `/login`: gated Hermes redirects
to `/login`, and a real page of ours there produced an infinite browser redirect loop.

Server-to-server calls into the dashboard (only `set_active_model_via_hermes()` — every
other call goes through the proxy) carry credentials for whichever mode the dashboard is
in: **ungated**, Hermes' per-process session token, scraped from the SPA shell by
`_get_hermes_session_token()`; **gated**, the `HermesSession` cookies *instead* — `GET /`
302s to the login page, so there is no shell to scrape and the token comes back `""`.

### The reverse proxy

`_proxy_to_dashboard()` forwards everything unmatched, stripping only `host` and
`transfer-encoding` — `authorization` and `cookie` must pass through. HTML responses get
`BACK_TO_SETUP_WIDGET` + `IMMUTABLE_INSTALL_WARNING_JS` injected before `</body>`.

WebSockets are a **fail-closed allowlist** (`PROXIED_WS_PATHS`): `/api/pty`, `/api/ws`,
`/api/events`, `/api/console`, and the `/api/plugins/*` prefix. A new Hermes release that
adds a socket outside these will 403 at our edge until it is listed. `/api/pub` is
deliberately excluded. `HERMES_WS_MAX_BYTES` must stay ≥ Hermes' own `ws_max_size` on both
hops or frames vanish mid-message with nothing in the logs.

## Working in this codebase

### Comment style is part of the contract

Comments here are long, and they explain *why*, cite the upstream file
(`hermes_cli/web_server.py`, `gateway/run.py`, …) and the version the behaviour was
verified at. That is the deliberate house style — this code is otherwise unreadable
without it, because most of it is compensating for upstream behaviour you cannot see from
this repo. Match it. When you correct a stale comment, say so in the comment (see the
`HERMES_PROVIDER_IDS` "CORRECTION (v2026.8.27 audit)" block for the established form)
rather than quietly deleting the wrong claim.

### Adding an env var / provider / channel

`ENV_VARS` in `server.py` is the single registry: `(key, label, category, is_secret)`.
Touch every one of these or the change is silently incomplete:

1. `ENV_VARS` — add the entry. **The category must already exist in `write_env()`'s
   `cat_order`**, or the value is dropped from `.env` with no error.
2. `HERMES_PROVIDER_IDS` — map the key to Hermes' *own* canonical provider id from
   `hermes_cli/auth.py`'s `PROVIDER_REGISTRY`. Several are non-obvious renames
   (`dashscope→alibaba`, `glm→zai`, `kimi→kimi-coding`, `hf→huggingface`,
   `ollama→ollama-cloud`). Entries mapped to `"custom"` also need `CUSTOM_STYLE_BASE_URLS`
   unless the base URL is user-supplied.
3. `templates/index.html` → `providerMap` in `app()` — the dropdown entry, placeholder,
   per-provider model field (`_MODEL_<KEY>`, client-side only), and `keyPrefix` if Hermes
   declares one (`KNOWN_PROVIDER_KEY_PREFIXES` — a failing prefix makes Hermes skip the
   key with only a warning, so the bot simply never answers).
4. Channels additionally need `CHANNEL_MAP`.
5. `README.md`'s provider/channel lists.

`PROVIDER_KEYS`, `SECRET_KEYS`, `ENV_LABELS` and `HERMES_CUSTOM_STYLE_KEYS` are all derived
from `ENV_VARS` — never hand-maintain them.

### Bumping HERMES_REF

The upgrade is one line in the Dockerfile, but it is never *only* one line. Audit, in
order, against the new release's source:

- `pyproject.toml` `[all]` and the extras string in the Dockerfile's `uv pip install`,
  plus `[tool.uv]` (`exclude-newer` has bricked builds before — if you must override it,
  the date has to be ≥ the newest pinned dep's release date).
- `PROVIDER_REGISTRY` in `hermes_cli/auth.py` vs every id in `HERMES_PROVIDER_IDS`, and
  `KNOWN_PROVIDER_KEY_PREFIXES` vs `providerMap` in the UI.
- New `@app.websocket(...)` routes vs `PROXIED_WS_PATHS` (fail-closed).
- `get_hermes_dir()` directory consolidations vs the `mkdir -p` list in `start.sh` **and**
  `_resolve_pairing_dir()` in `server.py` — those two encode the same rule and must move
  together.
- Dashboard flags (`--skip-build`, and note `--tui` was *removed* — passing a dead flag
  kills the subprocess and 503s the proxy), and the `hermes gateway run` flags.
- The `hermes backup` / `hermes import` CLI contract used by the Backup & Restore code
  (exit codes, `_IMPORT_SKIP_NAMES`).
- `_HERMES_SESSION_TOKEN_RE`'s injected variable name, and the dashboard auth gate.

Then update `ARG HERMES_REF`, `CHANGELOG.md`, and the UI's *What's New* panel.

### Release workflow

Branches are named **`release/<hermes-version>/<n>`**. `/1` is where that Hermes version
first landed; `/2`, `/3`, … are template-only fixes on top of it — **the Hermes version
never changes within a series**. `main` always holds the newest release. Published branches
stay put, because deployed users pin to them to hold or roll back a Hermes version.

`CHANGELOG.md` and the *What's New* panel in `templates/index.html` are two renderings of
the same release and must be updated together. They differ in register on purpose:
the changelog is written for someone reading the code (upstream file paths, repro details,
version numbers); the panel is written for someone running a bot (what changes for them,
what it might cost). Each panel entry carries its git branch and Hermes version.

### Notes

- **`AGENTS.md` and `CLAUDE.md` are the same document, committed twice.** Codex and
  other agents that follow the `AGENTS.md` convention read the first; Claude Code reads
  the second. They differ only in the title and this note — **edit both together**, or
  the two will quietly drift apart and one set of agents will act on stale guidance.
- The UI is deliberately dependency-free apart from Alpine.js and Google Fonts from CDN.
  There is no bundler; edit `templates/index.html` directly.
- `.env.example` documents bare Hermes env vars for reference. It is **not** how this
  template is configured — everything goes through the wizard into `/data/.hermes/.env`.
