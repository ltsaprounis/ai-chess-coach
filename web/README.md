# web/ — AI Chess Coach frontend

Vite + React + TypeScript UI for the AI Chess Coach backend.

The build plan, interface contract, and page inventory live in
[docs/08-frontend.md](../docs/08-frontend.md); toolchain and style
rules in [docs/GUIDELINES.md](../docs/GUIDELINES.md). Common
commands:

- `pnpm dev` — dev server (proxies `/api` to the backend)
- `pnpm gen:api` — regenerate API types from `openapi.json`
- `pnpm lint` / `pnpm format` — Biome (the only lint/format tool)
- `pnpm typecheck` — `tsc` strict, no emit
- `pnpm test` — Vitest
- `pnpm build` — type-check + production build
