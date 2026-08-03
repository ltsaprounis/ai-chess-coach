# Coach chat — follow-up conversations grounded in tools

Status: built 2026-07-30, archived 2026-08-03. The contracts migrated
into the component docs (03, 06, 07, 08 + domain), which are
authoritative; this doc remains the design record. Its "Open
questions and risks" section carries the follow-ups accepted at
review time. None of them are load-bearing, and all of them are now
tracked as one entry under "Housekeeping worth scheduling" in
[NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md). Expands
backlog item 8
("Ask a follow-up question",
[NEW-FEATURE-PROPOSAL.md](../NEW-FEATURE-PROPOSAL.md)), which flagged
it as "bigger than it looks" and deferred it until demand showed.
This doc is the design that makes it buildable: the provider seam,
the tool seam, and the persistence model are decided here; the
component slices at the end are ready to delegate.

## What it is and why

Explain-move and the coach report are one-shot: the student reads,
has the obvious next question — "but what if I take the knight?",
"show me a game where this actually happened" — and has nowhere to
put it. Chat mode adds a conversation panel in two places, sharing
one backbone:

- **Game chat** (Game page): anchored to a game, optionally to a
  ply — the natural continuation of an explanation.
- **Report chat** (Coach page): anchored to the report window the
  advice was generated over — the natural continuation of the brief.

What makes it a *coach* and not a chatbot is grounding: the agent
can query the student's stored games and run Stockfish mid-answer,
so "you blundered this same structure against marko77 in June" is a
verified claim with a link, not a hallucination.

## The three premises, validated

The feature request came with three architectural premises. All
three hold; each gets one refinement.

**1. "Chat mode for move analysis and the coach."** Valid, and
already on the backlog as item 8. The refinement: build one chat
backbone with two *scopes* (game, report), not two features. The
scope decides the seed context; everything downstream — provider
seam, tools, storage, SSE shape, frontend panel — is shared.

**2. "Agents need tools to query the DB and use Stockfish."**
Valid — a chat that cannot look anything up can only rephrase its
seed. The refinement: the coach component must not gain DB or
engine access. The hard rule stands — components never import each
other — and the existing engine seam already shows the pattern:
`PositionAnalystFn` is a callable the API layer injects, wrapping
the pool. Chat generalizes that one callable into a small injected
*toolkit* (below). Coach owns the tool contracts — names, schemas,
descriptions, and how results render into LLM-readable text (the
style contract: pawns, never centipawns) — while the API layer owns
the implementations over storage and the engine pool. Tools are
read-only and pre-scoped to the thread's player; there is no raw
SQL tool and never will be.

**3. "An abstract class defining one API over claude, copilot, and
future providers."** Valid in intent — one interface, N providers —
with the refinement that this interface already exists:
`CoachProvider` in [06-coach.md](../06-coach.md) is exactly that
seam, a `Protocol` with `complete` and `explain`, one factory, two
implementations. Chat *widens the existing seam* with a `chat`
method rather than introducing a parallel abstract class: one seam,
one factory, and every provider (including the planned `anthropic`
and `azure-foundry`) implements all three methods or none. Keeping
it a `Protocol` rather than an ABC follows the house style
(structural typing, no inheritance requirement, pyright-checked);
if genuinely shared machinery emerges across providers — budget
enforcement, transcript rendering — it lands as module-level
helpers, as the budget constants already do.

## Design

### The provider seam

```python
class ChatEvent(BaseModel):        # one streamed chat increment
    type: Literal["text", "tool", "done"]
    text: str = ""                 # chunk | tool summary | full reply
    provider_state: str | None = None   # done events only; see below

class CoachProvider(Protocol):
    async def complete(...) -> str            # unchanged
    def explain(...) -> AsyncGenerator[ExplainEvent]   # unchanged
    def chat(self, *,
             system_context: str,             # scope seed, rendered
             history: list[ChatMessage],      # prior turns, oldest first
             message: str,                    # the new user message
             toolkit: ChatToolkit,
             provider_state: str | None,      # from the thread row
             ) -> AsyncGenerator[ChatEvent]
```

The contract is **stateless with an opaque resume token**. Each call
carries everything needed to answer from scratch: the rendered seed,
the stored transcript, the new message. A provider *may* shortcut
the replay — claude-agent-sdk can resume a session by id, the
Copilot SDK keeps sessions in its runtime — by returning an opaque
`provider_state` string on its final `done` event, which the API
layer persists on the thread and hands back next message. A
provider that cannot resume (the planned API-backed providers, or a
resume that expired) ignores the token and replays the transcript.
Correctness never depends on warm state: the server can restart
between messages and the thread still works.

This shape is what makes premise 3 pay off: a session-object
interface would fit the two CLI-backed SDKs and fight every
HTTP-backed one; a stateless interface fits both, and resumption
stays a per-provider optimization hidden behind the seam.

Per-message budgets mirror explain: a `_CHAT_MAX_TURNS` tool budget
(start at 8), SDK-enforced `max_turns` on the Claude provider, the
counted grace-round pattern on Copilot, and the same stall timeout.

