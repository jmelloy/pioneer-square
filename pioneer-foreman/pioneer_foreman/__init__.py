"""Pioneer Square standalone foreman agent.

Connects to the backend over WebSocket, receives ``foreman-trigger`` events,
and runs the Foreman AI (Claude) to coordinate worker agents. All state I/O
goes through the backend REST API — no direct database access.
"""

__version__ = "0.1.0"
