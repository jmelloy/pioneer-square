# Repository Guidelines

## Project Structure & Module Organization

Pioneer Square is split into three main runtimes. `backend/` contains the FastAPI app, SQLite/Alembic schema, WebSocket handlers, and Foreman logic under `backend/foreman/`. `frontend/` is a Vue 3 + Pinia + Vite app; source lives in `frontend/src/`, static files in `frontend/public/`, and end-to-end tests in `frontend/tests/e2e/`. `worker/` contains the standalone Python worker package in `worker/pioneer_worker/` and its tests in `worker/tests/`. Shared operational docs are in `docs/`, prompt text in `prompts/`, and helper scripts in `scripts/`.

## Build, Test, and Development Commands

- `docker compose up --build`: run the backend and SPA quickstart.
- `cd backend && uvicorn main:app --reload --port 8000`: run the API locally after installing `backend/requirements.txt`.
- `cd frontend && npm run dev`: start the Vite dev server at `http://localhost:5173`.
- `cd frontend && npm run build`: type-check and build the frontend.
- `cd worker && pip install -e . && pioneer-worker`: install and run a worker using `pioneer-worker.toml`.
- `ruff check .` and `ruff format .`: lint and format Python from the repo root.

## Coding Style & Naming Conventions

Python targets 3.11 with Ruff configured in `ruff.toml` using 100-character lines, import sorting, pyupgrade, and bugbear rules. Use `snake_case` for Python modules, functions, and tests. Frontend code uses TypeScript, Vue single-file components, ESLint, and Prettier; use `PascalCase.vue` for components, `camelCase` for utilities and store members, and keep Pinia stores in `frontend/src/stores/`.

## Testing Guidelines

Backend and worker tests use pytest with `asyncio_mode = auto`; run `python -m pytest` from `backend/` or `worker/`. Frontend unit tests use Vitest: run `npm test` from `frontend/`, or `npm run test:coverage` for coverage. Name Python tests `test_*.py`; place frontend specs as `*.spec.ts` near the code or under `src/**/__tests__/`. Add tests for WebSocket flows, task lifecycle changes, and store updates when touching those paths.

## Commit & Pull Request Guidelines

Recent history uses concise imperative commits, often Conventional Commit style such as `fix(worker): ...`, `style: ...`, or short maintenance messages like `ruff`. Keep commits focused and mention the subsystem when helpful. Pull requests should include a brief problem/solution summary, test commands run, linked issues or tasks, and screenshots for visible frontend changes.

## Security & Configuration Tips

Do not commit secrets or local state. Use `backend/.env` for GitHub OAuth values, copy `worker/pioneer-worker.toml.example` before editing worker configuration, and keep `pioneer_square.db` and generated build output out of review unless explicitly required.
