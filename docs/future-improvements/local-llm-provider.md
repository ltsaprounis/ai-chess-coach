# Local LLM provider — offline coaching, no subscription

Status: researched 2026-07-31, not built; the build plan at the end
is decided but unstarted. This is a design record and a research
report, not a contract. Third-party figures ("Hardware and latency",
the model tables) are labelled as such; the uv, wheel-integrity and
build-time results under "Two shapes" were measured in this repo on
2026-07-31 and say so.

The ask: run the coach against a model on the user's own machine, so
the app works offline and needs no Claude Code or Copilot login.
Model-agnostic where that is free, opinionated where it is not.

Both shipped providers ride a local CLI login
([06-coach.md](../06-coach.md), "Providers"), which is the app's
accessibility floor today: you need a Claude subscription or a
Copilot seat before the coach says a word. Every other component
already runs offline — chess.com sync aside, Stockfish and the
opening book are local. The coach is the last hosted dependency.

## The short version

1. **Build one provider against the OpenAI wire protocol, not
   against a runtime.** Call it `openai-compatible`, not `local`.
   The same class reaches Ollama, llama.cpp, LM Studio and vLLM on
   a config change — and a hosted endpoint later, though **offline
   is a hard constraint for this iteration**, so that stays a
   possibility rather than a plan. `llama-cpp-python` in-process
   emits the same delta shape, so it is a second backend under one
   loop, not a second provider (see "Two shapes").
2. **Reference target `llama-server`; document Ollama as the
   easy path.** Ollama is what club players will install; it also
   silently truncates our report prompt at its 4k default and
   cannot be fixed from inside the provider. That is a
   documentation problem, not a code problem, and it is the single
   most likely cause of "the local coach is useless".
   Deliberately *not* what to develop against: `num_ctx`,
   `keep_alive` and `tool_choice` are all unreachable over `/v1`.
3. **Default model: `Qwen/Qwen3.6-27B` on a 32 GB+ machine,
   `Qwen/Qwen3.5-9B` on 16 GB.** Apache 2.0, and the 27B is the
   only local model with independent evidence for the exact
   failure modes this seam depends on.
4. **No chess-specialised model can serve as the provider.** Not
   one of them does tool calling; the closest purpose-built chess
   coach fabricates *more* than its own un-fine-tuned base model.
   Details in "Chess models" below — the answer is a clear no, and
   the reasons are interesting.
5. **The real work is the agentic loop**, which this provider must
   own outright. Both existing providers borrow one from a vendor
   SDK. This one has to accumulate streamed tool-call deltas,
   dispatch, and re-send, for one tool (`explain`) and four
   (`chat`), against models that are materially worse at it than
   Claude.
6. **Measure before building.** Nobody has run this app's prompts
   through any of these models, and no benchmark tests the style
   contract they have to satisfy. The build plan therefore opens
   with an afternoon-long spike and a stopping rule, not with code.

## The seam is a wire protocol, not a location

`CoachProvider` hides everything LLM-specific, so the question is
what the new class talks to. Every serious 2026 local runtime —
Ollama, llama.cpp's `llama-server`, LM Studio, vLLM, LocalAI, Jan,
KoboldCpp, mlx-lm — exposes `POST {base_url}/v1/chat/completions`
with `tools`, `tool_calls` and SSE streaming. Building against that
shape rather than any product's native API means the runtime becomes
a config value.

Hence the name. `local` describes where the model happens to sit;
`openai-compatible` describes what the provider actually speaks, and
the same class covers a no-subscription *hosted* path (OpenRouter,
Together) for a user who wants neither a local GPU nor a Claude
plan. That is a strictly larger accessibility win than "offline"
alone, for the same code.

The planned `azure-foundry` provider ([06-coach.md](../06-coach.md),
"Providers") does **not** collapse into this: Azure needs a
different client class, an `api-version` query parameter and its own
auth. It could subclass and share the loop; it cannot share the
config surface.

## Runtime: what to target

| Runtime | Tool calls | Constrained decoding | Install friction | License |
|---------|-----------|----------------------|------------------|---------|
| `llama-server` (llama.cpp) | streamed deltas, JSON-healed | lazy GBNF auto-derived from the tool schemas | `brew install llama.cpp` | MIT |
| Ollama | yes, `finish_reason: tool_calls` | `response_format` json_schema | DMG / .exe / one-liner | MIT |
| LM Studio | yes | GBNF (GGUF) + Outlines (MLX) | GUI app | proprietary terms |
| mlx-lm | yes, correct OpenAI shapes | **none at all** | `pip install mlx-lm` | MIT |
| vLLM | strongest on paper | structural tags, strict mode | Linux/NVIDIA-first | Apache 2.0 |

**Reference target: `llama-server`.** Its distinguishing property is
narrow but load-bearing: it derives grammar constraints for tool
calls automatically from arbitrary Jinja chat templates, so
constraint coverage does not depend on someone having hand-written a
parser for your particular model. (It is *not* the only runtime that
constrains tool calls — LocalAI, vLLM and LM Studio all do, in their
own ways. The auto-derivation is what is unique.) It also reads
context size from the model rather than capping it, streams partial
tool-call arguments through a JSON healer, and enables prompt
caching by default (`cache_prompt` true, `--cache-ram` 8192 MiB).
`--jinja` has been on by default since PR #17524 (2025-11-27); the
project's own `docs/function-calling.md` still says the flag is
required and is stale on this point.

