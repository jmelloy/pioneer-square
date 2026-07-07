"""Pioneer Square unified CLI.

A single ``pioneer`` entry point that launches any of the project's three
runtimes:

* ``pioneer serve``   — the FastAPI HTTP backend (was ``python main.py``)
* ``pioneer foreman`` — the standalone Foreman API proxy (was ``pioneer-foreman``)
* ``pioneer worker``  — a worker agent (was ``pioneer-worker``)

The three runtimes still live in their own source trees (``backend/``,
``foreman/``, ``worker/``); this launcher just puts the right directory on
``sys.path`` for the selected mode and hands off to the existing entry point.
"""

__version__ = "0.1.0"
