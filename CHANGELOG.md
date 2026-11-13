# Changelog

## Unreleased

- **AgentTaskTracker#2535** — An agent key may comment only on a task it holds or is mentioned on (epic 031 D7); any other task is 403 `AGENT_NOT_PERMITTED`. The skills teach the rule and `mention_agent_ids` on `spawnr_comment_on_task`. ([#19](https://github.com/spawnrapp/spawnr-skills/pull/19))

## 1.0.0 — 2026-10-18

The agent-flow release (epic 031): the three skills share `skills/spawnr-protocol/PROTOCOL.md`; install it beside any skill.

- **US-031.1** — The skills teach what an agent key may no longer do: delete a task, write an epic, create a top-level task, reassign one, or add a dependency between tasks it neither holds nor created. A claim on a task someone else holds is 409 `already_claimed`, and a reassign is 403 `agent_cannot_reassign`; neither is retried. ([#3](https://github.com/spawnrapp/spawnr-skills/pull/3))
- **US-031.2** — Roles, areas and needs on an agent's capabilities, and 409 `capability_mismatch` when a claim lacks one. ([#4](https://github.com/spawnrapp/spawnr-skills/pull/4))
- **US-031.3** — No skills change. Flows are defined by a person in Settings; the agent-facing `flow` parameter is US-031.4.
- **US-031.4** — Choosing a flow while claiming (`flow`), and 403 `flow_not_allowed` when the flow is not open to agents. ([#7](https://github.com/spawnrapp/spawnr-skills/pull/7))
- **US-031.5** — Handoffs between flow phases: five fields on every phase change, 422 `handoff_required`, 409 `role_mismatch`, and the openclaw `handoff` command. ([#8](https://github.com/spawnrapp/spawnr-skills/pull/8))
- **US-031.6** — The agent loop is wait, act, hand off, on the agent inbox (`spawnr_wait_for_updates`, `spawnr inbox wait`, `poll_tasks.py`). It also folds in US-9.2.100 (`spawnr inbox wait`, [#10](https://github.com/spawnrapp/spawnr-skills/pull/10)) and #2434: a `task.created` for a task already assigned to you is yours, not an offer ([#12](https://github.com/spawnrapp/spawnr-skills/pull/12)). ([#9](https://github.com/spawnrapp/spawnr-skills/pull/9))
- **US-031.7** — Proposals: an agent adds work as a pending subtask, and 409 `proposal_not_approved` when it claims one early. ([#6](https://github.com/spawnrapp/spawnr-skills/pull/6))
- **US-031.8** — Caps, Advisor gates and escalation, and the workspace pause. `blocked_by_cap` (AgentTaskTracker#2437) is documented in US-031.9 PR 2. ([#11](https://github.com/spawnrapp/spawnr-skills/pull/11))
- **US-031.9** — The skills moved to this repo ([#1](https://github.com/spawnrapp/spawnr-skills/pull/1)). The protocol release adds the shared protocol with an Advisor playbook and a table of every refusal code with what to do, the Coder to Tester worked examples for `spawnr-mcp` and `openclaw`, and `scripts/check_links.py`. `poll_tasks.py` takes `--timeout` instead of `--interval` (the old name still works), and its handler now gets the task detail, so it can read the pinned handoff.
