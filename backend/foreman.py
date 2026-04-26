"""
ForemanService: the AI orchestrator that lives in the backend.

Triggered by:
  - Chat messages directed to "foreman"
  - agent-done notifications from completed worker subprocesses

Uses the Claude API (tool use) to:
  - Reply to the human
  - Assign tasks to worker agents
  - Manage a GitHub Projects v2 board (create issues, move cards, comment)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

import anthropic
import httpx

logger = logging.getLogger(__name__)

FOREMAN_MODEL = "claude-opus-4-7"
MAX_HISTORY = 60   # messages to keep in memory before trimming oldest pairs
IDLE_TIMEOUT = 600  # seconds before the loop exits (restarts on next trigger)

TOOLS = [
    {
        "name": "reply",
        "description": "Send a message back to the human in the session chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The message to display in chat."}
            },
            "required": ["content"],
        },
    },
    {
        "name": "list_agents",
        "description": "Return the current list of registered worker agents and their states.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "assign_task",
        "description": (
            "Start an AI coding agent on a task. The agent streams its output to its terminal pane. "
            "Call list_agents first to find an idle agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "ID of the worker agent to use."},
                "tool": {
                    "type": "string",
                    "enum": ["claude", "codex", "pi"],
                    "description": "Which AI coding tool to run.",
                },
                "prompt": {"type": "string", "description": "The task prompt for the agent."},
                "model": {"type": "string", "description": "Optional model override (e.g. claude-opus-4-7)."},
            },
            "required": ["agent_id", "tool", "prompt"],
        },
    },
    {
        "name": "github_create_issue",
        "description": "Create a GitHub issue in the configured repository. Returns the issue number and node_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown body. Include task details, acceptance criteria, etc."},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Optional label names."},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "github_add_to_project",
        "description": "Add a GitHub issue to the session's configured project board. Returns the project item ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_node_id": {"type": "string", "description": "The node_id returned by github_create_issue."}
            },
            "required": ["issue_node_id"],
        },
    },
    {
        "name": "github_set_status",
        "description": (
            "Set the Status field of a project board item. "
            "Common values: 'Todo', 'In Progress', 'Done'. "
            "The exact option names depend on the project's configuration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Project item ID from github_add_to_project."},
                "status": {"type": "string", "description": "Status option name."},
            },
            "required": ["item_id", "status"],
        },
    },
    {
        "name": "github_comment_issue",
        "description": "Add a comment to a GitHub issue (e.g. agent output summary, follow-up notes).",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "body": {"type": "string", "description": "Markdown comment body."},
            },
            "required": ["issue_number", "body"],
        },
    },
]


def _system_prompt(agents: list[dict], config: dict) -> str:
    agent_lines = "\n".join(
        f"  - {a['name']} (id={a['id']}, state={a['state']})" for a in agents
    ) or "  (none registered yet — ask the human to register workers)"

    gh_parts = []
    if config.get("github_token"):
        if config.get("github_repo"):
            gh_parts.append(f"repo={config['github_repo']}")
        if config.get("github_project_id"):
            gh_parts.append(f"project={config['github_project_id']}")
    gh_status = ", ".join(gh_parts) if gh_parts else "not configured (session settings)"

    return f"""\
You are the Foreman of Pioneer Square, a software workshop that dispatches AI coding agents.

Your responsibilities:
1. Understand requests from the human and decompose them into concrete tasks.
2. Track each task as a GitHub issue and move it through the project board.
3. Assign tasks to idle worker agents with assign_task.
4. When an agent reports completion, review its output summary and decide:
   - Task complete → update status to Done, comment the summary, report to human.
   - More work needed → assign follow-up tasks (same or different agent).
   - Error → diagnose, retry if sensible, otherwise escalate to human.

Registered worker agents:
{agent_lines}

GitHub: {gh_status}

