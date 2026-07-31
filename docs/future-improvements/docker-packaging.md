# Docker packaging — tmpfs credentials, no keys on disk

Status: designed 2026-07-31, not built. Written to make `docker
compose up` the whole install story before the repo is announced
publicly, and to settle how a container authenticates when both
shipped coach providers ride a local CLI login rather than an API
key. It also picks up scan finding 14
([codebase-scan-2026-07.md](../archive/codebase-scan-2026-07.md),
default paths assume a source checkout), which was parked
explicitly "gated by the GPL distribution decision" — that decision
has since been made, so this doc is that revisit.

## What it is and why

Today the install is `git clone --recurse-submodules`, `make engine`
(a C++ build), `uv sync`, `pnpm install`, and a working `claude`
login. That is five prerequisites before the first game is fetched,
and the engine build is the one most likely to fail on a stranger's
machine. A container collapses it to one command and fixes the
Stockfish toolchain problem outright, because the build happens on a
known Linux image instead of the user's laptop.

The design goal that shapes everything below: **no API key, and no
credential written to a disk we control.** Both shipped providers
authenticate through an interactive CLI login
(`ClaudeAgentSdkProvider`, `CopilotSdkProvider`, `coach/providers.py`)
rather than a token, so there is no key to leak — and the design
should not reintroduce one for the container's convenience.

## What the container has to satisfy

These are properties of the code as it stands, not preferences. Each
one constrains the image.

- **`REPO_ROOT` is derived from the source layout.**
  `config/settings.py` computes `Path(__file__).resolve().parents[4]`
  and anchors `coach.config.yaml`, `data/coach.sqlite3`, `web/dist`,
  `vendor/chess-openings` and `engines/stockfish/src/stockfish` to it.
  Installing the package as an ordinary wheel puts `REPO_ROOT` inside
  `site-packages` and breaks all five. **The image must keep the
  `<root>/backend/src/chess_coach/…` shape and install editable**, as
  `uv sync` already does. This is finding 14, and honouring the layout
  is what lets the container need no code change at all.
- **The ASGI app is a factory.** There is no module-level `app`, so
  uvicorn needs `--factory chess_coach.api:create_app`.
- **`--host 0.0.0.0` is new.** No `--host` is passed anywhere in the
  repo today, so uvicorn binds 127.0.0.1 — unreachable from outside a
  container. This is a change in behaviour, not an existing option,
  and it is the one place where containerising widens the network
  surface the app has always had (the architecture slides list
  "single-user and loopback-only" as an accepted constraint).
- **One uvicorn worker, always.** `storage/db.py` opens a single
  `sqlite3` connection with `check_same_thread=False` shared across
  FastAPI's thread pool. `--workers N` would hand each process its own
  connection and break that assumption.
- **The image's Python needs serialized SQLite.** `open_db` refuses to
  start unless `sqlite3.threadsafety == 3`. `python:3.12-slim`
  satisfies this; it is worth asserting in the build rather than
  discovering at first boot.
- **Migrations are package data.** `_apply_migrations` loads them via
  `importlib.resources.files("chess_coach.storage") / "migrations"`,
  so all eleven `.sql` files must be present in the installed package.
  They apply automatically at startup; there is no migrate command.
- **The openings book is a hard startup dependency.** `load_opening_book`
  raises `FileNotFoundError` if `a.tsv`…`e.tsv` are missing and the
  app will not boot. Only those five files are read, so the image can
  copy them without the rest of the submodule.
- **A missing engine is not fatal.** `app.py` builds the pool only
  `if engine_bin.exists()`; otherwise `app.state.pool = None` and
  everything except analysis still works. Useful for a slim variant.
- **`web/dist` must exist before the factory runs.** The SPA catch-all
  route is registered only `if _WEB_DIST.is_dir()` at app-creation
  time, so the frontend has to be built into the image, not mounted
  later.