**Document Ollama as the recommended install.** It is what people
have. But see "What local mode cannot do" — three of the mitigations
you would naturally reach for are unavailable through its
OpenAI-compatible endpoint, and shipping without documenting the two
environment variables would mean shipping a broken experience.

**Vendor the runtime rather than asking for an install.** llama.cpp
is MIT and a `make local-model` target — git submodule, build, pull
a pinned GGUF — is the exact twin of the `make engine` target that
already builds Stockfish, so it adds no new concept for anyone who
has cloned this repo once. What must *not* happen is redistributing
someone else's binaries or bundling LM Studio, whose terms forbid
shipping with software that requires source disclosure, which
GPL-3.0-or-later does. Pointing at an already-running Ollama stays
supported and is what most users will do.

Worth keeping in proportion: the runtime is 5–50 MB and the model
is 6–17 GB. Removing the runtime install removes well under 1% of
what the user actually downloads, which is why "avoid installing a
runtime" is a smaller win than it sounds — and why the genuinely
zero-install path is not local at all, but the same provider
pointed at a hosted OpenAI-compatible endpoint (see "The seam is a
wire protocol").

**mlx-lm is the fastest Apple Silicon path and the riskiest**: it
has zero grammar, `response_format` or structured-output support, so
tool-call validity is entirely down to the model. Worth listing as
"advanced", never as the default.

## Two shapes: a server, or in-process

There is a prior decision before the client library, and it is the
one that decides how much a user has to install: does inference run
in a **separate process** we talk to over HTTP, or **inside the
FastAPI process** as a Python dependency?

The precedent points both ways and is worth stating honestly.
Stockfish is a separate process — but it is also *vendored*: `make
engine` inits a submodule and compiles it. So "the user installs a
runtime" is not actually this project's pattern; "the repo owns the
runtime and one make target builds it" is. A `make local-model`
twin of `make engine` is the smallest-surprise way to ship the
server option, and the report's earlier flat "do not bundle a
runtime" was wrong about this repo.

**What in-process actually costs**, beyond the dependency:

- **It blocks the event loop.** An HTTP call to a server is
  I/O-bound and `await` is free. In-process generation is
  compute-bound and must go through `asyncio.to_thread`, or it
  stalls every concurrent SSE stream — including the Game page's
  live eval, which shares the process.
- **Model lifetime couples to `lifespan`.** 17 GB stays resident
  unless we build our own unloading. A separate server gets idle
  unloading for free (`--sleep-idle-seconds`, `OLLAMA_KEEP_ALIVE`).

Neither is fatal, and both are the same reason Stockfish is a
subprocess rather than a linked library.

### In-process candidates

| Package | Platforms | Constrained tool calls | Verdict |
|---------|-----------|------------------------|---------|
| `llama-cpp-python` | wheels for macOS arm64, Linux x86_64/aarch64/riscv64, musl, CUDA; sdist builds in ~44 s | yes, but only 3 hand-written handlers — **not** llama.cpp's autoparser | viable via source build; weaker tool calling than `llama-server` |
| `onnxruntime-genai` | wheels for macOS 12+ arm64, Windows x64/arm64, Linux x64/arm64, py3.11–3.14, MIT | yes, native grammar support | narrow ONNX model library; no Metal path, so slow on Apple Silicon |
| `mlx-lm` | Apple Silicon only | **none at all** | fast, but Mac-only and unconstrained — cannot be the accessibility answer |

An earlier draft dismissed `llama-cpp-python` over a supposed
lockfile-URL policy. That objection was simply wrong — `uv.lock`
carries ~491 pythonhosted URLs — and the follow-up worry that a
per-backend wheel index is awkward to lock turned out to be wrong
too. What replaced them is a real limitation, found only by
opening the package.

**It does not inherit llama.cpp's tool-call autoparser.** The
lazy-GBNF, derive-the-grammar-from-any-Jinja-template mechanism
that is the whole argument for `llama-server` lives in
`common/chat.cpp`. `llama-cpp-python` binds only the core
`llama.h` — there is no `common_chat*` symbol anywhere in its
generated bindings. Chat formatting and tool parsing are
reimplemented in Python in `llama_chat_format.py`, and the
tool-capable handlers registered there are exactly three:
`functionary`, `functionary-v1/v2`, and `chatml-function-calling`.
No Qwen, no Hermes, no Llama 3.x native handler. It does use
grammars, but through hand-written per-format code, so the
recommended Qwen3.6-27B would fall back to the generic
`chatml-function-calling` path rather than a model-aware parser.

Since reliable tool calling is the crux of this entire design,
that is a material downgrade — and it is the honest reason to
prefer the server shape, rather than anything about packaging.

**Measured 2026-07-31, uv 0.8.14, M4 Max / 36 GB:**

- Platform-conditional indexes work exactly as documented. Two
  `[[tool.uv.index]]` entries (metal, cpu) plus marker-scoped
  `[tool.uv.sources]` produce **one universal lockfile** carrying
  both wheels under `sys_platform` markers. `uv sync
  --python-platform linux` installs the CPU wheel cleanly.
- Pinning a single index fails loudly on the other platform —
  `only has wheels for the following platform: macosx_11_0_arm64` —
  rather than silently falling back to a source build.
- The lockfile records these wheels as **URL only, with no
  `hash =`**, against 106 hashed `files.pythonhosted.org` entries.
  No integrity pinning, which is not merely theoretical:
- **The Metal wheels are corrupt from 0.3.31 onward.** CRC-tested
  every published version with CPython's own `zipfile.testzip()`,
  so this is not uv being strict: 0.3.30 passes; 0.3.31 and 0.3.32
  fail on `lib/cmake/ggml/ggml-config.cmake`; 0.3.33 has trailing
  content after the end-of-central-directory record; 0.3.34 fails
  CRC on `lib/libggml-base.0.16.0.dylib`. Four consecutive broken
  releases is a pipeline regression, not a one-off, so do not plan
  around a quick upstream fix. The Linux CPU wheels are fine.
- **The source build is cheap.** `uv add llama-cpp-python` with no
  index compiled 0.3.34 from sdist in **44 seconds**, Metal
  enabled automatically (`llama_supports_gpu_offload()` → True,
  `GPU name: MTL0 (Apple M4 Max)`). That is the same order as
  `make engine`, which makes the `make local-model` framing exact.
- `py.typed` ships, so pyright strict is not a concern.
- Incidental correction to "Hardware and latency" below: Metal
  reports `recommendedMaxWorkingSetSize = 28991 MB` on this 36 GB
  machine — ~80%, not the ~2/3 the community figure suggests.

**So the practical route in, if you want in-process:** depend on
the sdist and build from source (44 s, needs Xcode CLT), rather
than pinning the last intact wheel at 0.3.30 — four releases
behind and frozen until upstream's macOS pipeline is repaired.
Accept `chatml-function-calling` as the tool path, and verify
against the chosen model early, because that is where this option
is weakest.

## Client library (server shape)

If inference runs in a separate process, the transport question is
which HTTP client. Recommendation: add **`openai>=2.51`** and
hand-write the loop.

The alternatives were compared by writing the loop against each and
running pyright strict:

| Option | Verdict |
|--------|---------|
| `openai` SDK at a local `base_url` | ~132 lines, pyright strict clean, 0 `Any` |
| raw `httpx` + pydantic wire models | ~129 lines, strict clean, 3 `Any` sites, loses retries/timeouts/error taxonomy |
| `ollama` package | **fails** pyright strict on every `chat()` call — a bare `Callable` in its `tools` annotation poisons the signature |
| LiteLLM | heavier than the thing it wraps; 1.82.7–1.82.9 pulled from PyPI after a March 2026 credential-stealer compromise |
| pydantic-ai | wants to own the agent loop `CoachProvider` already owns |

Cost of the SDK: three genuinely new transitive packages — `distro`
(Apache-2.0), `jiter` (MIT), `tqdm` (MPL-2.0/MIT). All
GPL-3.0-compatible; `anyio`, `httpx`, `pydantic`, `sniffio` and
`typing-extensions` are already locked. What it buys over raw httpx
is retries, a 600 s default read timeout that happens to suit slow
local generation, and a typed error taxonomy under `openai.APIError`.

The honest counter-argument: httpx and pydantic are already
dependencies, the line counts are a wash, and — as below — the SDK's
streaming types do not actually hold, so much of the type safety is
nominal. This is a close call. It went to the SDK on resilience, not
on typing.

Note the shapes are not exclusive. The tool loop, the budget and the
`ExplainEvent`/`ChatEvent` mapping are identical either way; only
"send messages, get deltas" differs. Building the server shape first
and adding an in-process one later is a new transport under the same
loop, not a second provider written twice.

### The streaming types lie, and you must guard

`AsyncStream` parses SSE payloads through `construct_type`, which
does **not** validate, and `field_get_default` maps missing required
fields to `None`. Against local servers this is routine, not exotic:

- `tc.index` is typed `int` and is `None` when a server omits it.
- `finish_reason` is typed as a `Literal` and passes through any
  string — so no `assert_never` on it.
- `chunk.choices` can be `None`, not `[]` — iterating it raises.
- `reasoning_content` **is not on `ChoiceDelta` at all**. It works
  at runtime only because `model_config` sets `extra="allow"`.
  Reading it as an attribute is three pyright strict errors; go
  through `model_extra` with an `isinstance` narrow.

Use `.create(stream=True)`. Do **not** use the `.stream()` helper:
its `accumulate_delta` raises `RuntimeError` on a missing `index`,
and it raises `LengthFinishReasonError` on `finish_reason ==
"length"` — which, with a small local context, is a normal Tuesday,
and losing the partial answer to an exception is the wrong
behaviour for a coaching app.

### Tool-call accumulation

The one algorithm this provider genuinely must get right. `id` and
`function.name` arrive on the first delta of a call; `arguments`
arrives as JSON string fragments across many chunks and is only
parseable once complete:

```
partials: dict[int, Partial] = {}
current = 0
for chunk in stream:
    if not chunk.choices:            # usage-only chunk
        continue
    for tc in chunk.choices[0].delta.tool_calls or []:
        if tc.index is not None:     key = tc.index
        elif tc.id:                  key = len(partials)  # new call
        else:                        key = current        # continuation
        current = key
        slot = partials.setdefault(key, Partial())
        slot.id   = tc.id or slot.id
        slot.name = (tc.function.name if tc.function else None) or slot.name
        slot.args += (tc.function.arguments if tc.function else "") or ""
# json.loads(slot.args) once, at end of stream — never per chunk
```

Runtimes differ in a way this shape absorbs: llama.cpp streams
genuine fragments that must be concatenated, while Ollama and mlx-lm
tend to emit one complete tool call per delta. Accumulate by index,
treat a repeated full-arguments payload idempotently, and never
assume more than one delta per call.

## Models

All recommendations are Apache 2.0 and ungated — the licence matters
for a GPL-3.0-or-later project that wants to call itself accessible.
Llama is out on the Community Licence and on staleness (nothing
since Llama 4, April 2025); Gemma 4 moved to Apache 2.0 but is the
weakest current family on tool-name hallucination; gpt-oss-20b is
unchanged since August 2025 and is interrupted in 97% of LLM Chess
agentic games.

| Tier | Model | Quant | Size | Notes |
|------|-------|-------|------|-------|
| Good machine (32 GB+) | `Qwen/Qwen3.6-27B` | Q4_K_M | ~16.8 GB | best-evidenced local agentic model |
| Club laptop (16 GB) | `Qwen/Qwen3.5-9B` | Q5_K_M | 6.6 GB | IFEval 91.5, BFCL-V4 66.1 |
| CI / smoke | `Qwen/Qwen3.5-4B` | Q4_K_M | 2.7 GB | BFCL-V4 collapses to 50.3 — wiring test only |

Two independent sources back the 27B, and unusually they measure the
exact things this seam depends on rather than general capability:

- **arXiv:2607.27275** (29 Jul 2026) ran τ²-bench across BF16/FP8/
  INT4 and found the damage from 4-bit quantization lands on tool
  calling while *final task scores stay flat* — i.e. the standard
  benchmark hides it. Qwen3.6-27B was the most robust model tested
  (tool-name hallucination 0.38% → 0.47% BF16→INT4); Gemma-4-31B
  the worst (19.5% → 38.3%). This is why Q4 is safe *for this
  specific model* — a measured result, not a generalization.
- **LLM Chess** has `qwen3.6-27b@q4_k_s` completing 27/27 agentic
  games with zero interruptions and 5.5 hallucinated actions per
  1000 moves — frontier-class. Small sample, wide error bars, but
  every other local model in that table is 25–100% interrupted.

Three implementation notes that matter more than the model choice:

1. **Assert the served context.** Ollama's default is 4096 tokens
   below 23 GiB of detected VRAM, 32k below 47 GiB, 256k above
   (`server/routes.go`). A 16 GB *and* a 24 GB Mac both get 4k. Our
   report prompt plus four tool schemas plus 8 turns of tool
   results will be silently truncated **from the front** — which
   eats the system prompt first. Fail loudly below ~32k rather than
   producing bad advice.
2. **Turn thinking off for the coaching paths.** Qwen3.5/3.6 think
   by default; across an 8-turn loop that is a large latency cost
   for prose already grounded in the report. The parameter is
   runtime-specific — `enable_thinking` is the transformers/vLLM
   chat-template kwarg, while **Ollama takes a top-level `think`
   field** and silently ignores `enable_thinking`. Getting this
   wrong looks exactly like "the local model is slow".
3. **Keep Stockfish as the only source of chess truth.** ChessQA
   (arXiv:2510.23948) found only 4 of its runs cleared 50%, with
   Llama 4 Maverick at 17%; ChessArena (arXiv:2509.24239) found
   *no* evaluated LLM beat Maia-1100, an amateur-level engine. The
   current design already treats the model as a writer and the
   engine as the authority. A local provider must not relax that —
   it makes it more important, not less.

## Chess models: the answer is no

The sweep covered Hugging Face's ~1,300 `chess`-tagged models,
arXiv and GitHub. **No chess-specialised model can serve as the
CoachProvider**, for four independent reasons — any one is
disqualifying.

1. **None do tool calling.** Every chess fine-tune found is
   single-turn with a fixed I/O contract: C1-4B terminates on
   `FINAL_ANSWER: <uci>`; `chess-gemma-commentary` emits one canned
   paragraph and is documented as sensitive to exact input
   formatting; ChessGPT-chat predates function calling entirely.
   `explain` and `chat` need 1 and 4 registered tools over ~8 turns.
2. **Wrong input distribution.** Our report prompt is *archive-level
   aggregate statistics* — repertoire tables, monthly trends,
   turning points. These models are trained on single positions.
3. **Redundant.** Stockfish already supplies chess ground truth.
   The job the model has here is to *write*, not to know chess.
4. **Measured worse.** The closest thing to this app that exists —
   `khoilamalphaai/qwen3-1.7b-chess-coach-mlx`, a genuinely
   engine-grounded, rating-calibrated coach — self-reports a **33%
   fabrication rate even when grounded**. Its own un-fine-tuned
   base, Qwen3-1.7B, fabricates at **13%** under identical
   grounding. The chess fine-tune made faithfulness two and a half
   times worse.

Point 4 generalizes into the finding that actually matters: **every
credible chess-text system works by orchestration, not
specialisation.** The same model's fabrication rate goes from 33%
grounded to 99% ungrounded — the grounding does the work, not the
weights. Caïssa AI cross-checks a frontier model against a Prolog
rule engine. A brittleness study (arXiv:2605.17565) lifted
RedPajama-3B from 1.2% to 21.2% position accuracy and 19.3% to 95.3%
move validity purely by wrapping it in an external verifier, with no
retraining. This app's Stockfish-grounded, tool-calling architecture
is already the pattern that works.

What the survey found, for the record:

| Category | Representative | Verdict |
|----------|---------------|---------|
| Move prediction | `google-deepmind/searchless_chess` (270M, 2895 Elo), `adamkarvonen/chess_llms`, `jrahn/RookWorld-LM-124M` | strong players, emit no natural language at all |
| Chess conversation | `Waterhorse/chessgpt-base-v1` / `-chat-v1` (GPT-NeoX 2.8B, 2023) | obsolete; 31 and 16 downloads/month |
| Puzzle reasoning | `UofTCSSLab/C1-4B` (Qwen3-4B base, 48.1% on 900 puzzles, beats Gemini-3-Flash) | genuinely good, and its own paper says puzzles only — not strategy, not commentary |
| Commentary | `NAKSTStudio/chess-gemma-commentary` (270M) | too small, templated output, Gemma licence |
| Coaching | `khoilamalphaai/*` | see point 4; 32B variants are CUDA-oriented QLoRA with CC-BY-NC data |
| Hobbyist LoRAs | dozens, 2025–26 | single-digit downloads, no evals, no documentation |

### Two things worth taking

**Maia-2, behind its own seam — not as a provider.** MIT,
`pip install maia2`, 23M parameters, runs on Apple MPS/CPU with no
lc0 binary. It answers *"what would a 1500 player actually play
here"*, which is orthogonal to Stockfish's "what is objectively
best" and is a real coaching signal: recommending the engine-best
move to a 1200 player is bad pedagogy. Caveats before adopting — it
ships no `py.typed` (a typed wrapper is required, not optional), it
pins `requires_python <3.13`, and it pulls torch, which is a heavy
ask for a club-player install. Prefer it over **Maia-3 despite
Maia-3's better accuracy (57.1% move-matching): Maia-3 is AGPL-3.0**,
and §13's network-use clause is a real escalation over this
project's GPL-3.0-or-later for a hosted app.

**The prompt recipe, which costs nothing.** The chess-coach
project's measured v1→v2 gains came entirely from prompt and data
design: fabrication 46.1% → 31.7%, false facts per answer 0.62 →
0.46. The levers were VERIFIED-FACTS blocks in the prompt,
tier-contrastive framing, teaching *how to find* the move alongside
the move, and — the important one — **a non-LLM verifier that
re-checks every piece and square mentioned in the generated text
against the actual board, rejecting on mismatch.** We have
python-chess already. That verifier is buildable today, independent
of any of this, and it matters *more* with a weak local model than
with Claude.

## What local mode cannot do

The reality check that reshapes the design. Three mitigations you
would reach for are unavailable through the OpenAI-compatible
endpoint on the runtime club players will actually install:

| Mitigation | Status on Ollama `/v1` |
|-----------|------------------------|
| `tool_choice: "required"` to force the first tool call | **unsupported** — explicitly listed as such, alongside `logit_bias`, `n`, `user`, logprobs |
| `num_ctx` to fix the 4k default | **not a field** — only `OLLAMA_CONTEXT_LENGTH` server-side, or a Modelfile |
| `keep_alive: -1` to stop model unloading | **not a field** — only `OLLAMA_KEEP_ALIVE` server-side |

So a meaningful part of "local mode hardening" is *setup
documentation and environment variables*, not provider code. The
provider must feature-detect rather than assume, and must never
depend on `tool_choice`. This is also the strongest argument for
`llama-server` as the reference target — all three levers work
there.

What the provider *can* do, ordered by the evidence behind it:

1. **Never send `response_format` and `tools` in the same
   request.** arXiv:2606.25605 found open-weight models stop
   calling tools entirely under simultaneous JSON-schema
   constraints — the schema compiles to token masks that render
   tool-call tokens unreachable.
2. **Rescue-parse tool calls that arrive as text.** Small models
   routinely emit a fenced JSON object or a `<tool_call>` block
   into `content` instead of populating `tool_calls`. Detect the
   shape, then one *targeted* correction retry — not a blind one.
3. **Dedupe identical `(tool, args)` calls** inside the existing
   turn budget, returning a synthetic "already called, now answer"
   observation. Repeat-call loops are the documented way small
   models burn a budget.
4. **Strip `<think>` blocks defensively** from the visible stream
   in addition to reading `reasoning_content` — reasoning must
   never reach coaching prose.
5. **Validate every SAN/UCI token** in generated text against the
   scope FEN with python-chess. Cheap, already a dependency, and
   the documented weakness of every sub-32B open-weight model.
6. **Hard-fail on context overflow** rather than letting the
   runtime silently eat the system prompt.

Note what is *not* on that list: forcing tool use. arXiv:2605.14038
found small models often know a tool is needed and fail to call it
anyway — Llama-3.2-3B mismatched on 54% of arithmetic tasks — and
that the failure is in the cognition-to-action transition, so better
prompting will not fix it. With `tool_choice` unavailable, the
honest position is that **agentic flows will be less reliable
locally than they are on Claude**, and the app should degrade
rather than pretend.

The graceful-degradation path already exists in the contract:
`complete(prompt, analyst=None)` is a single-turn completion with no
tools, and it is how the profile narrative is generated today
([06-coach.md](../06-coach.md), "Player profile"). That flow will
work on any model that can write. A local provider should be able to
run the report that way too, so a user whose model cannot drive the
tool loop still gets a coach — with the verification rule dropped
from the prompt, since the cache is keyed on `PROMPT_VERSION` and
not on tool availability.

## Contract changes

Small, and the hard rules make them a single commit:

- **`domain.LlmProvider`** gains `"openai-compatible"`. The Literal
  lists implemented providers only, so `assert_never` in
  `create_provider` forces the branch to land with it.
- **`domain.LlmConfig`** gains `base_url: str | None` and needs a
  decision on sampling parameters (temperature at minimum — tool
  calling wants it low) and on how an optional API key arrives.
  Secrets never go in the config file
  ([01-config.md](../01-config.md)), so a hosted endpoint means a
  second env var beside `ANTHROPIC_API_KEY`.
- **[06-coach.md](../06-coach.md)** updates in the same commit —
  domain type change, hard rule.
- **`coach.config.example.yaml`** gains a worked agent entry, and
  the two Ollama environment variables belong in the README next to
  it, not buried in a doc.
- **Frontend: nothing.** `CoachAgentInfo` carries `provider` as the
  generated union, so it is `pnpm gen:api` and no component change.
  The roster is config-driven end to end.

Everything else is additive. The provider is one class next to
`ClaudeAgentSdkProvider` and `CopilotSdkProvider`, `provider_state`
stays `None`, and `render_chat_prompt` replay — already built and
tested for exactly this case — is the resume story.

## Testing

The suite is hermetic by policy: no network, no real LLM, no
Stockfish ([GUIDELINES.md](../GUIDELINES.md)). That does not change.
Stubbed-SSE unit tests cover the loop the way the Copilot provider's
`_FakeCopilotSession` covers its own, and they are where the
accumulator's edge cases belong: missing `index`, fragmented
arguments, a usage-only final chunk, `finish_reason` outside the
Literal, a tool call arriving as fenced text.

An **opt-in local integration test** is the right second layer, with
precedent: one already exists for the real engine binary. Point it
at a running `llama-server`, skip when absent.

A CI job running a tiny model is tempting and mostly does not pay.
The free 4 vCPU / 16 GB public-repo runner can host a sub-1B GGUF,
but a sub-1B model will very likely never emit a valid tool call —
so the job would exercise SSE framing and loop termination while
proving nothing about the tool path, which is the part that
actually needs proving. Stubs cover framing more cheaply and more
deterministically.

One cheap test worth adding regardless of any of this: **pin the
append-only, timestamp-free property of `render_chat_prompt`.** It
is what makes the chat seed prefix-cacheable, worth 13–31% TTFT
across agentic workloads (arXiv:2601.06007), and it is currently
true by accident rather than by contract.

## Hardware and latency

Other people's `llama-bench` numbers, 7B Q4_0, `-p 512 -n 128`:

| Machine | prefill (t/s) | decode (t/s) |
|---------|--------------|--------------|
| base M1 | 108–118 | 14 |
| base M4 | 221–224 | 14–24 |
| M4 Pro | 364–449 | 49–50 |
| M4 Max | 713–885 | 69–83 |
| RTX 4060 Ti | 3,395 | 64 |
| RTX 4090 | 11,993 | 186 |

Consumer NVIDIA prefills an order of magnitude faster than base
Apple Silicon, which is exactly the axis our long report prompt
loads. Applying these to a ~3k-token report prompt and ~700 tokens
out: roughly 45–90 s on a base-M 16 GB laptop with an 8–9B, ~20 s on
an M4 Max, 12–18 s on a desktop NVIDIA card. CPU-only is 2+ minutes
and should be declared unsupported.

The honest UX read: a cached, explicitly user-triggered report
behind a progress bar at 45 s is defensible. An `explain` call at
19 s is not — that flow feels broken above a few seconds. Expect
`explain` and `chat` to be the tiers' real dividing line, not
report generation.

Two levers change these numbers materially. **Prompt caching** turns
the chat flow's re-sent ~1,800-token seed from ~11 s of
time-to-first-token into ~1 s; `llama-server` has it on by default,
Ollama reuses exact-match prefixes. And on macOS the GPU-wired
memory ceiling is roughly 2/3 of RAM below 64 GB (community figure,
not Apple documentation) — an 8 GB Mac exposes ~5.3 GB, which is a
4B Q4 and nothing else.

## What to measure

Four questions this research could not answer from sources, each
cheap to settle in-repo. Questions 1 and 3 are the substance of the
build plan's Step 0 spike and gate everything after it; 2 and 4 can
wait for the steps that depend on them (Ollama documentation, and
the tool loop respectively).

1. **What does a real report prompt actually tokenize to?** The 3k
   estimate comes from fixtures; a 1,200-game archive will be
   larger, and this number decides whether base-M machines need the
   prompt staged into passes. Render for a heavy user, tokenize via
   `llama-server`'s `/tokenize`. Twenty minutes, and it dominates
   the latency picture.
2. **Does Ollama's 4k default truncate silently in practice, or
   error?** Send the real prompt to a default `ollama serve` and
   compare `prompt_eval_count` against the true token count.
3. **Which model holds up on *our* prompts?** No leaderboard
   measures the style contract this app enforces — pawns not
   centipawns, reference-link handles like `[g3]`, no invented
   links. A small in-repo bake-off over fixed report/explain/chat
   prompts, scored on "did it call `analyze_position`, did it cite
   only legal moves, did it stay inside the supplied stats", is the
   only thing that will answer it.
4. **Does the accumulator survive real traffic?** Capture SSE
   traces from both `llama-server` and Ollama with the chosen model
   and replay them as fixtures. In particular llama.cpp has a known
   small-MoE quirk where the tool-call count momentarily *decreases*
   between chunks, which naive diff-consumers reject.

## Build plan

Hosted endpoints are out of scope for this iteration: **offline is a
hard constraint**, so the OpenRouter path stays a documented
possibility rather than a step below.

### Step 0 — the spike: pure llama-cpp-python, tools included

Nobody has run *this app's* prompts through *these* models, so the
first move is an experiment, not code. The unknowns, ranked:

1. Can a local model drive an agentic loop with our tools on our
   prompts? **Unmeasured.**
2. Does the real report prompt fit a sane context? **Unmeasured** —
   the ~3k figure is from fixtures, not from a 1,200-game archive.
3. Is local prose good enough to call itself a coach?
   **Unmeasured.**
4. Does the wiring work? **Low risk** — one class behind a seam
   that already has two implementations.

The spike runs entirely on **llama-cpp-python + Qwen3.6-27B
Q4_K_M**, deliberately: it tests the stack Step 1 actually proposes,
end to end, with nothing else installed and no server to orchestrate.
`uv add llama-cpp-python` (~44 s source build), pull the GGUF, write
a throwaway script. No provider code, no contract changes.

**Three constructor defaults will produce a false negative if
missed** — all verified in `llama_cpp/llama.py`:

| Parameter | Default | Set it to |
|-----------|---------|-----------|
| `n_ctx` | **512** | `0` (read from model) or an explicit 32768 |
| `n_gpu_layers` | **0 — CPU only** | `-1`, to offload every layer to Metal |
| `chat_format` | `None` (auto-detect) | `"chatml-function-calling"` for tools |

A 512-token context and CPU-only inference would make a healthy
setup look unusably slow and truncated. Note the third one is a real
trade, not just config: opting into tool calling **replaces** the
model's own chat template with the handler's convention, and
Qwen3.6 was trained to emit `<tool_call>` tags. That mismatch is the
most plausible way this spike fails, and it is exactly what it
exists to find out.

**Dump the inputs first; the spike needs two strings, not a
database.** Render the report prompt and an explain prompt once, to
files, and have every run after that read the files.

> **Hazard: `open_db` writes.** It calls `_apply_migrations`
> ([storage/db.py](../../backend/src/chess_coach/storage/db.py)), so
> opening the live database through the app's own API migrates it —
> not what a read-only experiment should do. Bypass it:
> `sqlite3.connect("file:data/coach.sqlite3?mode=ro", uri=True)`.
> `backend/scripts/backfill.py` is the precedent for a one-off
> script against this database.

Pinning the inputs to files is what makes the model comparison
valid: Phase B's 15–20 runs and the walk-down below must see a
byte-identical prompt, or a re-sync or re-analysis shifts it
underneath the experiment and the difference gets attributed to the
model. Render the **widest** realistic scope — all time controls,
full history, no window — since the fixture-derived ~3k estimate is
exactly what is in question, and the typical case would measure the
wrong number.

The dumped prompts carry real usernames, opponents and dates, and
`data/` is gitignored for that reason: keep them out of the repo,
and quote token counts and structure in the report, never the prompt
body.

**Phase A — no tools.** Tokenize the dumped report prompt with
`llm.tokenize()` for a true count, then generate and read the
output. Record prompt tokens, wall clock split prefill/decode, and a
human judgment on the prose.

**Phase B — tools on.** Register the real `analyze_position` schema,
feed the dumped explain prompt, and run it 15–20 times. This is a
hit rate, not an impression:

- how often it calls the tool at all;
- how often the FEN argument is valid and parses;
- how often it then *uses* the returned lines rather than asserting
  its own variation;
- failure taxonomy: no call, malformed JSON, tool emitted as plain
  text, repeat-call loop, `<think>` leaking into prose.

**Kill criteria, agreed before starting:**

- Real report prompt exceeds **~16k tokens**, or
- Phase A prose is visibly worse than a mediocre human coach.

Either means stop and shrink the ambition — most likely to "local
generates profile narratives only" — rather than reaching for a
bigger model or a longer context.

**Write the results up as
`docs/spike-reports/local-llm-provider.md`** — prompt tokens,
timings, and the Phase B hit rate, against the conventions in
[spike-reports/README.md](../spike-reports/README.md). They are the
numbers the rest of this plan is missing: every latency figure under
"Hardware and latency" is someone else's hardware until they exist.
This doc then cites that report rather than restating it, so there
is one place the measurements live.

### After the spike — the branch

Phase B decides what Step 2 looks like, which is why the plan
genuinely stops here:

- **Tool calling reliable on P4** → build the loop directly on P4.
  `llama-server` demotes to a later alternative backend, and the
  in-process shape carries the whole feature.
- **Tool calling unreliable on P4** → re-run *Phase B only* against
  a throwaway `llama-server` with the same model and prompts. That
  disambiguates the one thing a single-stack spike cannot: whether
  the fault is the generic `chatml-function-calling` handler or the
  model and our prompts. If llama-server succeeds where P4 failed,
  it is the handler — build the loop on P3 (Step 2 below) and keep
  P4 for the non-agentic path.
- **Neither works** → the agentic flows are not viable locally.
  Ship `complete(analyst=None)` only, and say so plainly in the UI
  rather than shipping a coach that invents variations.

**Start at the ceiling, not at the target.** Qwen3.6-27B on 36 GB is
the best case; if it cannot do this, a 9B on a base M4 certainly
cannot. If it can, walk *down* — 9B, then 4B — until it breaks.
Where it breaks is the documented minimum spec, discovered rather
than guessed.

### Step 1 — P4 in-process, non-agentic first

`llama-cpp-python` with `complete(analyst=None)`, carrying over the
model and settings the spike validated. Fewest moving parts: one
dependency, no second process. Zero tool-calling risk in scope, so
it lands regardless of how Phase B went — and if Phase B failed
outright, this *is* the shippable product rather than a stepping
stone.

It also ships something real: the profile narrative already runs
through this flow, and a report without engine verification is a
usable offline coach for anyone with no subscription.

1. Contract commit — `LlmProvider` value, `LlmConfig` fields,
   [06-coach.md](../06-coach.md), `coach.config.example.yaml`,
   `pnpm gen:api`. Reusable whichever backend eventually wins.
2. `_ChatBackend` protocol and the normalized `_Delta` type —
   defined now, with one implementation, so step 2 is an adapter
   rather than a refactor.
3. The in-process backend and `complete`, bridging the sync
   generator with `asyncio.to_thread` plus a queue — the same
   callback-to-generator shape `CopilotSdkProvider.explain`
   already uses.

### Step 2 — the tool loop, on whichever backend the spike chose

`explain` first (one tool), then `chat` (four, plus replay). The
loop itself is backend-agnostic by construction — it consumes
`_Delta`, so the accumulator, budget, dispatch and event mapping are
written once regardless of which branch Phase B put us on.

If the spike sent us to **P3**: `llama-server` is where tool calling
is strongest — the autoparser derives grammars from the model's own
template, and `tool_choice`, `num_ctx` and prompt caching are all
reachable.

Not Ollama either way, despite it being what users will install. It
is the right thing to *document* and the wrong thing to *build
against* — `num_ctx`, `keep_alive` and `tool_choice` are all
unreachable over `/v1`, so it means debugging with the controls
removed and a silent 4k truncation waiting. Document it once the
prompts' real context needs are known.

One asymmetry to plan around whichever way this goes: P4 generates
tool calls with `stream=False`, so it never emits fragmented
arguments and cannot exercise the accumulator's hardest path. Those
stubbed fragment tests are written blind and are only validated for
real against an HTTP backend — so keep them even on a P4-only
build, and treat the first P3 run as their real test.

### Step 3 — the second backend

Add whichever of P3/P4 the spike did not select, as a `_ChatBackend`
implementation under the existing loop. This is the step that makes
the seam pay for itself, and it is also the honest cross-check: a
failure that appears on one backend and not the other is a transport
or handler fault, not a loop fault.

### Then

4. The hardening layer, in the order given under "What local mode
   cannot do".
5. The python-chess faithfulness verifier — worth building whether
   or not any of the rest of this ships, and worth more with a weak
   local model than with Claude.

## Sources

Primary claims here were fact-checked against primary sources; the
checks corrected several details, notably that `tool_choice`,
`num_ctx` and `keep_alive` are all unreachable through Ollama's
OpenAI-compatible endpoint, and that `reasoning_content` is absent
from the `openai` SDK's `ChoiceDelta` type.

- arXiv:2607.27275 — quantization damage lands on tool calling while
  task scores stay flat
- arXiv:2606.25605 — JSON-schema constraints suppress tool calling
- arXiv:2605.14038 — the knowing–doing gap in small-model tool use
- arXiv:2601.06007 — prompt caching for long-horizon agentic tasks
- arXiv:2510.23948 (ChessQA), arXiv:2509.24239 (ChessArena),
  arXiv:2605.17565 (brittleness testing of chess-trained LMs)
- Ollama `docs/context-length.mdx`, `docs/api/openai-compatibility.mdx`,
  `server/routes.go`
- llama.cpp `tools/server/README.md`, `common/chat.h`, PR #12379
  (streamed tool-call deltas), PR #17524 (`--jinja` by default)
- `Lichess/chess-position-evaluations` (CC0) — the clean dataset
  base, if fine-tuning is ever revisited
- `onnxruntime-genai` 0.15.0 (PyPI, 2026-07-29, MIT) — wheel
  platform list and native grammar/tool-calling support

Measured in-repo on 2026-07-31 with uv 0.8.14, rather than taken
from a source: the platform-conditional index resolution, the
single-index cross-platform failure message, the absent wheel
hashes, and the corrupt 0.3.33/0.3.34 Metal wheels (cross-checked
with CPython `zipfile.testzip()`).