Guidelines:
- Always reply to the human to acknowledge what you're doing.
- Create a GitHub issue before assigning work so every task is tracked.
- Keep chat replies concise; put detail in GitHub issue bodies/comments.
- If GitHub is not configured, still manage work but note tracking is unavailable.
- Prefer idle agents; do not double-assign a busy agent without asking.
- If no agents are idle, tell the human and wait."""


class ForemanService:
    def __init__(
        self,
        session_id: str,
        broadcast_fn: Callable,
        get_db_fn: Callable,
        spawn_agent_fn: Callable,  # async (session_id, agent_id, tool, prompt, model) -> None
    ):
        self.session_id = session_id
        self._broadcast = broadcast_fn
        self._get_db = get_db_fn
        self._spawn_agent = spawn_agent_fn

        self.messages: list[dict] = []
        self._task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._gh_field_cache: dict = {}  # project_id -> {field_id, options}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def notify(self, event: dict):
        """Enqueue a trigger (chat or agent_done) and ensure the loop is running."""
        self._queue.put_nowait(event)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self):
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            try:
                await self._process(event)
            except Exception:
                logger.exception("Foreman error processing event %s", event.get("type"))

    async def _process(self, event: dict):
        config = await self._get_config()
        agents = await self._get_agents()

        if event["type"] == "chat":
            self.messages.append({"role": "user", "content": event["content"]})
        elif event["type"] == "agent_done":
            name = event.get("agent_name", event.get("agent_id", "?"))
            tool = event.get("tool", "?")
            ok = event.get("exit_code", 0) == 0
            summary = event.get("summary", "(no output)")
            outcome = "completed successfully" if ok else "finished with an error"
            self.messages.append({
                "role": "user",
                "content": (
                    f"[System] Agent '{name}' ({tool}) {outcome}.\n"
                    f"Output summary:\n```\n{summary}\n```"
                ),
            })

        await self._set_state("thinking")
        try:
            await self._run_turn(config, agents)
        finally:
            await self._set_state("idle")

        # Trim history to avoid unbounded growth
        if len(self.messages) > MAX_HISTORY:
            self.messages = self.messages[-MAX_HISTORY:]

    async def _run_turn(self, config: dict, agents: list[dict]):
        client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        system = _system_prompt(agents, config)
        messages = list(self.messages)

        while True:
            response = await client.messages.create(
                model=FOREMAN_MODEL,
                max_tokens=4096,
                system=system,
                tools=TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await self._execute_tool(block.name, block.input, config)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_results})

        self.messages = messages

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, inputs: dict, config: dict) -> dict:
        try:
            if name == "reply":
                return await self._tool_reply(inputs["content"])
            if name == "list_agents":
                return {"agents": await self._get_agents()}
            if name == "assign_task":
                return await self._tool_assign(inputs, config)
            if name == "github_create_issue":
                return await self._gh_create_issue(config, inputs)
            if name == "github_add_to_project":
                return await self._gh_add_to_project(config, inputs)
            if name == "github_set_status":
                return await self._gh_set_status(config, inputs)
            if name == "github_comment_issue":
                return await self._gh_comment_issue(config, inputs)
            return {"error": f"Unknown tool: {name}"}
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return {"error": str(exc)}

    async def _tool_reply(self, content: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        await self._broadcast(self.session_id, {
            "type": "chat",
            "from": "foreman",
            "to": "user",
            "content": content,
            "createdAt": now,
        })
        db = await self._get_db()
        try:
            await db.execute(
                "INSERT INTO messages (session_id, from_agent, to_agent, content, message_type, created_at)"
                " VALUES (?, 'foreman', 'user', ?, 'chat', ?)",
                (self.session_id, content, now),
            )
            await db.commit()
        finally:
            await db.close()
        return {"ok": True}

    async def _tool_assign(self, inputs: dict, config: dict) -> dict:
        agent_id = inputs["agent_id"]
        tool = inputs["tool"]
        prompt = inputs["prompt"]
        model = inputs.get("model")
        await self._spawn_agent(self.session_id, agent_id, tool, prompt, model)
        return {"ok": True, "agent_id": agent_id, "tool": tool}

    # ------------------------------------------------------------------
    # GitHub helpers
    # ------------------------------------------------------------------

    def _gh_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _require_gh(self, config: dict, need_project: bool = False) -> tuple[str, str]:
        token = config.get("github_token", "")
        repo = config.get("github_repo", "")
        if not token or not repo:
            raise ValueError("GitHub token and repo are not configured for this session.")
        if need_project and not config.get("github_project_id"):
            raise ValueError("GitHub project ID is not configured for this session.")
        return token, repo

    async def _gh_graphql(self, token: str, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.github.com/graphql",
                headers={**self._gh_headers(token), "Content-Type": "application/json"},
                json={"query": query, "variables": variables or {}},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                raise RuntimeError("; ".join(e["message"] for e in data["errors"]))
            return data.get("data", {})

    async def _gh_create_issue(self, config: dict, inputs: dict) -> dict:
        token, repo = self._require_gh(config)
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=self._gh_headers(token),
                json={
                    "title": inputs["title"],
                    "body": inputs.get("body", ""),
                    "labels": inputs.get("labels", []),
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            return {"issue_number": data["number"], "node_id": data["node_id"], "url": data["html_url"]}

    async def _gh_add_to_project(self, config: dict, inputs: dict) -> dict:
        token, _ = self._require_gh(config, need_project=True)
        project_id = config["github_project_id"]
        data = await self._gh_graphql(token, """
            mutation Add($projectId: ID!, $contentId: ID!) {
              addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                item { id }
              }
            }
        """, {"projectId": project_id, "contentId": inputs["issue_node_id"]})
        return {"item_id": data["addProjectV2ItemById"]["item"]["id"]}

    async def _gh_set_status(self, config: dict, inputs: dict) -> dict:
        token, _ = self._require_gh(config, need_project=True)
        project_id = config["github_project_id"]

        # Fetch and cache the Status field + option IDs for this project
        if project_id not in self._gh_field_cache:
            data = await self._gh_graphql(token, """
                query Fields($id: ID!) {
                  node(id: $id) {
                    ... on ProjectV2 {
                      fields(first: 30) {
                        nodes {
                          ... on ProjectV2SingleSelectField {
                            id name
                            options { id name }
                          }
                        }
                      }
                    }
                  }
                }
            """, {"id": project_id})
            fields = data["node"]["fields"]["nodes"]
            status_field = next(
                (f for f in fields if isinstance(f, dict) and f.get("name", "").lower() == "status"),
                None,
            )
            if not status_field:
                raise ValueError("No 'Status' single-select field found in the project.")
            self._gh_field_cache[project_id] = {
                "field_id": status_field["id"],
                "options": {opt["name"].lower(): opt["id"] for opt in status_field["options"]},
                "option_names": [opt["name"] for opt in status_field["options"]],
            }

        cached = self._gh_field_cache[project_id]
        status_lower = inputs["status"].lower()
        option_id = cached["options"].get(status_lower)
        if not option_id:
            return {"error": f"Status '{inputs['status']}' not found. Available: {cached['option_names']}"}

        await self._gh_graphql(token, """
            mutation SetStatus($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $projectId
                itemId: $itemId
                fieldId: $fieldId
                value: { singleSelectOptionId: $optionId }
              }) { projectV2Item { id } }
            }
        """, {
            "projectId": project_id,
            "itemId": inputs["item_id"],
            "fieldId": cached["field_id"],
            "optionId": option_id,
        })
        return {"ok": True, "status": inputs["status"]}

    async def _gh_comment_issue(self, config: dict, inputs: dict) -> dict:
        token, repo = self._require_gh(config)
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/issues/{inputs['issue_number']}/comments",
                headers=self._gh_headers(token),
                json={"body": inputs["body"]},
                timeout=20,
            )
            r.raise_for_status()
            return {"ok": True, "comment_id": r.json()["id"]}

    # ------------------------------------------------------------------
    # State / DB helpers
    # ------------------------------------------------------------------

    async def _set_state(self, state: str):
        await self._broadcast(self.session_id, {
            "type": "agent-state",
            "agentId": "foreman",
            "state": state,
        })
        db = await self._get_db()
        try:
            await db.execute(
                "UPDATE agents SET state = ? WHERE id = 'foreman' AND session_id = ?",
                (state, self.session_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def _get_config(self) -> dict:
        db = await self._get_db()
        try:
            async with db.execute("SELECT * FROM sessions WHERE id = ?", (self.session_id,)) as cur:
                row = await cur.fetchone()
            return dict(row) if row else {}
        finally:
            await db.close()

    async def _get_agents(self) -> list[dict]:
        db = await self._get_db()
        try:
            async with db.execute(
                "SELECT id, name, type, state FROM agents WHERE session_id = ? AND type != 'foreman'",
                (self.session_id,),
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()