- **Configuration is YAML-only.** The sole environment variable the
  app reads is `ANTHROPIC_API_KEY`, and no shipped provider uses it.
  There is no env override for the database path, engine path, book
  directory or port — so the container is configured by mounting a
  `coach.config.yaml`, and `coach.config.yaml` itself is gitignored.

## The credential design — tmpfs, not a volume

Set `CLAUDE_CONFIG_DIR` and `COPILOT_HOME` to paths backed by tmpfs.
Both CLIs are spawned as child processes that inherit the server
process environment verbatim — `subprocess_cli.py` builds the child
env from `os.environ` with no filtering that matters here, and the
Copilot client is constructed with no `base_directory`, so its
runtime honours `COPILOT_HOME`. Setting the two variables as image
`ENV` means the login command and the server agree on where
credentials live without any application change.

```
services:
  api:
    environment:
      CLAUDE_CONFIG_DIR: /run/claude
      COPILOT_HOME: /run/copilot
    tmpfs:
      - /run/claude:uid=10001,mode=0700
      - /run/copilot:uid=10001,mode=0700
    volumes:
      - chess-data:/data
      - ./coach.config.yaml:/app/coach.config.yaml:ro
```

The `uid`/`mode` options are load-bearing: tmpfs mounts are root-owned
by default, the container runs as a non-root user, and without them
the CLIs cannot write their config at all — a failure that reads as
"login is broken" rather than "permissions".

First run is two commands, worth wrapping in `make docker-login`:

```
docker compose up -d
docker compose exec api claude
```