### The toolkit seam

```python
class ChatToolkit(Protocol):       # implemented by the API layer;
    # the engine seam, as today     # pre-scoped to the thread's
    async def analyze_position(self, fen: str) -> list[EvalLine]
    # storage lookups (read-only; sync repo calls wrapped in
    # asyncio.to_thread by the implementation)
    async def find_games(self, *, opponent: str | None = None,
                         opening: str | None = None,
                         result: Result | None = None,
                         time_class: TimeClass | None = None,
                         since: int | None = None,
                         until: int | None = None,
                         limit: int = 10) -> list[GameSummary]
    async def get_game(self, game_id: str) -> GameDetail | None
    async def opening_stats(self) -> list[OpeningStats]
```

Coach exposes each method to the model as one tool
(`analyze_position`, `find_games`, `get_game`, `get_opening_stats`)
using each SDK's mechanism — the in-process MCP server on Claude,
custom `Tool`s on Copilot — exactly as `analyze_position` is wired
today, and renders every result to text itself, so the prompt style
stays owned in one place. Parameters and results are existing
domain types; the filter arguments are plain kwargs, so no type
needs promoting out of storage.

The roster is deliberately small — every tool costs schema tokens
on every message. Not in v1: a report-rebuild tool (report chat is
seeded with the rendered report), highlights, and anything that
writes. `find_games` returns slim `GameSummary` rows;
`get_game` renders moves, judgments and evals compactly (the model
asks for a game only after finding it, so the expensive rendering
is paid per request, not per search hit).

Username and window scoping are baked into the implementation the
API layer builds per thread — the model passes filters, never a
username — so a tool call can only ever read the thread's player.

### Scopes and seeds

`system_context` is rendered by coach per scope:

- **game**: the game's identity (opponent, date, result, opening)
  and, when a ply anchor is set, the same `MoveContext` + seeded
  eval lines the explain prompt uses. When a cached explanation
  exists for that (game, ply, agent), it is prepended to `history`
  as the first assistant turn, so the chat genuinely continues from
  what the student just read.
- **report**: the report's data sections plus the cached advice as
  the first assistant turn, same reasoning. The window and
  time-class scope of the thread are the report's own.

The register rules from the explain style contract apply verbatim
(club player, pawns, idea before number), plus one chat-specific
instruction: claims about the student's games must come from tool
results, not memory of the seed.

### Persistence and cost

Two tables, migration 009 (mirroring the reports cache's sentinel
conventions):

```sql
chat_threads (
  id TEXT PRIMARY KEY,             -- uuid, minted by the API layer
  username TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  scope TEXT NOT NULL,             -- 'report' | 'game'
  game_id TEXT,                    -- scope='game' only
  ply INTEGER,                     -- optional move anchor
  since INTEGER NOT NULL,          -- 0 = open, as in reports
  until INTEGER NOT NULL,
  time_class TEXT NOT NULL,        -- '' = all controls
  provider_state TEXT,             -- opaque resume token, nullable
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
chat_messages (
  thread_id TEXT NOT NULL REFERENCES chat_threads(id),
  seq INTEGER NOT NULL,            -- per-thread, 1-based
  role TEXT NOT NULL,              -- 'user' | 'assistant'
  content TEXT NOT NULL,           -- markdown
  created_at INTEGER NOT NULL,
  PRIMARY KEY (thread_id, seq)
);
```

House cost policy holds without exception: every LLM call is a user
pressing send. Reopening a thread renders the stored transcript and
bills nothing. The chat-specific cost is transcript growth — a
replay provider re-bills the whole history each message — so v1
caps a thread (~40 messages), the UI nudges "start a new chat" at
the cap, and resumption keeps the common case (same provider, warm
runtime) cheap. Tool events are not persisted; they are progress
UI, same as explain.

`ChatMessage` (role, content, created_at) is used by storage, coach
and the API layer, so it lands in `domain`. `ChatEvent` stays on
the coach surface beside `ExplainEvent`; storage's thread/message
row types stay on its own surface like `CachedReport`.

### HTTP API

| Method | Path                                 | Behavior              |
|--------|--------------------------------------|-----------------------|
| POST   | `/api/players/{u}/chat/threads`      | Create thread: body `{scope, agent_id?, game_id?, ply?, since?, until?, time_class?}`; validates scope fields (game must exist and be analyzed for a ply anchor); returns the thread |
| GET    | `/api/players/{u}/chat/threads`      | List threads, newest first (id, scope, anchor identity, updated_at, first user line as title) |
| GET    | `/api/chat/threads/{id}`             | Thread + full transcript |
| POST   | `/api/chat/threads/{id}/messages`    | Body `{text}`; SSE response streaming `text`/`tool` events then `done` with the full reply (persisted before `done` is sent); `error` event mid-stream on `CoachProviderError`, nothing persisted; 409 while a reply is already streaming for this thread |
| DELETE | `/api/chat/threads/{id}`             | Delete thread + messages |

