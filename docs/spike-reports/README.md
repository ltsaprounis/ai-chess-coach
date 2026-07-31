# Spike reports

Measurements from time-boxed experiments run to answer a question a
design could not answer from sources. One file per spike.

A spike report exists because a plan was **gated on a number nobody
had**. It records what was run, on what hardware, and what came out
— including when the answer was "this does not work". A spike whose
result is only remembered in a chat log has not been run.

These are neither designs nor closed-out history:

- [future-improvements/](../future-improvements/) holds the design
  a spike serves. The design states the question and the stopping
  rule; the report answers it, and the design is then updated to
  cite the report rather than restating its numbers.
- [archive/](../archive/README.md) holds work that is finished.
  A spike report moves there only if its design is abandoned or
  built out entirely — until then it is live evidence.

## What a report must carry

Numbers without their conditions are not reusable, and most of this
project's latency and quality figures are currently someone else's
hardware. A report states:

- **Date, hardware, and exact versions** — chip and memory, package
  version, model id and quantization, every non-default setting.
  A configuration footgun silently invalidates a result: the local
  LLM spike has three, and any one of them produces a false
  negative.
- **The question and the stopping rule** as the design stated them,
  quoted, so a reader can tell whether the spike answered what it
  set out to.
- **The raw results**, including runs that failed. Hit rates beat
  impressions: "14 of 20 calls produced a valid FEN" is evidence,
  "tool calling seemed flaky" is not.
- **The verdict and what it changes** — which branch of the design
  the numbers select, in the design's own vocabulary.
- **What was not measured**, plainly. An unmeasured thing read as a
  measured one is the failure mode these reports exist to prevent.

## Reports

- [local-llm-provider.md](local-llm-provider.md) — 2026-07-31,
  M4 Max / 36 GB. Step 0 of
  [local-llm-provider.md](../future-improvements/local-llm-provider.md):
  `llama-cpp-python` + Qwen3.6-27B Q4_K_M against the real report and
  explain prompts. Both kill criteria pass — the real report prompt is
  9,503 tokens and the prose holds up — and the model's tool calling is
  perfect at 60/60 valid calls. The blocker is the library: the only
  applicable tool handler drops every tool result.
