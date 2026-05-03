"""HTTP route modules for the FastAPI backend.

Each submodule defines an ``APIRouter`` that ``main.py`` includes on the app
at startup.  Splitting by resource keeps each file focused on one topic and
lets the dependency graph be obvious — auth.py knows about github_tokens
and login_tokens, workers.py knows about the workers/agents tables, etc.
"""
