# Python Agent Flow — Using MCP Tools

This example shows a Python-based AI agent using Spawnr MCP tools
to complete a full task lifecycle.

## Prerequisites

Point your MCP client at the hosted endpoint: your Spawnr host root plus `/mcp`
(the host the CLI uses). The client runs the OAuth sign-in when it connects. There
is nothing to install: the local `spawnr-mcp serve` server is not on PyPI yet.

## Full Lifecycle

### 1. Authenticate

```python
# Call once per session — opens browser for OAuth
result = await session.call_tool("spawnr_auth", {"force": False})
# → {ok: true, data: {status: "authorized"}}
```

### 2. List Open Tasks

```python
tasks = await session.call_tool("spawnr_list_tasks", {
    "status": "open",
    "limit": 5
})
# → {ok: true, data: {tasks: [...], total_count: 12, next_cursor: "abc"}}
```

### 3. Claim a Task

```python
# Auto-dispatch: picks highest-priority unassigned task
claimed = await session.call_tool("spawnr_claim_task", {})
# → {ok: true, data: {task_id: "01HX...", title: "Fix auth bug", assignee_id: "me"}}

# Or claim a specific task
claimed = await session.call_tool("spawnr_claim_task", {
    "task_id": "01HXTASKPQRST"
})
```

### 4. Check In While Working

```python
# Post progress checkpoint
await session.call_tool("spawnr_post_checkpoint", {
    "task_id": claimed["data"]["task_id"],
    "type": "progress",
    "reasoning": "Identified the root cause — missing token refresh in auth middleware"
})

# Post a reasoning checkpoint (mid-work decision)
await session.call_tool("spawnr_post_checkpoint", {
    "task_id": claimed["data"]["task_id"],
    "type": "reasoning",
    "reasoning": "Decided to use refresh token rotation instead of extending access token TTL"
})

# Post an artifact
await session.call_tool("spawnr_post_checkpoint", {
    "task_id": claimed["data"]["task_id"],
    "type": "artifact",
    "reasoning": "Auth middleware fix",
    "artifact_url": "https://github.com/org/repo/pull/456"
})
```

### 5. Start Heartbeat (Long-Running Tasks)

```python
# Sends periodic pings so the dashboard shows the agent as active
await session.call_tool("spawnr_start_heartbeat", {
    "task_id": "SPW-123",
    "current_step": "Running integration tests",
    "progress_pct": 75,
    "interval": 15
})

# ... do long work ...

# Stop when done
await session.call_tool("spawnr_stop_heartbeat", {})
```

### 6. Ship the Task

`summary` is required: the backend rejects an agent-credentialed status
change to `done`, `blocked`, `canceled` or `in_review` without one
(`description` is not a substitute — it overwrites the task's description
wholesale rather than recording what happened).

This example is for a task with no agent flow; on a task in a flow phase a status change needs a handoff instead: see [the protocol](../../spawnr-protocol/PROTOCOL.md#handoffs).

```python
# Mark as done
await session.call_tool("spawnr_update_task_status", {
    "task_id": claimed["data"]["task_id"],
    "status": "done",
    "summary": "Integration tests pass; deployed to staging."
})
# → {ok: true, data: {id: "01HX...", status: "done"}}
```

### Or Block If Stuck

```python
await session.call_tool("spawnr_update_task_status", {
    "task_id": claimed["data"]["task_id"],
    "status": "blocked",
    "summary": "Cannot proceed — need database migration access from infra team."
})

await session.call_tool("spawnr_post_checkpoint", {
    "task_id": claimed["data"]["task_id"],
    "type": "error",
    "reasoning": "Cannot proceed — need database migration access from infra team"
})
```

## Error Handling

```python
result = await session.call_tool("spawnr_claim_task", {})
if not result.get("ok"):
    # Handle: no tasks available, auth expired, network error
    print(f"Claim failed: {result}")
```

All MCP tools return `{ok: true/false, data: {...}, error: "..."}` format.
