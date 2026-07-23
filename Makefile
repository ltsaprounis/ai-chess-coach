ENGINE_SRC := engines/stockfish/src

.PHONY: engine
engine:
	git submodule update --init engines/stockfish
	$(MAKE) -C $(ENGINE_SRC) -j build
	$(ENGINE_SRC)/stockfish --help >/dev/null 2>&1 || true
	@echo "Stockfish binary: $(ENGINE_SRC)/stockfish"

.PHONY: check
check:
	cd backend && uv run ruff check . && uv run ruff format --check . \
		&& uv run pyright \
		&& uv run lint-imports --cache-dir .cache/import_linter \
		&& uv run pytest

.PHONY: dev-api
dev-api:
	@cd backend && PORT=$$(uv run python -c "from chess_coach.config \
		import load_config; print(load_config().server.port)") && \
		echo "AI Chess Coach → http://localhost:$$PORT" && \
		uv run uvicorn --factory chess_coach.api:create_app \
			--reload --port $$PORT

.PHONY: gen-api
gen-api:
	cd backend && uv run python -c "import json; \
		from chess_coach.api import create_app; \
		print(json.dumps(create_app().openapi()))" > ../web/openapi.json
	cd web && pnpm gen:api