With no browser in the container both CLIs fall back to a paste-a-code
flow (Copilot uses GitHub's device code), so the login completes from
the host browser. Credentials land in tmpfs, the server finds them on
the next coach request, and nothing is written to any disk we control.

### Why not an API key

`create_provider` ignores the `api_key` argument it is passed — no
shipped provider reads it, and `LlmProvider` is a two-value `Literal`
that does not include an API-backed option. Supporting a key is a code
change to `coach/providers.py` and a new provider, not a compose
setting. Putting `anthropic_api_key` in the config file is a hard
`ConfigError` by design.

### Why not a mounted host config directory

Bind-mounting `~/.claude` works on Linux hosts but not on macOS, where
Claude Code keeps its OAuth tokens in the Keychain rather than
`.credentials.json`. On a Mac the mount silently carries settings but
not credentials. It also shares the user's entire Claude state —
history, projects, settings — with the container when one credential
is all that is needed.

### Why not a persistent named volume

A volume buys "log in once, ever", which is the better default for a
stranger cloning the repo, and it is what the shipped compose file
should use. It is the wrong default *for this repo's author*, because
its failure modes are silent:

- `docker commit` and `docker export` capture the writable layer but
  never mounted filesystems. A volume is safe here; the plain writable
  layer is not, and an image accidentally containing a live OAuth
  token is exactly the accident worth engineering away before
  publishing images.
- A volume lives in Docker Desktop's VM disk, which Time Machine and
  cloud backups archive. tmpfs never reaches that disk.
- A stopped container's credentials remain readable via `docker cp`
  indefinitely. tmpfs credentials exist only while the app runs.

tmpfs trades those for a loud, cheap failure: a login prompt after
every stop, including every host reboot. That asymmetry — silent leak
versus visible prompt — is the whole argument.

### What tmpfs costs, precisely

Re-login on every `docker compose stop`, reboot, rebuild and recreate.
Two caveats make it cheaper than it sounds and one makes it dearer:

- Container *recreates* wipe credentials under any scheme short of a
  volume, so during active development tmpfs and the writable layer
  cost the same.
- Nothing else degrades. There is no preflight auth check — `app.py`
  only constructs provider objects at startup — so the container boots
  and serves normally with no credentials. Ingestion, Stockfish
  analysis, openings, the repertoire explorer, highlights, the report
  aggregation, profile facts and every cached LLM artefact all work
  untouched. Only four endpoints need a live login: `POST /coach`,
  `POST /profile`, `GET /explain` and `POST /chat/…/messages`.
- Boot is not free: the opening book is parsed from TSV at every
  startup, so a stop/start cycle pays that cost as well as the login.

When credentials are absent, failure is lazy and already well shaped:
`CoachProviderError` becomes HTTP 502 for JSON endpoints and an SSE
`error` event for streams, carrying the CLI's own "not logged in"
detail. That message is what the user sees after a reboot, so it
should name the fix — see the slices.

## The image — three stages

```
1  engine   debian + build-essential
            git submodule build of engines/stockfish
            explicit ARCH; NNUE net fetched during build
2  web      node 22 + pnpm 11
            pnpm install --frozen-lockfile; pnpm build -> web/dist
3  runtime  python 3.12-slim, non-root user, /app as REPO_ROOT
            uv sync (editable) + copied stockfish, web/dist, TSVs
```

Details that are easy to get wrong:

- **Pass an explicit `ARCH`.** Stockfish defaults to `ARCH=native`,
  which bakes the builder's CPU features into the binary and produces
  an image that crashes on older hardware. The repo only ever builds
  native, so this is new ground: pick `x86-64-bmi2`/`x86-64-avx2` and
  an arm64 equivalent per platform.
- **The engine build needs network.** `make build` runs `net:` first,
  which downloads the NNUE network. Not obvious from `make engine`.
- **Pin pnpm explicitly.** `web/package.json` has no `packageManager`
  field, so Corepack cannot infer it; CI's `pnpm/action-setup@v6` with
  `version: 11` is the only pin in the repo. Node 22 comes from
  `web/.nvmrc` and `engines`.
- **Never regenerate the lockfile in the build.** A pre-commit hook
  fails any `web/pnpm-lock.yaml` containing a tarball or `http(s)://`
  URL, so the build must use `--frozen-lockfile` against the public
  registry and must not bake a mirror into the lockfile.
- **Set `storage.db_path: /data/coach.sqlite3`** in the mounted config
  so the database lands on the one volume. WAL means the volume also
  carries `-wal` and `-shm` files, and rules out a network filesystem.
  `open_db` creates the parent directory, so an empty volume is fine
  provided the non-root user can write it.
- **Keep `server.port` in sync with `--port`.** The app does not bind
  using `server.port`; uvicorn's flag does. The startup banner prints
  `server.port` regardless, so a mismatch produces a banner that lies.

## Licensing — what changes, and what does not

Adding a Dockerfile to the repo creates no new obligation. It is
build instructions, GPL-licensed like the rest of the tree, and a user
running `docker build` is building for themselves, which the GPL does
not restrict. Every ingredient is compatible: python-chess is
GPL-3.0-or-later (the reason the project is), Stockfish is GPLv3 in a
separate process, the openings data is CC0, and the remaining 36
runtime packages are MIT/BSD/Apache with certifi's MPL-2.0 the only
one needing a second look (it is a CA bundle, and MPL 2.0 §3.3 makes
it GPL-compatible regardless).

**Publishing pre-built images is the step that adds duties**, and they
are satisfiable rather than blocking. Such an image conveys compiled
Stockfish and python-chess, so it must carry or point to the
corresponding source — for Stockfish, the source for the *exact*
submodule commit plus its net, which its README states directly.
Tagging images to git tags and setting the OCI labels
(`org.opencontainers.image.source`, `licenses=GPL-3.0-or-later`) meets
this. Note the README's existing framing is unaffected: hosting
remains not-distribution under the GPL, but an image handed to someone
else is.

The real constraint on published images is not copyleft but
redistribution rights: **neither CLI may be baked in.** The Claude Code
CLI is proprietary, and `claude-agent-sdk` now ships a bundled copy
inside its wheel (`claude_agent_sdk/_bundled/claude`), so a plain
`uv sync` pulls it into the image without anyone deciding to. That is
fine for an image a user builds and keeps; it is not fine for one we
publish. The Copilot runtime is downloaded at first use from
github.com and registry.npmjs.org, which is also a reproducibility and
egress issue, not only a licensing one. A published image therefore
wants the coach path disabled or the CLIs provisioned at run time,
and the build needs a documented switch between the two.

## Contract changes (main session, before delegating)

None to the numbered component docs, provided the image keeps the
source layout. That is the point of the `REPO_ROOT` constraint above:
mounting a `coach.config.yaml` with absolute paths covers every
configurable path, so no code changes and
[01-config.md](../01-config.md) stays accurate as written.

Packaging does not become a numbered component. It owns no module in
`chess_coach`, adds no import boundary and needs no sub-agent, so it
stays out of the component numbering and out of the import-linter
contracts — the same call [player-profile.md](player-profile.md)
made for keeping the profile inside component 6.

Two changes are worth making deliberately rather than by accident:

- `--host 0.0.0.0` widens the bind. It belongs to the container only;
  `make dev-api` should keep binding loopback.
- If we later want env overrides for `db_path`, `bin_path` and
  `book_dir` instead of a mounted file, that *is* a config contract
  change and goes to **config-dev** with
  [01-config.md](../01-config.md) updated in the same commit. Not
  needed for this design.

## Slices, in order

1. **main session** — `Dockerfile` (three stages) and
   `.dockerignore`. Acceptance: image builds on both architectures;
   `docker run` boots, applies migrations against an empty volume,
   serves the SPA and `/api`, and reports the engine as available.
2. **main session** — `docker-compose.yml` with the data volume, the
   read-only config mount, and named-volume credentials as the shipped
   default; plus `docker-compose.tmpfs.yml` as the documented override
   for RAM-only credentials. Acceptance: both modes reach a working
   coach call after one login.
3. **main session** — `make docker-build`, `make docker-up`,
   `make docker-login`. Acceptance: a clean clone reaches a coaching
   answer with no host toolchain beyond Docker.
4. **api-dev** — make the unauthenticated failure self-explanatory:
   the 502 and SSE `error` payloads should name the container login
   command, not just the host one. Small, and it is what a user hits
   first after a reboot in tmpfs mode.
5. **main session** — README quickstart gains a Docker path, and
   `coach.config.example.yaml` gains the commented container block
   (`storage.db_path: /data/coach.sqlite3`).

## Risks and trade-offs

- **The bundled Claude CLI is not on `PATH`.** It lives inside
  `site-packages`, so `docker compose exec api claude` needs a symlink
  added in the image. Whether that bundled binary supports the
  interactive login flow at all is the single assumption this design
  rests on and has not been tested — verify before slice 1.
- **Copilot's runtime download at first use** means the coach path
  reaches the network unless pre-seeded with
  `python -m copilot download-runtime` at build time. On a machine
  behind a corporate npm proxy that is also where the build is most
  likely to fail.
- **Chat resume copies credentials.** The SDK may materialise a
  temporary `CLAUDE_CONFIG_DIR`, copying `.credentials.json` and
  `.claude.json`, when resuming a session. The copy should land in
  tmpfs too; confirm where it goes before trusting the RAM-only claim.
- **Image size.** A bundled CLI in the hundreds of megabytes plus a
  Python runtime and a Stockfish binary makes this a large image. Not
  a blocker, worth measuring.
- **Two engine processes by default** (`engine.workers: 2`) means the
  container wants at least two cores to analyse at documented speed.

## Decisions to confirm before starting

1. **Shipped default: volume or tmpfs?** This doc recommends volume as
   the committed default (best first-run experience for the audience
   the announcement targets) with tmpfs as a documented override. The
   opposite default is defensible and is one stanza's difference.
2. **Do we publish images at all, or only ship the Dockerfile?**
   Shipping only the Dockerfile keeps the CLI redistribution question
   closed and costs users one build. Publishing needs the OCI labels,
   tag-to-commit discipline, and a coach-disabled variant.
3. **One image or two?** A no-engine variant boots without Stockfish
   and is much smaller, and the code already tolerates a missing
   binary. Probably not worth the second build until someone asks.
