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
		&& uv run pyright && uv run lint-imports && uv run pytest

.PHONY: dev-api
dev-api:
	cd backend && uv run uvicorn --factory chess_coach.api:create_app \
		--reload --port 8000