POST-with-SSE-response is compatible with the frontend as built:
the SSE hooks already use `fetch` streaming rather than
`EventSource` (which is GET-only) precisely so pre-stream JSON
errors surface — see `useExplain.ts`. Client disconnect aborts
generation; the user message and any partial reply are not
persisted, mirroring explain's cache-nothing rule. An aborted or
errored turn also **clears `provider_state`**: the discarded
message may already have reached the provider's warm session, so
the stored transcript and that session have diverged — resuming it
next turn would replay the student's message into a conversation
that half-remembers it. Nulling the token forces the next message
down the replay path, which is built from the database alone;
the database stays the single source of truth.

The engine pool being down degrades, not fails: the toolkit's
`analyze_position` is built only when the pool is up (the model is
told when it is not), same as `/coach`'s analyst wiring.

### Frontend

One `ChatPanel` component, two mounts: under the explanation panel
on the Game page ("Ask a follow-up") and under the advice on the
Coach page. Reuses the SSE parsing the explain hook owns (extract
the block parser rather than copying it), the `AgentSelect` roster
from Settings, and the markdown renderer — replies carry the same
app-relative game links, produced by the model from tool results
this time, so `append_game_links`-style post-processing is not
required in v1 (an open question below). Tool events render as the
same transient progress lines explain shows. SSE payload types are
hand-declared with backend-model comments, per GUIDELINES.

## Contract changes (main session, before delegating)

- `domain.py`: `ChatMessage`. README's domain-type listing updated.
- [06-coach.md](../06-coach.md): `ChatToolkit`, `ChatEvent`,
  `CoachProvider.chat`, seed renderers, budgets, the tool roster
  and its rendering rules.
- [03-storage.md](../03-storage.md): the two tables + repo
  functions (create/get/list/delete thread, append message, update
  `provider_state`/`updated_at`).
- [07-api.md](../07-api.md): the five routes and the toolkit
  wiring (this is where coach meets storage *and* engine; they
  still never import each other).
- [08-frontend.md](../08-frontend.md): the panel and its mounts.

## Slices, in order

1. **coach-dev** — `ChatToolkit` protocol, tool schemas + result
   rendering, seed renderers for both scopes, `chat` on both
   providers (budget + stall + grace-round patterns as in explain);
   tests with a stub toolkit and stubbed SDKs, snapshot tests on
   the seed renderers.
2. **storage-dev** — migration 009 + thread/message repo functions;
   temp-DB tests including re-open and cascade delete.
3. **api-dev** — routes, per-thread toolkit construction
   (`asyncio.to_thread` over the sync repos, pool wrapper when up),
   one-stream-per-thread 409, mid-stream error mapping; integration
   tests with a stub provider and stub toolkit.
4. **frontend-dev** — after `pnpm gen:api`: extract the SSE block
   parser from `useExplain.ts`, `useChat` hook, `ChatPanel`, two
   mounts, thread reopen + new-thread affordance.

Order matters only at the seam: 1 defines what 3 injects; 2 and 1
can run in parallel.

## Open questions and risks

- **Resume fidelity.** Whether claude-agent-sdk `resume` preserves
  the in-process MCP tools and replaced system prompt, and whether
  Copilot sessions survive runtime restarts, decides how often the
  replay fallback actually runs. The design works either way; the
  cost profile differs. Measure during slice 1.
- **Link discipline.** The report flow guarantees link integrity by
  minting URLs server-side (`append_game_links`). Chat replies cite
  games the model found via tools, so handles cannot be
  pre-assigned. v1 has the model write app-relative links from tool
  results (which include ids) and accepts the risk of a mistyped
  path degrading to a dead link; if that bites, a post-processing
  pass validating `/games/{id}` links against tool-returned ids is
  the fix, one place, in coach.
- **Explain convergence.** A game-scoped thread seeded at a ply is
  a superset of explain. Once chat proves out, explain's UI could
  become "first message of a thread" and its endpoint retire —
  deliberately out of scope here; the explanation cache and its
  regenerate flow stay untouched.
- **Transcript growth vs. context.** The ~40-message cap is a
  guess. If long threads matter, summarize-and-truncate in the
  replay path is the follow-up, owned by coach.

Accepted at the 2026-07-30 boundary review as known small gaps,
none load-bearing:

- **Report-seed latency.** Report-scope chat rebuilds the full
  `PlayerReport` (a python-chess replay of every analyzed game in
  the window) on every message. CPU-only and correct, but at
  1,200-game archives it adds real per-message latency; a
  per-thread memo in the API layer is the cheap fix.
- **Mid-stream scope switch (UI only).** Changing the mount's scope
  (a ply click) while a reply streams re-resolves the thread
  without aborting the old stream; its `done` can land in the new
  scope's client-side transcript. Server-side persistence stays
  correct (the old thread id) — a refresh shows truth.
- **Never-started stream leak.** If a client vanishes between the
  in-flight mark and the SSE generator's first iteration, the
  in-flight slot leaks until restart. The window is one ASGI
  dispatch; the cleanup-ordering fix covers every started stream.
