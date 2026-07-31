# Local LLM provider — Step 0 spike

Run 2026-07-31. Answers Step 0 of
[future-improvements/local-llm-provider.md](../future-improvements/local-llm-provider.md).

**Verdict in one line:** the prompt fits, the model's tool calling is
flawless, and `llama-server` runs report, explain and chat on one
process — while `llama-cpp-python`'s only applicable tool handler
silently discards every tool result. **Build on `llama-server` alone.**

The chess content is unreliable on both backends, in the same way, and
that is a separate problem the provider choice does not touch.

## Hardware and environment

| | |
|---|---|
| Machine | Apple M4 Max, 36 GB unified memory |
| OS | macOS 15.7.7 (24G720) |
| Xcode CLT | 26.1.0.0.1.1761104275 |
| GPU as seen by ggml | `MTL0 (Apple M4 Max)`, `MTLGPUFamilyApple9` |
| Metal working set | `recommendedMaxWorkingSetSize = 28991.03 MB` |

That last figure independently reproduces the design doc's own
in-repo measurement (~80% of RAM, not the ~2/3 community figure).

## Versions

| Component | Version |
|---|---|
| `llama-cpp-python` | 0.3.34, **built from sdist**, Metal auto-enabled |
| uv | 0.8.14 (af856fb88 2025-08-28) |
| python-chess | 1.11.2 |
| Stockfish | dev-20260720-f4bcd404 (repo submodule build) |
| Python (spike venv) | CPython 3.13.7 |
| Python (backend, dump only) | CPython 3.12.11 |
| `PROMPT_VERSION` | `2026-07-one-register-one-unit` |

`uv add llama-cpp-python chess` resolved in 42 s and **built the
package in 25 s** (67 s wall, end to end), confirming the design's
"~44 s source build" order of magnitude. `llama_supports_gpu_offload()`
→ True. The corrupt-Metal-wheel problem the design records was avoided
entirely by building from source, as it recommends.

## Model

`unsloth/Qwen3.6-27B-GGUF` → `Qwen3.6-27B-Q4_K_M.gguf`, Apache 2.0,
16,817,244,384 bytes (16.82 GB — exactly the design's figure).

sha256 `5ed60d0af4650a854b1755bd392f9aef4872643dc25a254bc68043fa638392a0`,
**verified equal to the repo's published LFS oid.** GGUF arch string is
`qwen35`. 65/65 layers offloaded to Metal; 16,027.70 MiB allocated of
27,648 MiB available.

Incidental: Hugging Face throttles hard per connection here — a single
`curl` sustained ~1 MiB/s (3 h ETA), while 12 ranged connections
sustained ~17.3 MiB/s (16 min). Worth knowing before `make local-model`
is written.

## Every non-default setting

The design names three constructor footguns. All three were set, and a
fourth setting turned out to matter more than any of them.

| Parameter | Library default | Used here |
|---|---|---|
| `n_ctx` | 512 | **32768** |
| `n_gpu_layers` | 0 (CPU-only) | **-1** (all 65 layers to Metal) |
| `chat_format` | `None` (auto) | Phase A: **`None`, deliberately** — the model's own template. Phase B: **`"chatml-function-calling"`** |
| `max_tokens` | 16 | Phase A 2048 then **8192**; Phase B 1536 |
| `temperature` | 0.8 | **0.7** |
| `tool_choice` | — | `"auto"` (arms 1–2), `"none"` on the follow-up turn (arm 3) |

Phase A keeps the model's own template on purpose: replacing it is
Phase B's variable, not a constant.

## The question and the stopping rule, as the design stated them

> 1. Can a local model drive an agentic loop with our tools on our
>    prompts? **Unmeasured.**
> 2. Does the real report prompt fit a sane context? **Unmeasured** —
>    the ~3k figure is from fixtures, not from a 1,200-game archive.
> 3. Is local prose good enough to call itself a coach? **Unmeasured.**

> **Kill criteria, agreed before starting:**
> - Real report prompt exceeds **~16k tokens**, or
> - Phase A prose is visibly worse than a mediocre human coach.
>
> Either means stop and shrink the ambition — most likely to "local
> generates profile narratives only" — rather than reaching for a
> bigger model or a longer context.

**Neither kill criterion fired.**

## The inputs, and the database hazard

Both prompts were rendered once to files and every run after that read
the files, so the input is byte-identical across all 60 Phase B runs.

| | |
|---|---|
| Archive | 8,167 stored games, 1,220 analyzed |
| Scope | widest realistic: all time controls, full history, no window |
| `report-prompt.md` | 18,598 chars, sha256 `f286792a…` |
| `explain-prompt.md` | 4,723 chars, sha256 `5217d01d…` |

The archive is **8,167 games, not the ~1,200 the design assumed** —
`build_report` aggregates, so prompt size is bounded by section count
rather than game count, but the denominators it quotes are much larger.

The explain target was chosen deterministically (most recent analyzed
game containing a blunder, at its first blunder ply): a real 2.83-pawn
blunder, `b6` played where the engine preferred `Bxe2`, with the stored
rapid player profile embedded. Five engine lines at depth 16, multipv 5.

**On the `open_db` hazard.** The design's prescribed
`sqlite3.connect("file:…?mode=ro", uri=True)` does not work against
this database: its header is WAL (`write_version=2`), and SQLite cannot
open a WAL database read-only unless the `-shm` file already exists —
a read-only connection cannot create one. What was done instead is
strictly *more* conservative:

1. a byte-identical copy (sha256 verified) into the scratchpad,
2. opened with `?immutable=1`, which needs no `-shm` and takes no
   locks,
3. `storage.Db(connection)` constructed **directly**, reusing the real
   queries and row→domain mapping without ever reaching `open_db` or
   `_apply_migrations`.

The live database's sha256 and mtime were verified unchanged after the
dump. Migration 011 remains unapplied, as intended.

## Phase A — no tools

### The number the plan was missing

**The real report prompt is 9,503 tokens** (`llm.tokenize`), plus 100
for `SYSTEM_PROMPT`.

That is **3.2× the fixture-derived ~3k estimate**, and comfortably
inside the ~16k kill criterion. With 8,192 tokens of output the whole
exchange peaked at ~16.6k of the 32,768-token context — so 32k is
enough and 16k would not be.

It also confirms the design's Ollama warning concretely: a 9,503-token
prompt against a 4,096-token default is truncated from the front,
which eats the system prompt first.

### Timings (M4 Max, 27B Q4_K_M, all layers on Metal)

| | 2048-token cap | 8192-token cap |
|---|---|---|
| Model load | 16.8 s (cold) | 1.3 s (warm page cache) |
| Prefill / TTFT | 53.77 s | 53.89 s |
| Prefill rate | 177 tok/s | 176 tok/s |
| Decode | 134.28 s | 513.77 s |
| Decode rate | 15.3 tok/s | 13.6 tok/s |
| Output | 2,048 tokens (**truncated**) | 7,001 tokens (complete) |
| **Total** | 188.05 s | **567.66 s (9 min 28 s)** |

**~177 tok/s prefill is well under the 713–885 tok/s the design's
borrowed table quotes for an M4 Max** — that table is 7B Q4_0, and
this is a 27B. The design's derived estimate of "~20 s on an M4 Max"
for a report is wrong by a factor of ~28 for this model size: the real
figure is **9.5 minutes**.

### The first run failed, and how

The 2048-token run produced **no coaching brief at all**. The model
spent the entire budget emitting planning prose ("Here's a thinking
process: 1. **Analyze User Input:** …") and was cut off mid-outline.
Recorded rather than discarded: it is the honest default-ish result,
and it is what a provider with a modest `max_tokens` would ship.

At 8192 tokens the brief appears — but of 25,839 output chars,
**20,248 (78%) are reasoning** and only 5,581 are the deliverable.

Reasoning demarcation is a trap worth stating precisely. The output
contains **no `<think>` opening tag** — the template emits it as part
of the generation prompt — but it does contain the **closing
`</think>`**. So a provider must split on the *closing* tag; searching
for `<think>` finds nothing and would let the entire reasoning dump
through as coaching prose. The 2048-token run never reached `</think>`
at all, so a truncated generation yields pure reasoning with no
delimiter anywhere.

**`enable_thinking` cannot be reached from `llama-cpp-python`.** The
GGUF's own template supports it
(`{%- if enable_thinking is defined and enable_thinking is false %}`,
i.e. thinking is on unless explicitly disabled), and
`Jinja2ChatFormatter.__call__` does forward `**kwargs` into the render
— but `Llama.create_chat_completion` has a fixed parameter list with no
template-kwargs passthrough. There is no supported public path. This
resolves the design's implementation note 2 for the P4 backend: the
answer is "you cannot, without owning the template yourself".

### Prose judgment

**The register is good and the arithmetic is faithful; the chess is
fabricated.** The kill criterion — "visibly worse than a mediocre human
coach" — is not met on style, and is arguable on content.

What it got right:

- Second person throughout, written to the student.
- **"Pawns", never "centipawns", in the brief.** (The word appears in
  the reasoning, where it is quoting the instruction back to itself.)
- **Zero invented `[gN]` handles** — 4 cited, all from the supplied 15.
- Every headline figure transcribed correctly: 1.64/8.8% last 30 days
  vs 1.68/8.9% whole span; opening 0.31/2.0%, middlegame 2.19/11.6%,
  endgame 2.15/11.3%; 1,356 timeouts, 1,321 resignations; peak 1503
  June 2026, now 1477; 52% overall and 52% after a loss; repertoire
  counts (2,238 → "over 2,200"; 2,215; 343; 57; 106) all correct.
- **All 4 cited engine-preferred moves are correct** — `Rfd8`, `e5`,
  `Rc8`, `Qh5+` each match their turning point exactly.
- A specific, well-sized two-week training plan.

What it fabricated — every one of these is invented, since the prompt
supplies only the move, the loss and the engine's preference, never a
refutation line:

- **`[g10]` is internally incoherent.** It writes that the student's
  `4.Nf3` "loses to Qh5+ … [Qh5+] maintains the initiative and wins
  material" — the same move as both the opponent's refutation and the
  student's improvement. Verified with python-chess: it is **White to
  move**, so `Qh5+` is the *student's* engine-recommended move; `f7`
  (which it claims is won) is empty; and it is not mate (Black has
  `Kd7`).
- **`[g3]`: "a forced back-rank mating sequence".** The position is a
  pure rook-and-pawn endgame — no queens, bishops or knights on the
  board — and the eval moves +0.00 → +7.29, i.e. losing, not mated.
- **`[g7]`: "cuts off the king and forces promotion".** The passed pawn
  in that position is Black's; `Rc8` stops it.
- **`[g1]`: "traps the knight and wins a full piece"** — plausible, but
  unsupplied and unverified.
- One label/number mismatch: "about forty percent … letting a piece
  drop or walking into a forced checkmate" — 40% is
  20.0% + 19.1% (slipped win + hung piece), whereas the two categories
  it names sum to 26.7%. The reasoning trace had the pairing right and
  the brief garbled the labels.

So: **3 of 4 cited positions carry an invented tactical justification,
and 1 of those 4 is provably self-contradictory** — while the numbers
around them are clean. This is precisely the pattern the design
predicts, and it is the strongest possible argument for the
python-chess faithfulness verifier listed under "Then", step 5.

## Phase B — tools on

The real `analyze_position` name, description and schema from
`coach/providers.py`, with the tool backed by the project's actual
Stockfish binary at the config defaults (depth 16, multipv 5) and its
observation text reproducing `_render_lines` exactly.

Three arms of 20 runs on prompt v1, plus a 20-run replication of arm 1
and 7 arm-3 runs on the corrected prompt v2 (see "A flaw in this
spike's own input" below). **87 tool-calling runs in total**, every one
of which called the tool correctly.

| | arm 1 `role:"tool"` | arm 2 `role:"user"` | arm 3 `role:"user"` + `tool_choice:"none"` on follow-up | arm 1 on v2 |
|---|---|---|---|---|
| Runs | 20 | 20 | 20 | 20 |
| Called `analyze_position` | **20/20** | **20/20** | **20/20** | **20/20** |
| Argument JSON parsed | 20/20 | 20/20 | 20/20 | 20/20 |
| FEN valid and parses | **20/20** | **20/20** | **20/20** | **20/20** |
| Asked about `fen_after` | **20/20** | **20/20** | **20/20** | **20/20** |
| Produced a finished answer | 0/20 | 0/20 | **0/20** | 0/20 |
| Returned `functions.analyze_position:` as text | 15/20 | 19/20 | 0/20 | 15/20 |
| Returned empty text | 5/20 | 1/20 | 0/20 | 5/20 |
| Mean wall clock per run | 35.8 s | 35.4 s | 137.8 s | 29.5 s |

### The model's tool calling is flawless

Every run, in every arm, on both prompts, emitted exactly one
well-formed call, with valid JSON, a legal FEN, and — notably — the FEN
of **`fen_after`**, byte-identical within each prompt version. That is
the correct follow-up position and precisely what the tool description
asks for. Across **87 runs: no malformed arguments, no repeat-call
loops, no hallucinated tool names, no calls omitted.**

On the evidence here, `Qwen3.6-27B Q4_K_M` decides *whether* and *what*
to call better than the design dared assume. This is the single most
load-bearing result in the spike, and it is the one the design was most
worried about.

### The library throws the answer away

Arms 1 and 2 produce no usable prose at all. The final message is
either the literal string `functions.analyze_position:` returned as
content (15/20 and 19/20) or nothing (5/20 and 1/20) — the model
re-attempting the call on the follow-up turn, with the handler
surfacing the attempt as text. The cause is in
`llama_cpp/llama_chat_format.py`, in
`chatml_function_calling` — the **only** tool-capable handler that
applies to Qwen, since the other two are functionary-specific.

Its Jinja template branches on `message.role` for `system`, `user` and
`assistant`. **There is no branch for `tool`.** A tool message
therefore renders as a bare `<|im_start|>tool\n` — no content, and no
closing `<|im_end|>`. Rendering the template directly confirms it: the
engine's analysis is absent from the prompt the model sees.

A second defect is visible in the same render: the handler builds its
Jinja environment with `autoescape=select_autoescape(["html","xml"])`,
so the model's own prior tool call is **HTML-escaped** when replayed —
`{&#34;fen&#34;:&#34;FEN&#34;}`.

So arms 1 and 2 measure 0/20 "used the returned lines" for a reason
that has nothing to do with the model: the lines never arrived.

### The workaround works, and it is expensive

Arm 3 drops `tools` from the request on the turn after the engine has
answered (`tool_choice="none"`). This is the strongest workaround a
provider could implement without owning the chat template, and it
proves the essential point: **the model receives and uses the engine
analysis.** Every run quotes the returned line back rather than
asserting a variation of its own — so the 0/20 in arms 1–2 really is
the plumbing, not the model.

At `max_tokens=1536` it does not *finish*: **0/20 runs closed their
`<think>` block**, all 20 hitting the cap mid-reasoning at
3,800–5,000 chars and 137.8 s. Raising the budget fixes that. A
single verified run at `max_tokens=4096` on prompt v2:

| | |
|---|---|
| Wall clock, both turns | **187.3 s** |
| `finish_reason` | `stop` (not truncated) |
| Total output | 10,212 chars |
| Of which the answer | **835 chars (8%)** |
| Closed `</think>` | yes |
| Cited a move from the returned lines | yes |

And the answer is *good*. Verified against python-chess and the stored
record: it names the refutation correctly (after `Nxc2`, `Qxc2` wins
the knight — legal, and in the returned lines), quotes the engine's
preferred `Rfd8` correctly, states the swing as "about 7 pawns"
against a recorded `cp_loss` of 670, and ties it to the student's own
hanging-pieces pattern in the right register.

So the full loop *can* work in-process today, with a workaround, and
produce a correct explanation. Two costs make that a poor foundation:

- **92% of the generated tokens are reasoning the user never sees**,
  and `llama-cpp-python` cannot turn thinking off (above). The
  provider pays for all of it.
- **187 s for one `explain`.** The design's own UX line is that an
  explain call "at 19 s is not [defensible] — that flow feels broken
  above a few seconds". At 187 s it is not a candidate.

### It also drives a 36 GB machine into swap

Ten sequential `max_tokens=4096` runs at `n_ctx=32768` exhausted swap:
6,869 MB of 8,192 MB used, 18% system memory free, and the run stalled
at ~3% CPU thrashing on page-ins. It was stopped after 6 completed
runs. A 16.8 GB model plus a 32k KV cache plus long generations does
not sit comfortably in 36 GB, which sharpens the design's "good
machine (32 GB+)" tier: 32 GB is the floor for loading this model, not
for running it in a long session.

### A flaw in this spike's own input, and what it cost

The v1 explain prompt was self-contradictory, and the model caught it
before the reader did — one arm-3 run spent its reasoning on, in
paraphrase, *the prompt says the student was playing white, but the
move is `b6`, which is a black pawn move*.

It was right. `analyze_game` evaluates **every ply of the game**, so
`GameAnalysis.evals` holds both players' moves; the dump script picked
"the first blunder in `evals`", which landed on the *opponent's* move
while the prompt header named the student's colour. A real explain call
is only ever anchored to a move the student clicked, so this input was
not representative.

The prompt was rebuilt (v2) selecting the first blunder whose mover
matches the student's colour — a genuine 6.7-pawn `Nxc2` where the
engine preferred `Rfd8` — and arm 1 was re-run against it. **The
results are identical on every measure**, so the flaw did not affect
any conclusion here. It is recorded because it could have, and because
"the spike measured the wrong input" is exactly the sort of thing these
reports exist to surface.

Worth noting separately: nothing in `build_move_context` or the explain
route constrains `ply` to the student's own moves. The frontend only
ever links moves the student played, so this is not a live bug — but it
is unenforced, and it is what let the dump script go wrong.

## Follow-up — `llama-server` (P3) on all three flows

Run the same day, after the P4 phases, to answer a question the spike
had deliberately left open: *can one backend carry report, explain and
chat, so the project does not have to ship two?*

`brew install llama.cpp` → **b10200 (5f55650a7)**, a **19.9 MB** package
against a 16.8 GB model, which puts the design's "the runtime is a
rounding error" point beyond argument. Started as:

```
llama-server -m Qwen3.6-27B-Q4_K_M.gguf -c 32768 -ngl 99 \
  --host 127.0.0.1 --port 8080 --jinja --reasoning-format deepseek
```

`--jinja` is **enabled by default**, confirming the design's note that
llama.cpp's own `function-calling.md` is stale on this point.

Two facts established before any generation:

- **The tokenizers agree exactly.** `/tokenize` returns **9,503** for
  the report prompt — the same figure `llm.tokenize` gave, independently
  confirming the headline Phase A number. Explain is 1,688 tokens and
  the report-scope chat seed 8,953.
- **Prompt caching is on by default.** A follow-up turn logged
  `prompt eval time = 226.77 ms / 4 tokens` — the whole seed was reused
  and only the new tokens prefilled. That is the lever the design
  values at 13–31% TTFT, working out of the box.

### It has every control P4 lacked

`--reasoning on|off`, `--reasoning-budget N`, `--chat-template-kwargs`,
and `--reasoning-format deepseek`, which routes thinking to a separate
`message.reasoning_content` field instead of into the prose. Sending
`chat_template_kwargs={"enable_thinking": false}` per request works:
**`reasoning_content` came back empty on every run below, and no
`<think>` tag ever reached the content.** That is the single thing
`llama-cpp-python` could not do at all.

Left on (the P4-equivalent configuration), it is just as slow: an
explain turn generated 2,598 and then 4,096 tokens of reasoning at
~15.6 tok/s, and one run hit the cap. **All figures below are with
thinking off**, which is the configuration a provider would actually
ship.

### Results

| Flow | Tools | Runs | Called a tool | Args parsed | Used the tool result | Finished | `<think>` leak | Mean wall clock |
|---|---|---|---|---|---|---|---|---|
| report | none | 1 | — | — | — | 1/1 | 0/1 | **172.2 s** |
| explain | 1 | 12 | **12/12** | **12/12** | **12/12** | **12/12** | 0/12 | **38.4 s** |
| chat | 4 | 3 | 3/3 | 10/10 | — | 2/3 | 0/3 | 107.7 s |

**Report — 172.2 s against P4's 567.7 s, a 3.3× speedup**, and the
shape of the output is the real gain: `finish_reason: stop`, 9,620
prompt tokens, and **all 1,641 completion tokens are the brief** rather
than 78% reasoning. 6,104 chars, no `<think>`, **no "centipawn", no
invented `[gN]` handles** (cited `[g1] [g3] [g4] [g14]`, all supplied).

**Explain — the flow P4 could not complete at all — is 12/12 clean.**
Every run called `analyze_position` exactly once, with a valid FEN for
`fen_after`, received the engine lines, and **used them in a finished
answer**, in two turns, with no reasoning tokens. 27.5–50.3 s per run
(mean 38.4 s) against P4's 187 s. This is the result that decides the
backend question.

**Chat — the weakest of the three, and the one with a real failure.**
Tool *selection* is sensible: asked "which opening am I losing the most
rating in, and can you show me a recent game where it went wrong?", it
chained `find_games` → `get_game` and produced a fluent answer with a
correctly-formed game link. All 10 calls parsed. But **one run in three
burned its budget in a repeat-call loop** — `analyze_position` four
times, twice with *byte-identical* arguments — and returned no answer.
That is exactly the failure the design's hardening item 3 (dedupe
identical `(tool, args)` calls) exists to absorb, observed in the wild.

`get_opening_stats` was never chosen, despite the question being about
openings — worth noting, on a very small sample.

### The fabrication does not go away

Same pattern as Phase A, and P3 does not fix it — nor should it be
expected to. The chat answer stated "Move 15. Bxg6", "Move 20. Bg5",
"Move 25. Nf3". Checked against the stored game: those moves were
**`Bg3`, `Nxf6` and `f4`**. All three named moves do occur somewhere in
the game, but not at the moves cited — real tokens lifted from the
supplied move sheet and reattached to invented move numbers.

Explain is not exempt. One of the 12 runs opened with "After **Nxc2**,
White simply plays **Qxc2**. **Your queen is gone** … (queen for
knight)". `Qxc2` captures a **knight**; Black's queen is on a5 and
stays there. That is the headline claim of the explanation, and it is
wrong — while the P4 probe on the *same position* got it right
("winning the knight"). So this is run-to-run variance, not a backend
difference.

This is now the fourth independent sighting of the same behaviour
(Phase A's turning points, Phase A's `[g10]` contradiction, chat's move
numbers, explain's queen), across both backends. **The python-chess
faithfulness verifier is the mitigation, and none of this provider work
substitutes for it.** Note what it would have caught in each case: a
piece named that is not on the named square, and a move attributed to
the wrong side or the wrong move number — both cheap, local checks.

## Verdict

**Both Step 0 kill criteria pass.** The prompt is 9,503 tokens against
a ~16k ceiling, and the prose is not worse than a mediocre human coach
in register or arithmetic — though it fabricates chess.

On the design's own branch under "After the spike":

> - **Tool calling unreliable on P4** → re-run *Phase B only* against
>   a throwaway `llama-server` with the same model and prompts. That
>   disambiguates the one thing a single-stack spike cannot: whether
>   the fault is the generic `chatml-function-calling` handler or the
>   model and our prompts.

That disambiguation was first settled at the source level — the fault
is provably **the handler** — and then confirmed empirically: the
`llama-server` follow-up ran the same prompts through the same model
and **completed every flow P4 could not**. The model was never the
problem; 87/87 correct tool calls on P4 said so, and P3's 12/12 clean
explain runs close it.

**One backend is enough, and it is `llama-server`.** It carries report,
explain and chat on the same process, so the project does not need to
ship both shapes.

The nuance the branch did not anticipate is that P4 is not simply
*broken*. With the workaround it completes the whole loop and writes a
correct explanation. It is ruled out on **cost**, not capability:
187 s per `explain`, 92% of it invisible reasoning that cannot be
switched off through this API, on a machine that swaps under the load.
`llama-server` reaches `enable_thinking`, `num_ctx` and prompt caching,
and derives its tool grammars from the model's own template — which is
to say it addresses all three of the costs, not just the handler bug.

## What was NOT measured

Stated plainly, because these are the things a reader could mistake for
measured:

- **The 9B and 4B walk-down was not run, and is deliberately deferred.**
  The design's "start at the ceiling, walk down until it breaks" stays
  unstarted by decision on 2026-07-31, not by oversight — so **the
  minimum spec remains guessed, not discovered, and the guess should
  not be quoted as though this spike produced it.** Nothing in the
  build plan depends on it: the walk-down answers "what is the smallest
  machine this runs on", which is a question about *other people's*
  hardware, and it only becomes worth running when a hardware
  requirement is actually published. The harness and the pinned prompts
  are in place if that changes, so re-running it is a download and one
  pass per model.
- **`chat` rests on 3 runs.** Enough to see a repeat-call loop, nowhere
  near enough for a rate. `render_chat_prompt` replay and session
  resume were not exercised at all, and the chat toolkit's other three
  tools were backed by throwaway SQL over the DB copy rather than by
  the real `ChatToolkit`.
- **No streaming anywhere.** Every run used `stream=False`, so the
  fragmented-tool-call-argument path — the accumulator's hardest case,
  and the one the design says only a real HTTP backend can exercise —
  is still untouched. P3 is where that test now lives.
- **Prompt caching was observed, not benchmarked.** A 4-token prefill
  on a cached follow-up proves it is on; the design's 13–31% TTFT claim
  was not measured.
- **`--reasoning off` and `--reasoning-budget` were not tried.** Only
  the per-request `chat_template_kwargs` route was, and it worked.
- **The report flow is one run on P3**, so its 172.2 s has no variance
  behind it.
- **One prompt pair, one player, one position.** All 60 Phase B runs
  used the same explain prompt and all Phase A runs the same report
  prompt. Hit rates describe this input, not the input distribution.
- **Prose was judged once**, by one reader, on one 8192-token
  generation. The fabrication findings are verified against python-chess
  and the source prompt; the "good register" judgment is not a rubric.
- **The finished `explain` answer is a sample of one.** The 187 s
  end-to-end run that closed `</think>` and produced a correct
  explanation was a single probe, run after the 10-run arm-3
  replication was stopped by memory exhaustion. Its answer is verified;
  its *rate* is not. Nothing here says how often that path produces a
  correct explanation rather than a plausible one.
- **The `max_tokens=4096` arm-3 replication did not complete** — 6 of
  10 runs finished before swap exhaustion stopped it, and their
  per-run records were never written, so only the console counts
  survive for those.
- **Temperature 0.7 throughout**, never swept. Tool calling generally
  wants lower, and the design flags a temperature decision as an open
  `LlmConfig` question.
- **Arms 1 and 2's failure taxonomy was under-counted as recorded.**
  The harness's detector looked for `<tool_call>` and fenced JSON, not
  this handler's own `functions.<name>:` convention, so it logged an
  empty taxonomy. Re-scored by hand: 15/20 tool-as-text, 5/20 empty.
  The corrected figures are the ones quoted above.
