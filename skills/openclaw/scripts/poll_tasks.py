#!/usr/bin/env python3
"""
poll_tasks — long-running worker loop that drives Spawnr tasks through an
actionable -> claimed -> done pipeline, one legal workflow transition at a
time.

Each round lists the tasks this agent holds and works them, then WAITS on the
agent's inbox (`GET /agents/{id}/inbox/wait`) for up to `--interval` seconds
instead of sleeping, so an offered phase, a comment on a task it holds or an
answer to its question wakes the loop within a second. See `_handle_inbox_event`
for what each event does.

Pass `--handler module:function` pointing at a callable that accepts the
task dict and returns a result dict:

    def handle(task: dict) -> dict:
        # ... do the work ...
        return {"status": "done"}          # or blocked

If the callable raises, the task is moved toward `blocked` (if its workflow
has a legal path there) with the exception message, so a crash doesn't leave
the task silently stuck.

A task the agent holds in an agent-flow phase (`phase_role` set) is not claimed
again. The handler may return `{"status": "testing", "handoff": {"done": ...,
"not_done": ..., "decisions": ..., "open_questions": ..., "for_next": ...}}`; with
no `status` the task moves to the flow's next phase (else `done`), and any handoff
field left out is filled with "Not reported by the handler.".

Environment variables:
  SPAWNR_API_URL        required
  SPAWNR_API_KEY        required
  SPAWNR_WORKSPACE_ID   optional; narrows to a single workspace

Event lines: a comment or an answer is printed to stdout as one JSON line,
`{"event": "task.comment_added" | "question.answered", "data": {...}}`; the
handler is not called for it. Work results stay `{"task_id": ..., "result": ...}`.

Example:
  poll_tasks.py --handler my_agent:handle --interval 30
  poll_tasks.py --handler my_agent:handle --statuses backlog,needs_triage
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import socket
import sys
import time
import urllib.parse
from collections import deque
from typing import Any, Callable, Optional

# Reuse the CLI's request helper — same auth, retries, 429 handling.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
from spawnr_client import (  # noqa: E402
    _HANDOFF_FIELDS,
    _MAX_TASK_SUMMARY_LEN,
    _TASKS_MINE_SERVER_CAP,
    _my_agent_id,
    _request,
    _warn_unknown_status,
)


_stopping = False


def _stop(_signo: int, _frame: Any) -> None:
    global _stopping
    _stopping = True
    sys.stderr.write("\npoll_tasks: received signal, draining in-flight task...\n")


def _load_handler(spec: str) -> Callable[[dict], dict]:
    try:
        module_name, func_name = spec.split(":", 1)
    except ValueError:
        sys.stderr.write("error: --handler must be 'module:function'\n")
        sys.exit(2)
    mod = importlib.import_module(module_name)
    try:
        return getattr(mod, func_name)
    except AttributeError:
        sys.stderr.write(f"error: {module_name}.{func_name} not found\n")
        sys.exit(2)


# #1233: hardcoding "open" left a poller on the bug or story default
# workflow template with ZERO claimable work, forever — `"open"` is in
# `VALID_STATUSES` (backend/app/api/routes/tasks.py), the WRITE-accepted
# vocabulary, but the STORED vocabulary is open-ended: `apply_template`
# (backend/app/api/routes/workflows.py) `$set`s a template's own statuses via
# `update_many` with no validator, and `GET /tasks/mine` has no `type` filter
# to exclude them. The built-in templates alone produce `draft`/`ready`
# (story/default) and `new`/`triaged`/`fixing`/`closed`/`wont_fix`
# (bug/default) — none of which are `"open"`.
#
# The honest replacement question is "is this task actionable", not "is it
# open". This script is stdlib-only and outside the backend package, so it
# can't ask the workflow engine directly — it approximates by taking every
# status keyed `"category": "todo"` across the built-in `WORKFLOW_TEMPLATES`
# (backend/app/api/routes/workflows.py):
#
#   task/default & task/kanban  -> open
#   story/default & story/kanban -> draft, ready | open
#   bug/default & bug/kanban     -> new, triaged | open
#
# union: open, draft, ready, new, triaged.
#
# This is deliberately narrower than "not terminal". A not-terminal
# denylist would also admit `in_progress`, `blocked`, and `in_review` —
# categories "active", not "todo" — and that livelocks the poller instead of
# starving it: a failed handler moves a task toward `blocked`, so if
# `blocked` counted as actionable the very next 30s cycle would re-claim and
# immediately re-fail the same task forever. Reclaiming `in_review` is worse
# than a livelock — it would PATCH a task a human is actively reviewing
# straight back toward `in_progress`. On the ORIGINAL `task/default`
# template, `open` is the only `"todo"`-category key, so this rule is a
# strict superset of the old behavior everywhere it used to work.
#
# What this still gets wrong, on purpose rather than silently: a workspace
# on a CUSTOM workflow (a `StatusDef` the org authored itself, not one of
# the six built-in templates above) has status keys this script has never
# seen and cannot categorize — those tasks are invisible to the default set,
# same failure shape as the bug this fix closes, just for a smaller
# population. `--statuses` below is the escape hatch for that case.
#
# Pinned by backend/tests/test_openclaw_poll_actionable_statuses.py, which
# derives the "todo"-category union live from WORKFLOW_TEMPLATES so a new
# built-in template status fails there instead of quietly starving pollers
# in the field.
_ACTIONABLE_STATUSES = frozenset({"open", "draft", "ready", "new", "triaged"})


# #1256: `_claim_path` walks a MULTI-HOP path toward a claim target
# (`story/default`: `draft -> ready -> in_progress`; `bug/default`:
# `new -> triaged -> fixing`), one PATCH per hop. The backend's dependency
# execution gate (`enforce_can_start`) only fires on the hop that ENTERS a
# gate-active status — the LAST hop — so a dependency-blocked task used to
# sail through every earlier hop and only 409 on the final one, leaving it
# parked at the intermediate status (`ready`/`triaged`) with no reverse
# transition to undo it. Neither built-in template defines one.
#
# `GET /tasks/mine` (#1256, `backend/app/api/routes/tasks.py`) now carries
# the same derived `blocked_state` the gate itself computes, so this script
# can skip a blocked task BEFORE walking it at all, rather than discovering
# the block on the last hop. `_BLOCKED_STATES` mirrors the backend's own
# denial set exactly (`assert_can_start`, `task_blocking.py` —
# `{"waiting", "stuck"}`, the same set `is_dependency_blocked` uses), so the
# listing and the gate can never disagree.
_BLOCKED_STATES = frozenset({"waiting", "stuck"})


def _is_dependency_blocked(task: dict) -> bool:
    """True when `/tasks/mine` reports `task` as dependency-blocked.

    A MISSING `blocked_state` key means NOT blocked, deliberately: an older
    backend that predates #1256 can't report the field at all, and this
    script must keep working against it — falling open to the pre-#1256
    claim-then-409 behaviour for that server, not refusing to poll it.
    """
    return task.get("blocked_state") in _BLOCKED_STATES


def _list_actionable_tasks(
    workspace_id: str | None, statuses: frozenset[str] | None = None
) -> list[dict]:
    """Fetch this agent's tasks and keep only the actionable, unblocked ones.

    `statuses` overrides `_ACTIONABLE_STATUSES` (see `--statuses`) — for a
    workspace on a custom workflow whose "not started" keys this script
    can't know about.
    """
    wanted = statuses if statuses is not None else _ACTIONABLE_STATUSES

    path = "/tasks/mine"
    if workspace_id:
        path += f"?workspace_id={workspace_id}"
    resp = _request("GET", path) or {}
    tasks = resp.get("tasks", [])

    # #1233 part 2: `GET /tasks/mine` caps at 100 (`.to_list(length=100)`,
    # newest-created first) and this filter runs client-side, after that
    # cap. An agent with more than 100 assignments whose newest 100 don't
    # include any actionable one would see zero results — indistinguishable
    # from "no work to do" — with no signal, unless this fires. Checked
    # against the PRE-filter count so it fires regardless of which statuses
    # end up matching. stdout is this script's machine-readable JSON-lines
    # contract, so this goes to stderr only.
    if len(tasks) == _TASKS_MINE_SERVER_CAP:
        sys.stderr.write(
            f"poll_tasks: /tasks/mine returned {_TASKS_MINE_SERVER_CAP} tasks "
            "(its server-side cap) before actionable-status filtering; there "
            "may be more assigned tasks the server never returned.\n"
        )

    # US-031.5: a task this agent holds in an agent-flow phase (`phase_role` set) is its
    # to work whatever the phase is called, so it is kept even when its status is not a
    # "not started" one. (US-031.6: a phase offered to this agent's role is claimed from
    # the inbox wait in `_handle_inbox_event`, and then arrives here.)
    actionable = [t for t in tasks if t.get("status") in wanted or t.get("phase_role")]

    # #1256: drop dependency-blocked tasks here, BEFORE `_work_task` ever
    # walks `_claim_path` — the one place the `/tasks/mine` payload is read,
    # so this sits beside the status filter above and the `--statuses`
    # override path is covered for free. `_work_task`/`_claim_path`
    # themselves are unchanged; see `_claim_path`'s docstring for the
    # narrower race window this doesn't (and can't) close.
    result = []
    for t in actionable:
        if not _is_dependency_blocked(t):
            result.append(t)
            continue
        task_id = t.get("id") or t.get("_id")
        blocker_ids = [b.get("id") for b in t.get("blockers", [])]
        sys.stderr.write(
            f"poll_tasks: skipping task {task_id}: dependency-blocked "
            f"({t.get('blocked_state')}) by {len(blocker_ids)} unresolved "
            f"blocker(s) {blocker_ids} — the backend's execution gate would "
            "409 the claim and an agent can never force past it. "
            f"Left at {t.get('status')!r} (not advanced through intermediate "
            "statuses).\n"
        )

    return result


# ---------------------------------------------------------------------------
# #1233 review follow-up — workflow-aware transitions.
#
# Listing the right tasks was only half the bug. `_work_task` used to claim
# with an unconditional `PATCH {"status": "in_progress"}`, and every write
# goes through `validate_transition` (backend/app/api/routes/workflows.py),
# which enforces whatever workflow the task's own workspace+item_type has —
# built-in template or custom. Probed against the real WORKFLOW_TEMPLATES:
#
#   story/default  draft       -> in_progress : 422 (only `ready` is legal)
#   bug/default    new/triaged -> in_progress : "succeeds" via the QA-122
#                                                unknown-target escape, but
#                                                lands the task OUTSIDE its
#                                                own workflow — orphaned.
#   task/default   in_progress -> done         : 422 (only in_review/blocked)
#
# So the unconditional PATCH either kills the worker (a hard 422 outside
# `main()`'s `except SystemExit` — see `_work_task`'s old comment) or
# actively corrupts the task's workflow state. Both are worse than the
# original "does nothing" bug.
#
# The fix below asks the task's OWN workflow (`GET /workflows/{item_type}`,
# cached per workspace+item_type) what's legal, and walks a shortest legal
# path — one PATCH per hop — from the task's current status toward a
# semantic target (an "active"-category status to claim; a status matching
# the handler's declared outcome to finish). If no legal path exists (a
# required field is unset, or the workflow has no status matching the
# outcome at all), it does NOT force the move — it records what it can as a
# task comment and leaves the status exactly where it was. Invariant: never
# leave a task worse than it was found.
# ---------------------------------------------------------------------------


# `GET /workflows/{item_type}` (`app/api/routes/workflows.py`'s `get_workflow`)
# 404s for TWO different reasons with the SAME status code: "Workspace not
# found" (a scope/visibility problem the caller should hear about loudly)
# and "No custom workflow for {item_type}. Default (any-to-any) applies."
# (a real, safe-to-treat-as-missing case). A bare `treat_as_missing={404}`
# would conflate them — a scope error would silently become "any-to-any
# applies", route to the `["in_progress"]` fallback, and get CACHED for
# `_WORKFLOW_CACHE_TTL` seconds. This substring narrows the signal to the
# one case that's actually safe. Pinned by
# backend/tests/test_openclaw_poll_actionable_statuses.py against
# `get_workflow`'s real source, so a reworded detail string fails there
# instead of quietly reopening the conflation (worst case if it drifts:
# EVERY workflow lookup on a no-template workspace — the common case —
# starts exiting per-task via `_work_task_safely`, which is loud, not
# silent, so this is a fail-safe direction to drift in, not a fail-open one).
_MISSING_WORKFLOW_DETAIL = "No custom workflow"


def _get_workflow(workspace_id: Optional[str], item_type: str) -> Optional[dict]:
    """Fetch (and cache) the custom workflow for workspace+item_type.

    Returns `None` when there is no custom workflow for this workspace —
    see `_MISSING_WORKFLOW_DETAIL` above for why that's narrower than "any
    404". A 404 that ISN'T that (e.g. "Workspace not found") falls through
    to `_request`'s normal error path and exits — caught per-task by
    `_work_task_safely`, so it skips just that task loudly rather than
    silently caching a false "any-to-any" verdict for 5 minutes.

    Cached per (workspace_id, item_type) for `_WORKFLOW_CACHE_TTL` seconds —
    one call per board per cache window, not one per task — matching the
    backend's own Redis TTL for the same lookup
    (`workflows.py`'s `WORKFLOW_CACHE_TTL = 300`).
    """
    if not workspace_id:
        # No workspace on the task (legacy data) — nothing to look up.
        # Falling back to "no custom workflow" preserves the pre-#1233
        # behaviour for exactly the tasks it always worked for.
        return None

    key = (workspace_id, item_type)
    now = time.monotonic()
    cached = _workflow_cache.get(key)
    if cached is not None and now - cached[1] < _WORKFLOW_CACHE_TTL:
        return cached[0]

    workflow = _request(
        "GET",
        f"/workspaces/{workspace_id}/workflows/{item_type}",
        treat_as_missing=frozenset({404}),
        missing_if_detail_contains=_MISSING_WORKFLOW_DETAIL,
    )
    _workflow_cache[key] = (workflow, now)
    return workflow


_workflow_cache: dict[tuple[str, str], tuple[Optional[dict], float]] = {}
_WORKFLOW_CACHE_TTL = 300  # seconds; mirrors workflows.py's WORKFLOW_CACHE_TTL


def _category_of(workflow: dict, status_key: str) -> Optional[str]:
    for s in workflow.get("statuses", []):
        if s.get("key") == status_key:
            return s.get("category")
    return None


def _fields_satisfied(task: dict, required_fields: list[dict]) -> bool:
    """A transition's `required_fields` must already hold on the task.

    Mirrors `validate_transition`'s own check
    (`missing.append(...)` when `not val`) — but this walker only ever
    reads fields the task ALREADY has (from `GET /tasks/mine`'s response,
    which carries the same field names: `assignee_id`, `description`,
    `severity`, ...). It never invents a value to force a move through —
    that would be fabricating data the caller never provided.
    """
    return all(task.get(rf.get("field")) for rf in required_fields)


def _find_transition_path(
    workflow: dict,
    task: dict,
    from_status: str,
    is_target: Callable[[str], bool],
) -> Optional[list[str]]:
    """Shortest legal path of status keys from `from_status` to the nearest
    status satisfying `is_target`, mirroring `validate_transition`'s own
    edge semantics (`backend/app/api/routes/workflows.py`) so every path
    this returns is guaranteed PATCH-able:

    * `from_status` must be a real status in this workflow. An orphaned
      task (QA-122's "unknown-source" case) is out of scope for an
      unattended poller — it returns `None` rather than attempting the
      one-move self-heal a human/dashboard caller gets.
    * An empty overall `transitions` list means any-to-any (kanban
      templates) — a single hop straight to any status satisfying
      `is_target`.
    * A status with NO transition object naming it as `from` is ALSO
      any-to-any FROM THAT STATUS specifically (see workflows.py's "No
      transitions from this status defined — allow"). Matching the backend
      exactly, that wildcard hop is NOT gated by `required_fields` — no
      transition object exists to hold them. This looks like a bug at
      first read; it isn't, it's the real server behaviour, so it's kept
      and named here rather than "fixed" into a stricter rule the backend
      doesn't itself enforce.
    * Every REAL transition's `required_fields` must already be satisfied
      by the task's own current fields (see `_fields_satisfied`).

    Returns the ordered list of status keys to PATCH through in sequence
    (excluding `from_status`), `[]` if `from_status` already satisfies
    `is_target`, or `None` if no legal path exists.
    """
    valid_keys = {s["key"] for s in workflow.get("statuses", [])}
    if from_status not in valid_keys:
        return None
    if is_target(from_status):
        return []

    transitions = workflow.get("transitions", [])
    if not transitions:
        for key in sorted(valid_keys):
            if is_target(key):
                return [key]
        return None

    graph: dict[str, list[dict]] = {}
    for t in transitions:
        graph.setdefault(t["from"], []).append(t)

    seen = {from_status}
    queue: deque[tuple[str, list[str]]] = deque([(from_status, [])])
    while queue:
        node, path = queue.popleft()
        edges = graph.get(node)
        if not edges:
            # Wildcard: no transition objects leave this node, so (per the
            # backend) a direct hop to ANY valid key is allowed, unchecked
            # by required_fields — see the docstring above.
            for key in sorted(valid_keys):
                if key not in seen and is_target(key):
                    return path + [key]
            continue
        for t in edges:
            target = t["to"]
            if target in seen or target not in valid_keys:
                continue
            if not _fields_satisfied(task, t.get("required_fields", [])):
                continue
            new_path = path + [target]
            if is_target(target):
                return new_path
            if _category_of(workflow, target) == "done":
                # A terminal ("done"-category) status is a legal WILDCARD
                # SOURCE (per the backend, a status with no outgoing
                # transitions of its own is any-to-any FROM there — see the
                # wildcard branch above) but must never be treated as a
                # mere WAYPOINT here. Continuing the search past it would
                # let this walker route e.g. a bug/default claim through
                # `wont_fix` (new -> wont_fix has no required_fields, so
                # it's reachable) on the way to `fixing` (wont_fix has no
                # outgoing transitions of its own, so the wildcard rule
                # would allow ANY next hop, unchecked). Each individual hop
                # would be legal per the backend, but the INTENT is wrong,
                # and if the second hop then failed, the task would be
                # left sitting at `wont_fix` — a "new" bug silently turned
                # "won't fix" by a network blip. Only allowed when
                # `is_target` itself wants it (handled above).
                continue
            seen.add(target)
            queue.append((target, new_path))
    return None


# US-006.3's dependency execution gate (`enforce_can_start`,
# backend/app/services/task_blocking.py) is what stops an agent starting a
# dependency-blocked task — and an agent caller can NEVER force past it
# (`_HUMAN_AUTH_TYPES = {"jwt"}`; `force` is JWT-only). Both PATCH call
# sites (`update_task`, the bulk-update per-item loop) reach it ONLY via
# `is_active_transition(old, new)`.
#
# #1246 made that gate itself workflow-aware: `task_blocking.
# gate_active_statuses(workflow_active_statuses)` unions a hardcoded FLOOR
# (`ACTIVE_STATUSES = {"in_progress", "in_review"}`) with the target
# workspace+item_type's own workflow-resolved "gate-active" status keys —
# so a transition into `bug/default`'s `fixing` gates exactly like
# `in_progress` always has. `#1249` separately made `PATCH /tasks/{id}`
# accept those workflow-native keys as legal write targets at all. Together
# they're why this poller no longer needs to refuse every claim on
# `bug/default` (see `_claim_path` below for what refusal case remains).
#
# Everything below duplicates that backend derivation because this script
# can't import the backend package. Two pieces, matching the backend's own
# two functions exactly:
#
#   `_GATE_ACTIVE_STATUSES_FLOOR` mirrors `task_blocking.ACTIVE_STATUSES` —
#   the hardcoded minimum, unioned in regardless of workflow.
#
#   `_gate_active_statuses(workflow)` mirrors `task_blocking.
#   gate_active_statuses()` UNIONED with `workflows.workflow_active_status_
#   keys()`'s own resolution — which is a DENYLIST
#   (`category not in {"todo", "done"}`), not an allowlist of
#   `category == "active"`. That distinction is load-bearing: the backend
#   fails CLOSED toward gating for an unrecognized, missing, or `None`
#   category, and this must match it byte for byte or the poller could
#   trust a status the backend doesn't actually gate — the exact bypass
#   #1233's review caught, reopened under a new name.
#
# Pinned by backend/tests/test_openclaw_poll_gate_active_statuses.py against
# the real `gate_active_statuses` AND the real category-denylist constant,
# both as values AND as the claim-safety behavior they drive — see
# `_claim_path` below for how it's used.
_GATE_ACTIVE_STATUSES_FLOOR = frozenset({"in_progress", "in_review"})

# Mirrors app.api.routes.workflows._KNOWN_INACTIVE_CATEGORIES exactly — the
# only two categories `workflow_active_status_keys` treats as NOT
# gate-active. Everything else (an explicit "active", an unrecognized
# value, a missing key, `None`) counts as gate-active, fail-closed.
_GATE_INACTIVE_CATEGORIES = frozenset({"todo", "done"})


def _gate_active_statuses(workflow: Optional[dict]) -> frozenset[str]:
    """The status keys whose ENTRY the backend's dependency execution gate
    actually triggers on, for THIS workflow — mirrors
    `task_blocking.gate_active_statuses()` exactly:

        gate_active_statuses(workflow_active_statuses)
            == ACTIVE_STATUSES | (workflow_active_statuses - {"blocked"})

    where `workflow_active_statuses` is itself
    `workflows.workflow_active_status_keys()`'s result (the category
    DENYLIST described above `_GATE_INACTIVE_CATEGORIES`).

    `workflow is None` (no custom workflow for this workspace+item_type)
    mirrors `workflow_active_status_keys` returning the empty set for that
    case — the floor alone applies, unchanged from pre-#1246 behavior.

    `"blocked"` is excluded BY NAME, unconditionally — even from a workflow
    that tags a status literally named `"blocked"` with a category that
    would otherwise make it gate-active (every built-in template with a
    `blocked` status does exactly this). A CUSTOM-named parking status
    (e.g. `"parked"`) tagged the same way is NOT excluded — the backend's
    carve-out is a literal-string exception, not a semantic "any parking
    status" rule, and this has to match that precisely rather than
    generalize it. See `task_blocking.gate_active_statuses`'s own
    docstring for why the carve-out exists at all: gating entry into
    "blocked" would 409 the one caller class (agents) that most needs to
    record a dependency-blocked state, and agents can never `force` past a
    409 either.

    This is a SEPARATE question from `_legal_active_path`'s notion of "the
    workflow's active status" (an ALLOWLIST — `category == "active"` only,
    unchanged by this fix). That function asks "what does this workflow
    call its working state", and tolerates no ambiguity — an odd category
    there just means no legal claim target is found, refusing safely by
    construction. This function asks "what will the backend's gate
    actually fire on", and must match the backend's real, wider denylist
    precisely — an ambiguous category here is NOT a refusal signal on its
    own, it's a gate-active signal, exactly like the backend. `_claim_path`
    combines both: a target only counts as a safe claim if it satisfies
    BOTH the legal allowlist AND this gate denylist.
    """
    if workflow is None:
        return _GATE_ACTIVE_STATUSES_FLOOR
    extra = {
        s.get("key")
        for s in workflow.get("statuses", [])
        if s.get("key") and s.get("category") not in _GATE_INACTIVE_CATEGORIES
    }
    return frozenset(_GATE_ACTIVE_STATUSES_FLOOR | (extra - {"blocked"}))


def _legal_active_path(workflow: Optional[dict], task: dict) -> Optional[list[str]]:
    """The workflow-LEGAL hops toward an "active"-category status.

    Legality only — no opinion on whether landing there is SAFE under the
    backend's dependency execution gate. `_claim_path` below wraps this
    with that opinion; use `_claim_path` for anything that's actually going
    to PATCH. This is exposed separately so the two questions ("is there a
    legal transition" vs "would using it bypass the gate") can be answered,
    and reported on, independently — see `_claim_path`'s docstring.

    `workflow is None` (no custom workflow for this workspace+item_type)
    keeps the pre-#1233 default of a single PATCH to `in_progress` — the
    backend's own fallback for that case is any-to-any with VALID_STATUSES
    governing, which `in_progress` always satisfies.
    """
    if workflow is None:
        return ["in_progress"]
    current = task.get("status")
    return _find_transition_path(
        workflow, task, current, lambda k: _category_of(workflow, k) == "active"
    )


def _claim_path(workflow: Optional[dict], task: dict) -> Optional[list[str]]:
    """The hops to move `task` toward an "active"-category status — but
    ONLY when landing there is also safe under the backend's dependency
    execution gate (`_gate_active_statuses` above).

    #1233 review (original): a workflow-legal claim path was not
    automatically a SAFE one. `is_active_transition` — the only thing that
    triggers `enforce_can_start` — used to fire solely on a transition
    into the hardcoded `in_progress`/`in_review`, with no idea a
    workflow's own "active"-category status might be named something
    else. `bug/default`'s only such status, `fixing`, wasn't in that
    hardcoded set, so claiming through it would have silently started work
    on a possibly dependency-blocked bug with NO gate check ever running —
    worse than the original #1233 bug (zero work claimed): unattended,
    unauthorized work. This function used to refuse EVERY claim on
    `bug/default` outright rather than risk that.

    #1246 (backend) then made the gate itself workflow-aware
    (`task_blocking.gate_active_statuses`, mirrored by
    `_gate_active_statuses` above), and #1249 made `PATCH /tasks/{id}`
    accept a workflow's own status keys as legal write targets at all.
    Both reasons for the blanket `bug/default` refusal are gone: `fixing`
    now genuinely triggers the backend's gate — a dependency-blocked bug
    409s exactly like `open -> in_progress` always has — so `bug/default`
    claims through `new -> triaged -> fixing` like any other built-in
    template's claim path now.

    KNOWN CONSEQUENCE of the multi-hop walk, now AVOIDED before the walk
    starts (#1256): `_walk_transitions` PATCHes each hop in `path` in turn,
    and if the FINAL hop 409s (`task_blocked`) or otherwise fails, the task
    is left sitting at the intermediate status (`triaged` for
    `bug/default`, `ready` for `story/default`) rather than rolled back —
    there is no `triaged -> new` / `ready -> draft` transition to undo it
    with. `_list_actionable_tasks` now drops any task `/tasks/mine` reports
    as dependency-blocked (`blocked_state` in `{"waiting", "stuck"}`)
    BEFORE it ever reaches this walk, so a task the backend would gate is
    never even claimed. That narrows the hazard to two cases it can't
    close: a list-then-claim race (a blocker appears between the listing
    and the final hop) and a server too old to send `blocked_state` at all
    (`_is_dependency_blocked` fails open for that case, on purpose). In
    both, the final hop still 409s and the task still parks at the
    intermediate status exactly as before this fix — that fallback is
    retained deliberately, not regressed; `_work_task_safely` still
    isolates it to one task per cycle, and the intermediate status is still
    `"todo"`-category, so the task stays listed and gets retried (self-
    healing, not silently stranded). This has been `story/default`'s
    fallback behavior since #1245; `bug/default` shares it rather than
    being treated as a special case.

    The refusal branch below is NOT dead code post-#1246/#1249 — it still
    fires for a workflow (built-in or custom) with no workflow-legal path
    at all, or whose only reachable active-category status is the one
    NAME-based carve-out `_gate_active_statuses` never treats as
    gate-safe (a status literally called `blocked`). Refusing there is
    still correct: claiming would either be illegal, or would enter
    `blocked` believing the gate ran on it when it provably does not.
    """
    path = _legal_active_path(workflow, task)
    if path is None or path[-1] not in _gate_active_statuses(workflow):
        return None
    return path


def _outcome_path(
    workflow: Optional[dict], current_status: str, desired_status: str, task: dict
) -> Optional[list[str]]:
    """The hops needed to move `task` to a legal representation of the
    handler's declared outcome (`desired_status`, either a floor name from
    `_SUMMARY_REQUIRED_STATUSES` or a workflow-native status key a handler
    returned directly — see `_outcome_path` callers' exact-key branch).

    `workflow is None` keeps the pre-#1233 default: a single direct PATCH,
    since any-to-any + VALID_STATUSES governs and always accepted these.

    `desired_status == "done"` maps to the workflow's literal `"done"` key
    when it has one; otherwise to its single `done_statuses` entry. A
    workflow with MULTIPLE done statuses and no literal `"done"` key (e.g.
    bug/default: `closed` vs `wont_fix`) is deliberately left unresolved —
    which of those a generic `{"status": "done"}` handler result means is a
    human distinction (fixed vs won't-fix), not the poller's to guess.
    `blocked`/`canceled`/`in_review` are exact-key matches only: they are
    specific semantic states, not "any non-todo status", so a workflow
    without that literal key has no legal target for it.
    """
    if workflow is None:
        return [desired_status]

    valid_keys = {s["key"] for s in workflow.get("statuses", [])}
    if desired_status == "done":
        if "done" in valid_keys:
            target_keys = {"done"}
        else:
            done_statuses = workflow.get("done_statuses", [])
            if len(done_statuses) != 1:
                return None
            target_keys = set(done_statuses)
    else:
        if desired_status not in valid_keys:
            return None
        target_keys = {desired_status}

    return _find_transition_path(
        workflow, task, current_status, lambda k: k in target_keys
    )


# ---------------------------------------------------------------------------
# #1247 — hand off, don't strand, when `_outcome_path` can't resolve the
# handler's declared outcome to any legal status.
#
# Two live instances, both proven against the real WORKFLOW_TEMPLATES:
# `bug/default` has no literal `done` key and an ambiguous `done_statuses`
# (`closed` vs `wont_fix` — see `_outcome_path`'s docstring for why guessing
# between them is a product call, not this poller's), so an ordinarily
# SUCCEEDING handler that returns nothing (`_work_task`'s default outcome,
# `{"status": "done"}`) hits this. `story/default` has no `blocked` status at
# all, so a FAILING handler (`_work_task`'s exception-mapped outcome) hits it
# too. Both used to leave the task exactly where the claim step put it
# (`fixing` / `in_progress`) forever — outside `_ACTIONABLE_STATUSES`, so
# never re-listed, with only a stderr warning as the record.
#
# The decision (not this poller's to re-litigate): auto-walking to a
# terminal status is explicitly REJECTED. On `bug/default` the only terminal
# reachable from `fixing` is through `in_review`, so auto-closing would mean
# an agent closing its own bug with no human review, and choosing between
# `closed`/`wont_fix` risks marking a FIXED bug won't-fix. Instead: hand the
# task off to a human by moving it to the workflow's own review/handoff
# status — the last "active"-category stop before a "done"-category one.
#
# WHY this is a resolution and not just a different stuck state, and why
# nobody should "fix" this later by matching on the literal name
# `in_review`: `fixing` (or `in_progress`) asserts an AGENT IS ACTIVELY
# WORKING the task — false the instant the handler has returned, success or
# failure. The handoff status asserts A HUMAN SHOULD LOOK AT THIS — true
# either way. Landing there converts a lie (still "fixing") into a correct
# handoff (needs review); it is not interchangeable with any other
# "active"-category status, including the one the task is already sitting
# on.
# ---------------------------------------------------------------------------


def _handoff_candidates(workflow: dict) -> frozenset[str]:
    """The workflow's own review/handoff statuses — derived from its
    STRUCTURE, never matched by name. Hardcoding a status name (`in_review`)
    is the exact pattern this repo has hit five times now (#1225, #1233,
    #1246, #1249, #1255; #1265 tracks the same class in MCP handlers).

    The principled definition: an "active"-category status with a legal
    one-hop transition — real or wildcard — into a "done"-category status.
    That is "the last stop before terminal, where a human takes over". This
    is a STRUCTURAL property, similar in spirit to `_find_transition_path`'s
    own edge semantics but not identical to them: unlike a real BFS hop,
    the qualifying edge here is never gated by `_fields_satisfied` — a
    candidate's status is a fact about the workflow's shape alone, not
    about whether THIS task's fields happen to satisfy that specific
    transition today. (Reachability from a given task's current status
    — which DOES respect `required_fields` — is `_handoff_path`'s job,
    below, via `_find_transition_path`.)

    Verified against all six built-in templates
    (`TestHandoffCandidateDerivation` in
    backend/tests/test_openclaw_poll_actionable_statuses.py):

      task/default, story/default, bug/default -> {"in_review"}: each has
      exactly one "active" status (`in_review`) with a direct edge into a
      "done" status (`done` / `done` / `closed`). `fixing` (bug/default) and
      `blocked` (task/default) do NOT qualify — their only edges land on
      OTHER "active" statuses (`in_review`, `in_progress`), never directly
      on a "done" one.

      task/kanban, story/kanban, bug/kanban -> {"in_progress"} (the "any hop
      is legal" wildcard rule below, since `transitions` is empty and there
      is only one "active" status). A generic `{"status": "done"}` outcome
      never reaches this at all — each kanban template has a LITERAL
      `"done"` key, so `_outcome_path` resolves it directly. But
      `blocked`/`canceled`/`in_review` outcomes DO reach `_handoff_path` on
      these templates (none of those three is a literal kanban status key
      either) — it just returns `[]`, since `in_progress` (the only
      candidate) is also where a claimed task already sits, so there is no
      hop to take. That falls through to the pre-#1247 comment-only
      fallback, same as `dev` — see
      `test_kanban_failure_outcome_falls_through_to_comment_only_unchanged`.

    Ties (more than one qualifying status) are broken by `_find_transition_path`'s
    own BFS — nearest first, real edges explored in each status's own
    declared transition order (not sorted; only the two wildcard branches
    fall back to `sorted(valid_keys)`) — the same tie-break every other
    multi-target search in this file already uses. No built-in template has
    more than one candidate.
    """
    statuses = workflow.get("statuses", [])
    active_keys = {s["key"] for s in statuses if s.get("category") == "active"}
    done_keys = {s["key"] for s in statuses if s.get("category") == "done"}
    if not active_keys or not done_keys:
        return frozenset()

    transitions = workflow.get("transitions", [])
    if not transitions:
        # Workflow-wide any-to-any (kanban templates, see `_find_transition_
        # path`'s own docstring): every "active" status is trivially one
        # legal hop from every "done" status.
        return frozenset(active_keys)

    graph: dict[str, list[dict]] = {}
    for t in transitions:
        graph.setdefault(t["from"], []).append(t)

    candidates = set()
    for key in active_keys:
        edges = graph.get(key)
        if not edges:
            # Node-level wildcard: no transition object lists this status as
            # `from`, so — per the backend, matching `_find_transition_path`'s
            # own wildcard branch — a direct hop to ANY valid status,
            # including a "done" one, is legal from here.
            candidates.add(key)
            continue
        if any(t["to"] in done_keys for t in edges):
            candidates.add(key)
    return frozenset(candidates)


def _handoff_path(
    workflow: dict, task: dict, current_status: str
) -> Optional[list[str]]:
    """The hops (if any) from `current_status` to the workflow's own
    handoff status (`_handoff_candidates`) — the fallback `_work_task` uses
    when `_outcome_path` can't resolve the handler's declared outcome at
    all.

    Reuses `_find_transition_path`, so every property that primitive already
    guarantees carries over unchanged: `required_fields` are checked against
    the task's own values (never fabricated), and a "done"-category status
    is never treated as a mere waypoint en route to something else (the
    guard #1233's review added, against exploiting the backend's "no
    outgoing transitions = wildcard" rule).

    #1247 review, blocker 2 — an earlier version of this docstring claimed
    that guard was structurally unreachable FOR THIS SEARCH specifically,
    reasoning that any status with a real edge into a "done" status is
    already a `_handoff_candidates` member itself. That reasoning only
    covers "active"-category sources — `_handoff_candidates` requires
    `category == "active"` AND a done edge, so a "todo"-category (or
    unrecognized-category) status with a real edge into a "done" status is
    NOT a candidate, and the guard genuinely fires reaching past it.
    Measured: workflow `working(active) -> parked(todo) -> dropped(done, no
    outgoing edges) ; review(active) -> shipped(done)`, candidates =
    `{"review"}`. WITH the guard, `_handoff_path(..., "working")` is
    `None` — `parked` is enqueued (todo-category, not a done waypoint
    itself), but `dropped` (done-category) is refused as a pass-through, so
    `review` is unreachable. WITHOUT it, BFS would treat `dropped`'s
    wildcard (no outgoing transitions of its own) as license to hop
    onward, returning `["parked", "dropped", "review"]` — marking the task
    `dropped` en route to a review status, exactly the #1233-review hazard
    this guard exists to prevent. `bug/default` has this same shape for
    real: `new`/`triaged` (todo) both have a real edge straight into
    `wont_fix` (done, no outgoing edges of its own) — it just never bites
    in practice because `wont_fix` isn't reachable from `fixing`/`in_review`
    (the only statuses `_handoff_path` is ever actually called from), not
    because the shape is impossible. See
    `TestFindTransitionPathNeverRoutesThroughADoneWaypoint` for the
    guard pinned at the shared-primitive level, and
    `TestHandoffPathNeverRoutesThroughADoneWaypoint` for this caller's own
    fixture, proving the guard is load-bearing here too.

    Returns `None` when this workflow has no qualifying status at all (the
    caller must fall back further, to the pre-#1247 comment-only behaviour —
    "if none qualifies, keep today's behaviour"), `[]` when `current_status`
    already IS a qualifying status (nothing to move, so there's no hop to
    carry a summary on — also handled by the comment-only path), or the
    ordered hops otherwise.
    """
    candidates = _handoff_candidates(workflow)
    if not candidates:
        return None
    return _find_transition_path(
        workflow, task, current_status, lambda k: k in candidates
    )


def _handoff_summary(outcome: dict, handoff_target: str) -> str:
    """Build the summary for a handoff hop.

    Two obligations. First, it must never be empty — `_normalize_outcome`
    keys its backfill on the HANDLER'S OWN DECLARED status
    (`out.get("status")`, checked against the resolved required-status set
    from `_summary_required_statuses`: the `_SUMMARY_REQUIRED_STATUSES`
    floor UNIONED with this task's own workflow's terminal-category
    statuses), not on the handoff target this function is building a
    summary FOR. This fallback is reached precisely because `_outcome_path`
    couldn't resolve that declared status to any legal target at all — that
    can happen for a declared status that falls OUTSIDE the resolved set
    (neither one of the floor's four literal names NOR one of this
    workflow's own terminal-category keys — e.g. a handler returning
    `{"status": "shipped"}` on a workflow with no `shipped` key at all, or a
    recognized-but-unreachable non-terminal key), in which case
    `_normalize_outcome` never backfilled anything and `outcome.get(
    "summary")` really can be falsy here. The converse also reaches this
    function WITHOUT that emptiness problem: when the declared status IS
    one of the floor's own names that this particular workflow simply lacks
    a legal target for — `bug/default` declaring `done` (its only
    `done`-category keys are `closed`/`wont_fix`, ambiguous per
    `_outcome_path`'s own docstring) and `story/default` declaring `blocked`
    (it has no `blocked` status at all) are the two live handoff triggers
    the block comment above `_handoff_candidates` names — `_normalize_
    outcome` DOES backfill (`"done"`/`"blocked"` are floor members, in the
    resolved set regardless of this workflow's own keys), so
    `outcome.get("summary")` is already populated by the time it gets here;
    `_outcome_path` still returns `None` (no legal TARGET exists, which is a
    separate question from whether a summary exists) and this function
    still runs, but not because the summary was ever missing. Second — the
    point of this function, not just a formality — it must make clear
    whether the handler SUCCEEDED or FAILED, since the handoff status
    itself (an "active"-category status, same as `claimed_status` was)
    carries no such signal on its own the way a real `done` vs `blocked`
    would have.
    """
    unresolved_status = outcome.get("status")
    body = (outcome.get("summary") or "").strip()
    if not body:
        body = f"Recorded by openclaw poll_tasks: {unresolved_status}."
    text = (
        f"[Outcome: {unresolved_status}] Handed off to '{handoff_target}' for "
        f"human review — the workflow has no status that legally represents "
        f"'{unresolved_status}'. {body}"
    )
    return text[:_MAX_TASK_SUMMARY_LEN]


# #1130: an agent (X-API-Key) caller MUST send a non-empty `summary` for any of
# these statuses, or the PATCH 422s. Duplicated from the backend's
# AGENT_SUMMARY_REQUIRED_STATUSES (app/api/routes/tasks.py) because this script
# is stdlib-only and outside the backend package — the copy is pinned by
# backend/tests/test_openclaw_summary_contract.py so it can't drift silently.
#
# #1271: this is a FLOOR, not the whole story. The backend's real check
# (`_resolve_summary_required_statuses`, `app/api/routes/tasks.py`) unions
# this floor with the workflow's OWN terminal-category statuses
# (`workflows.workflow_summary_required_status_keys`) — so a custom workflow
# whose terminal status is spelled e.g. `"shipped"`, never one of these four
# literal names, is STILL summary-required by the backend, and a PATCH into
# it with no summary still 422s. `_summary_required_statuses` below resolves
# the full set for a given task's workflow; this constant remains the
# hardcoded minimum that always applies, workflow or not.
_SUMMARY_REQUIRED_STATUSES = {"done", "blocked", "canceled", "in_review"}

# Mirrors `app.api.routes.workflows._KNOWN_NON_TERMINAL_CATEGORIES` exactly
# — deliberately a SEPARATE constant from `_GATE_INACTIVE_CATEGORIES` above,
# not a reuse: the two backend predicates disagree on purpose, and that
# disagreement is load-bearing, not an oversight. `_GATE_INACTIVE_CATEGORIES`
# ({"todo", "done"}) answers "does entering this status trigger the
# dependency execution gate" — an "active" status always does, a "done" one
# never does. This constant answers a different question — "does LEAVING a
# task at this status require an agent to explain itself" — where a "done"
# status always does (that's the whole point of a terminal state) and an
# "active" one never does (the task is still being worked). "active" sits on
# OPPOSITE sides of the two questions: gate-active (yes) but not
# summary-required (no). Matching `_KNOWN_NON_TERMINAL_CATEGORIES` byte for
# byte means an unrecognized/missing/`None` category is treated as
# summary-required here (denylist, fail CLOSED — #1255's own design), never
# silently exempted the way an allowlist (`category == "done"`) would.
_NON_TERMINAL_CATEGORIES = frozenset({"todo", "active"})


def _summary_required_statuses(workflow: Optional[dict]) -> frozenset[str]:
    """The full set of status keys a PATCH into requires a non-empty
    `summary` for, given THIS task's own (workspace, item_type) workflow —
    mirrors `app.api.routes.tasks._resolve_summary_required_statuses`
    exactly: the `_SUMMARY_REQUIRED_STATUSES` floor UNIONED with this
    workflow's own terminal-category (non-`_NON_TERMINAL_CATEGORIES`) status
    keys.

    Deliberately a UNION, NEVER a replacement of the floor — a workflow that
    mislabels one of the floor's own literal names (e.g. tags `done`
    `category: "active"`) does not exempt an agent from explaining a `done`
    hop; the backend enforces the floor regardless of what any workflow's
    own categories say about those four specific names, and this must match
    that.

    `workflow is None` (no custom workflow for this workspace+item_type)
    resolves to the floor alone — mirrors
    `workflow_summary_required_status_keys`'s empty-set fallback for the
    same case, same as `_gate_active_statuses(None)` mirrors its own
    backend counterpart above.
    """
    floor = frozenset(_SUMMARY_REQUIRED_STATUSES)
    if workflow is None:
        return floor
    extra = {
        s.get("key")
        for s in workflow.get("statuses", [])
        if s.get("key") and s.get("category") not in _NON_TERMINAL_CATEGORIES
    }
    return floor | extra


# A claim hop only needs a summary in the unusual case where the nearest
# "active" status for a CUSTOM workflow happens to also be a
# summary-required one (e.g. a workflow with no "in_progress"-equivalent,
# where "in_review" is the only active-category key) — none of the six
# built-in templates hit this. Generic, harmless text for that edge case.
_CLAIM_SUMMARY = "Claimed by openclaw poll_tasks."


def _walk_transitions(
    task_id: str,
    path: list[str],
    summary: str,
    *,
    force_summary_on_final: bool = False,
    required_statuses: frozenset[str] = frozenset(_SUMMARY_REQUIRED_STATUSES),
) -> None:
    """PATCH through `path` one hop at a time.

    A hop landing on a status in `required_statuses` carries `summary` —
    this mirrors the backend's OWN check exactly (same resolved set: see
    `_summary_required_statuses`, the `_SUMMARY_REQUIRED_STATUSES` floor
    UNIONED with this task's own workflow's terminal-category statuses), so
    it fires precisely when the server would otherwise 422 the hop for
    missing one, whether that hop is an intermediate step (e.g. `in_review`
    on the way to `done`) or the final one. Every caller in `_work_task`
    resolves this ONCE per task (workflow already fetched) and threads it
    through; the default here is the floor alone, so existing callers that
    never pass it (this module's own tests included) keep exactly
    pre-#1271 behaviour.

    `force_summary_on_final` (#1247 review, blocker 1): attach `summary` to
    the LAST hop regardless of whether its target is in `required_statuses`.
    `_handoff_candidates` (the ONLY caller that passes this) derives its
    target from the workflow's STRUCTURE, so it can legally land on an
    "active"-category status. `required_statuses` is NOT guaranteed to
    exclude that target — a built-in template's handoff candidate is
    literally `in_review`, an "active"-category status that is ALSO one of
    the floor's own literal names, so it IS in `required_statuses` via the
    floor half of the union (only the workflow-derived EXTENSION half is
    guaranteed active-category-free, per `_NON_TERMINAL_CATEGORIES`'s
    denylist). What IS guaranteed to fall outside `required_statuses`, and
    is the actual case this parameter exists for, is a HAND-AUTHORED
    active-category handoff status that doesn't happen to spell one of the
    floor's four names either (a `review`/`qa`/`verifying` status this
    script has no other way to know needs a summary). Without
    `force_summary_on_final`, that specific hop would carry no summary at
    all and the handler's outcome would be recorded NOWHERE — worse than
    the pre-#1247 comment-only fallback it replaced, which always recorded
    it as a comment regardless of the task's own vocabulary. For a workflow
    whose handoff target happens to already be in `required_statuses` (e.g.
    `in_review`), this parameter is redundant but harmless — the same hop
    already gets a summary from `required_statuses` alone.

    Scoped to ONLY the final hop, and ONLY via this explicit opt-in,
    because every OTHER caller must keep today's behaviour of only
    consulting `required_statuses`: the claim hop (`_CLAIM_SUMMARY`) would
    overwrite a task's real summary with placeholder claim text on every
    single claim if this defaulted to on, and the resolvable-outcome call
    (which passes `outcome.get("summary", "")`) would PATCH
    `{"summary": ""}` and blank out an already-correct empty value.
    """
    last_index = len(path) - 1
    for index, target in enumerate(path):
        body: dict[str, Any] = {"status": target}
        if target in required_statuses or (
            force_summary_on_final and index == last_index
        ):
            body["summary"] = summary
        _request("PATCH", f"/tasks/{task_id}", body)


def _normalize_outcome(
    result: dict,
    task_id: str,
    *,
    required_statuses: frozenset[str] = frozenset(_SUMMARY_REQUIRED_STATUSES),
) -> dict:
    """Make a handler's result safe to PATCH.

    Two jobs, and the second is the one that isn't obvious.

    **Backfill `summary`.** Handlers are user code written against this skill's
    documented contract, and that contract used to be ``{"status": "done"}`` —
    no summary. Such handlers exist in the wild, and a 422 here is not
    survivable: ``_request`` exits the process, and while `_work_task_safely`
    now isolates that per task (see below) rather than killing the whole
    worker, a 422 mid-outcome would still abandon THIS task's PATCH — better
    to never trigger it. Fixing only the literals this script builds itself
    would leave every pre-existing handler broken, so the guard sits on the
    shared path. `required_statuses` (#1271) is the RESOLVED set for this
    task's own workflow — the `_SUMMARY_REQUIRED_STATUSES` floor UNIONED
    with this workflow's own terminal-category statuses (see
    `_summary_required_statuses`) — never the floor alone by itself, so a
    handler returning a workflow-native terminal status (e.g.
    ``{"status": "shipped"}`` on a workflow with no literal `done`/`blocked`/
    `canceled`/`in_review` key) still gets a summary backfilled here, not
    just at the `_walk_transitions` PATCH layer. Both layers must consume
    the same resolved set, or a bare `{"status": "shipped"}` handler result
    would reach `_walk_transitions` with `summary=""` and still 422.

    **Drop `description`.** The old contract also documented
    ``{"status": "blocked", "description": ...}``. `description` IS a legal
    ``UpdateTaskRequest`` field, so once `summary` is backfilled that PATCH
    stops 422ing and starts *succeeding* — and the backend ``$set``s
    description wholesale, silently replacing the task's real description with
    a handler's error blurb. That trades a loud crash for permanent data loss,
    which is strictly worse. So a legacy `description` is folded into `summary`
    (it is a far better record than the generic fallback) and then stripped.
    Stripping is unconditional: ``{"status": "in_progress", "description": ...}``
    overwrites just as destructively. The contract never offered
    description-editing; a polling loop must not rewrite task descriptions.
    """
    out = {k: v for k, v in result.items() if k != "description"}
    if out.get("status") not in required_statuses:
        return out

    summary = (out.get("summary") or "").strip()
    if not summary:
        # Prefer the handler's own words over a generic line.
        summary = (result.get("description") or "").strip()
    if not summary:
        summary = f"Recorded by openclaw poll_tasks: {out['status']}."
        # Never silent: this fires only for a handler that supplied nothing at
        # all, whose author otherwise has no way to learn they're
        # non-compliant — the same "looks like it works but doesn't" trap this
        # change fixes elsewhere. stdout stays clean for machine consumers.
        sys.stderr.write(
            f"poll_tasks: handler returned no summary for task {task_id}; "
            f"recorded a generic one. Return "
            f'{{"status": ..., "summary": ...}} to record something useful.\n'
        )
    out["summary"] = summary[:_MAX_TASK_SUMMARY_LEN]
    return out


_NOT_REPORTED = "Not reported by the handler."


def _next_phase(task_id: str, current_status: Optional[str]) -> tuple[str, str]:
    """`(status, role)` of the phase after `current_status` in the task's agent flow, read
    from `GET /tasks/{id}` `agent_flow.phases`; `("done", "")` when it is the last phase
    or the flow cannot be read."""
    flow = (_request("GET", f"/tasks/{task_id}") or {}).get("agent_flow") or {}
    phases = flow.get("phases") or []
    keys = [p.get("status_key") for p in phases]
    if current_status in keys and keys.index(current_status) + 1 < len(phases):
        following = phases[keys.index(current_status) + 1]
        return following["status_key"], following.get("role", "")
    return "done", ""


def _work_phased_task(task: dict, handler: Callable[[dict], dict]) -> None:
    """US-031.5 (epic 031 D5): work a task this agent HOLDS in an agent-flow phase.

    It is already ours, and `start` or a claim walk would move it out of its phase (which
    needs a handoff), so no claim path runs. The handler runs, then ONE PATCH changes the
    status and carries the handoff the server requires on a phase change:

    * target status: the handler's `status`, else the next phase of the flow, else `done`;
    * handoff: the handler's `handoff` (a dict of the five fields), a field it left out
      or blank filled with "Not reported by the handler."; `done` falls back to the
      handler's `summary`, and `for_next` to the role of the phase it hands to;
    * a handler that raises moves the task to `blocked` with the error in `not_done`.
    """
    task_id = task.get("id") or task.get("_id")
    failure = None
    try:
        result = handler(task) or {}
    except Exception as e:  # noqa: BLE001
        failure = f"Handler raised {type(e).__name__}: {e}"
        result = {"status": "blocked"}

    target = result.get("status")
    next_role = ""
    if not target:
        target, next_role = _next_phase(task_id, task.get("status"))
    fallback = {field: _NOT_REPORTED for field, _flag in _HANDOFF_FIELDS}
    fallback["done"] = (result.get("summary") or "Completed by openclaw handler.").strip()
    fallback["for_next"] = next_role or (
        "A person should look at this task." if target == "blocked" else _NOT_REPORTED
    )
    if failure:
        fallback["not_done"] = failure
    sent = result.get("handoff") or {}
    handoff = {
        field: str(sent.get(field) or "").strip() or fallback[field]
        for field, _flag in _HANDOFF_FIELDS
    }
    handoff = {k: v[:_MAX_TASK_SUMMARY_LEN] for k, v in handoff.items()}

    _request("PATCH", f"/tasks/{task_id}", {"status": target, "handoff": handoff})
    sys.stdout.write(json.dumps({"task_id": task_id, "result": target}) + "\n")
    sys.stdout.flush()


def _work_task(task: dict, handler: Callable[[dict], dict]) -> None:
    task_id = task.get("id") or task.get("_id")
    if not task_id:
        return
    if task.get("phase_role"):
        return _work_phased_task(task, handler)

    workflow = _get_workflow(task.get("workspace_id"), task.get("type", "task"))
    # #1271: resolve ONCE per task (the workflow document is already in
    # hand) and thread through every summary-gating call below — both
    # `_normalize_outcome` (backfilling on the handler's returned dict) and
    # every `_walk_transitions` call (gating on the PATCH body itself).
    # Both layers must consume the SAME resolved set, or a handler that
    # returns a bare workflow-native status (e.g. `{"status": "shipped"}`,
    # no `summary` key at all) would get no backfill from the first layer
    # and then fail the second layer's PATCH with `summary=""` — a 422
    # either way. See `_summary_required_statuses`.
    summary_required_statuses = _summary_required_statuses(workflow)

    # 1. Claim the task — via whatever transition ITS workflow allows, not a
    # hardcoded `in_progress`, and only when landing there is also SAFE
    # under the backend's dependency execution gate (see `_claim_path`'s
    # docstring). The gate is workflow-aware since #1246/#1249, so this is
    # no longer a blanket "this template never claims" case — `bug/default`
    # claims fine now. What remains is a narrower safety net: a workflow
    # with no legal path at all, or whose only reachable active status is
    # the one name-based carve-out (`blocked`) the gate never fires on.
    # Don't force it in either case: skip this task, warn with the specific
    # reason, and leave it untouched for a human (or a future cycle) rather
    # than killing the worker, corrupting workflow state via the
    # unknown-target escape, or starting unauthorized work with the gate
    # skipped.
    claim_path = _claim_path(workflow, task)
    if claim_path is None:
        legal_path = _legal_active_path(workflow, task)
        if legal_path is not None:
            # This branch is reachable in exactly ONE case, not two.
            # `_legal_active_path`'s target predicate (category == "active",
            # an ALLOWLIST — call it A) is always a SUBSET of
            # `_gate_active_statuses`'s predicate (category NOT IN
            # {"todo", "done"}, a DENYLIST — call it D): every
            # "active"-category key trivially satisfies "not todo, not
            # done". So `legal_path[-1]` (which by construction satisfies A)
            # is always in D, and therefore always in
            # `_GATE_ACTIVE_STATUSES_FLOOR | (D - {"blocked"})` UNLESS it IS
            # the literal string `"blocked"` — the one name-based carve-out.
            # `legal_path` is also the NEAREST such status (BFS shortest
            # path), not "the only one" — a workflow could have a farther,
            # gate-checked target too, but that's irrelevant here: this
            # function only ever proposes the nearest one, so if the
            # nearest one is `blocked`, that's the only diagnosis to make.
            sys.stderr.write(
                f"poll_tasks: skipping task {task_id}: the nearest "
                f"workflow-active status reachable from "
                f"{task.get('status')!r} is {legal_path[-1]!r}, which the "
                "backend's dependency execution gate deliberately excludes "
                "from its gate-triggering set (entering it PARKS the task, "
                "it doesn't START one). Claiming into it would misrepresent "
                "parked work as started, with no dependency check ever run. "
                "Left unchanged.\n"
            )
        else:
            sys.stderr.write(
                f"poll_tasks: skipping task {task_id}: no legal transition "
                f"from {task.get('status')!r} to an active status in its "
                "workflow — a transition's required field may be unset on "
                "this task. Left unchanged.\n"
            )
        return
    # #1271: pass the resolved set here too, not just on the outcome/handoff
    # hops below. `_find_transition_path`'s wildcard rule (a "done"-category
    # status is refused as a WAYPOINT, but is still a legal wildcard SOURCE —
    # see its own docstring) means a claim path CAN legally traverse a
    # status this resolved set requires a summary for even though claiming
    # is not, semantically, "finishing" anything. Known tradeoff, not a bug:
    # `_CLAIM_SUMMARY` placeholder text may overwrite a real summary on such
    # a hop — already the accepted behavior today for the floor name
    # `in_review` (a workflow can legally route a claim through it), and
    # strictly better than the alternative of NOT gating here, which would
    # 422 the hop and strand the task before the handler ever runs.
    _walk_transitions(
        task_id, claim_path, _CLAIM_SUMMARY, required_statuses=summary_required_statuses
    )
    claimed_status = claim_path[-1] if claim_path else task.get("status")

    # 2. Run the handler
    try:
        result = handler(task) or {
            "status": "done",
            "summary": "Completed by openclaw handler.",
        }
    except Exception as e:  # noqa: BLE001
        result = {
            "status": "blocked",
            "summary": f"Handler raised {type(e).__name__}: {e}"[:5000],
        }

    # 3. Record the outcome — again, only via a transition the workflow
    # actually allows. If there's a direct legal path (the common case),
    # take it, same as before #1247.
    #
    # If there ISN'T one — e.g. story/default has no `blocked` status at
    # all, or bug/default's `done_statuses` is ambiguous between
    # `closed`/`wont_fix` for a generic "done" — #1247's fix applies:
    # instead of leaving the task exactly where the claim step put it
    # (`in_progress` / `fixing` — both "active"-category, so NOT in
    # `_ACTIONABLE_STATUSES` and never re-listed; a MANUFACTURED stuck task,
    # worse than never claiming it at all), hand it off to a human by moving
    # it to the workflow's own review/handoff status (`_handoff_path` —
    # derived from the workflow's structure, never a hardcoded name; see the
    # block comment above `_handoff_candidates`). That status is a genuine
    # resolution, not just a different stuck state: `fixing`/`in_progress`
    # assert an agent is actively working the task, which is false the
    # instant the handler returned; the handoff status asserts a human
    # should look at it, which is true whether the handler succeeded or
    # failed. Auto-walking further, to a terminal status, is deliberately
    # NOT done — see the block comment above for why (an agent would be
    # closing its own bug with no human review, and guessing `closed` vs
    # `wont_fix` risks misrepresenting a fixed bug as won't-fix).
    #
    # Only when NEITHER a direct path NOR a handoff exists (no built-in
    # template hits this; a hand-authored workflow might) does today's
    # comment-only fallback still apply — status stays exactly where the
    # claim step put it, outcome recorded as a task comment, warning fires.
    # Never silent, and never forced onto an illegal or misrepresentative
    # status either way.
    #
    # The handoff hop is ALSO wrapped in that same comment-only safety net
    # if the PATCH itself fails (a race — e.g. a dependency became
    # unresolved mid-handler, so the handoff status's entry gate now 409s
    # it): see the `except SystemExit` below. The summary must reach the
    # task either way; only WHERE it lands may vary.
    outcome = _normalize_outcome(
        result, task_id, required_statuses=summary_required_statuses
    )
    path = _outcome_path(workflow, claimed_status, outcome.get("status"), task)

    handoff_target: Optional[str] = None
    if path is None:
        handoff = _handoff_path(workflow, task, claimed_status)
        if handoff:  # non-empty: a real hop exists. `None` (no qualifying
            # status at all) and `[]` (already sitting on the only
            # qualifying status — nothing to move) both fall through to the
            # comment-only branch below, unchanged.
            path = handoff
            handoff_target = path[-1]

    if not path:  # None (no legal path, including no handoff) or [] (outcome
        # or handoff already satisfied) — both need the summary recorded
        # some other way, since no hop will carry it.
        if path is None:
            sys.stderr.write(
                f"poll_tasks: task {task_id}: no legal transition to "
                f"'{outcome.get('status')}' in its workflow, and no "
                "workflow handoff status is reachable either; recording "
                f"the outcome as a comment only. Status stays "
                f"'{claimed_status}'.\n"
            )
        if outcome.get("summary"):
            _request("PATCH", f"/tasks/{task_id}", {"summary": outcome["summary"]})
    elif handoff_target is None:
        _walk_transitions(
            task_id,
            path,
            outcome.get("summary", ""),
            required_statuses=summary_required_statuses,
        )
    else:
        # #1247 review (blocker 1): `_handoff_candidates` derives its
        # target from the workflow's STRUCTURE, not a hardcoded name, so
        # the hop can legally land on an "active"-category status that
        # `summary_required_statuses` doesn't necessarily cover — e.g. a
        # hand-authored `review`/`qa`/`verifying` status, none of which
        # spell `in_review`, is never in `summary_required_statuses` (the
        # workflow-derived EXTENSION half of that union excludes every
        # active-category key; the FLOOR half only ever contains those four
        # literal names). Passing `summary_required_statuses` here still
        # matters even WITH `force_summary_on_final=True`: the force flag
        # only ever attaches `summary` to the LAST hop in `path`
        # (`index == last_index` in `_walk_transitions`) — it says nothing
        # about any INTERMEDIATE hop a multi-hop handoff `path` might pass
        # through first. `_find_transition_path`'s own waypoint guard
        # refuses a "done"-category status as a mere passthrough, but
        # PERMITS an unrecognized-category one — exactly the case the
        # workflow-derived extension (fail-closed on unrecognized
        # categories, see `_NON_TERMINAL_CATEGORIES`) puts in
        # `summary_required_statuses`. Drop `required_statuses=
        # summary_required_statuses` from this call and such an
        # intermediate hop would carry no summary at all — 422ing the whole
        # handoff before the final hop the force flag protects is ever
        # reached. `force_summary_on_final=True` forces the summary onto
        # THAT final hop regardless of whether it's separately covered, so
        # a name mismatch between the two never drops it there either —
        # still exactly one PATCH carries it, never both, never neither.
        #
        # #1247 review (the earlier round): the handoff hop is ALSO a REAL
        # status PATCH — unlike the comment-only fallback it replaces, it
        # can fail for reasons that have nothing to do with whether the
        # outcome itself is resolvable (e.g. a dependency became unresolved
        # WHILE the handler ran, and the handoff status's own entry gate
        # now 409s it; `_request` exits the process on that, and
        # `_work_task_safely` only isolates it to THIS task). The one
        # invariant that must survive regardless of what happens to the
        # status: the summary must never be silently lost just because the
        # status PATCH failed — the same #1220/#1224 class of bug, in this
        # same file. So the handoff is attempted FIRST (the summary rides
        # the hop when it works, same as any other resolvable outcome), and
        # only on failure does today's pre-#1247 comment-only PATCH run as
        # the safety net — never both, and never neither.
        sys.stderr.write(
            f"poll_tasks: task {task_id}: no legal transition to "
            f"'{outcome.get('status')}' in its workflow; attempting handoff "
            f"to '{handoff_target}' for human review instead of leaving it "
            f"at '{claimed_status}'.\n"
        )
        try:
            _walk_transitions(
                task_id,
                path,
                _handoff_summary(outcome, handoff_target),
                force_summary_on_final=True,
                required_statuses=summary_required_statuses,
            )
        except SystemExit:
            sys.stderr.write(
                f"poll_tasks: task {task_id}: handoff to '{handoff_target}' "
                "failed (see the error above); falling back to recording "
                "the outcome as a comment only, exactly like the pre-#1247 "
                "behaviour — the task's status may not reflect the intended "
                "handoff, but the summary must not be lost.\n"
            )
            if outcome.get("summary"):
                _request("PATCH", f"/tasks/{task_id}", {"summary": outcome["summary"]})

    sys.stdout.write(
        json.dumps({"task_id": task_id, "result": result.get("status")}) + "\n"
    )
    sys.stdout.flush()


def _work_task_safely(task: dict, handler: Callable[[dict], dict]) -> None:
    """`_work_task`, but a `SystemExit` from ONE task never reaches the loop.

    `_request` exits the process on a hard failure (a 4xx/5xx `_walk_transitions`
    couldn't route around, a network error past retries). That is a
    reasonable reaction to a single CLI invocation — it is not a reasonable
    reaction for a long-running worker with other queued tasks: one
    unprocessable task (a race, an unexpected required field, a transient
    5xx that outlasted the retry budget) must not strand every other task
    behind it. Warn naming the task and move on.
    """
    task_id = task.get("id") or task.get("_id") or "<unknown>"
    try:
        _work_task(task, handler)
    except SystemExit:
        sys.stderr.write(
            f"poll_tasks: task {task_id} failed unrecoverably (see the error "
            "above) — skipping it and continuing to the next task rather "
            "than exiting the worker.\n"
        )


# US-031.6: the wake-up. Event types are the task stream's own names.


def _handle_inbox_event(me: str, event: dict) -> None:
    """Act on ONE inbox event. The server has already filtered to this agent.

    * `task.created`, or `task.updated` that did not assign the task: a phase offered to
      this agent's role. Claim it (the same PATCH `spawnr_claim_task` sends). A 409
      (`already_claimed`, `role_mismatch`, `capability_mismatch`, `proposal_not_approved`,
      `task_blocked`) means it is not ours: skip it, never retry. A claimed task shows up in
      the next `/tasks/mine` listing and is worked there.
    * `task.updated` carrying `changes.assignee.new`: the task was assigned to this agent
      (the inbox only carries its own), so there is nothing to claim; the next listing works it.
    * `task.comment_added` (on a task this agent holds) and `question.answered`: print the
      event as one JSON line for the operator. The handler is not called for them.
    * anything else (`task.status_changed` included, a handoff already sends `task.updated`): ignored.
    """
    kind, data = event.get("type"), event.get("data") or {}
    if kind in ("task.comment_added", "question.answered"):
        sys.stdout.write(json.dumps({"event": kind, "data": data}) + "\n")
        sys.stdout.flush()
        return
    if kind not in ("task.created", "task.updated"):
        return
    task_id = data.get("task_id")
    assigned = ((data.get("changes") or {}).get("assignee") or {}).get("new")
    if assigned or not task_id:
        return
    _request(
        "PATCH",
        f"/tasks/{task_id}",
        # exactly what `spawnr_claim_task` sends; a task in a phase keeps the phase's status
        {"assignee_id": me, "assignee_type": "agent", "status": "in_progress"},
        treat_as_missing=frozenset({409}),
    )


def _wait_and_claim(me: str, cursor: Optional[str], wait_s: int) -> Optional[str]:
    """Long-poll the inbox once (<= `wait_s` seconds), act on each event, return the new cursor.

    The cursor goes back as `since` on the next call, so an event is neither delivered twice
    nor skipped; the first call (`cursor` None) starts from now. The socket timeout is
    `wait_s` + 10: the request is held open for `wait_s` seconds on purpose. One event that
    fails hard (`_request` exits) is reported and skipped, so it cannot hold back the rest or
    the cursor.
    """
    query = {"timeout": wait_s, **({"since": cursor} if cursor else {})}
    path = f"/agents/{me}/inbox/wait?{urllib.parse.urlencode(query)}"
    resp = _request("GET", path, timeout=wait_s + 10) or {}
    for event in resp.get("events", []):
        try:
            _handle_inbox_event(me, event)
        except SystemExit:
            sys.stderr.write(
                f"poll_tasks: inbox event {event.get('type')} for task "
                f"{(event.get('data') or {}).get('task_id')} failed unrecoverably (see the "
                "error above) — skipping it and continuing.\n"
            )
    return resp.get("cursor") or cursor


def _parse_statuses(raw: str) -> frozenset[str]:
    """Comma-separated `--statuses` override, spellchecked but never rejected.

    Each value goes through `spawnr_client._warn_unknown_status` — the same
    "warn, don't reject" rule #1225/#1230 established for `list --status`,
    for the same reason: the stored status vocabulary is workflow-defined
    and open-ended, so there is no closed set to validate against.
    """
    values = [s.strip() for s in raw.split(",") if s.strip()]
    if not values:
        sys.stderr.write("error: --statuses cannot be empty\n")
        sys.exit(2)
    for value in values:
        _warn_unknown_status(value)
    return frozenset(values)


def _resolve_actionable_statuses(raw: str | None) -> frozenset[str] | None:
    """`--statuses` -> the override set, or `None` to use `_ACTIONABLE_STATUSES`.

    `raw is not None`, NOT truthiness — a wrapper that forwards an unset
    shell var as `--statuses "$VAR"` must get a loud `--statuses cannot be
    empty` exit (from `_parse_statuses`) rather than silently falling back
    to the default set with no signal at all. That silent fallback is the
    exact "looks like it works" failure class #1233 exists to kill: `if raw`
    would treat `--statuses ""` the same as omitting the flag entirely.
    """
    if raw is None:
        return None
    return _parse_statuses(raw)


def main() -> None:
    p = argparse.ArgumentParser(prog="poll_tasks", description=__doc__)
    p.add_argument(
        "--handler", required=True, help="'module:function' importable callable"
    )
    p.add_argument(
        "--interval",
        type=int,
        default=30,
        help="seconds to wait on the inbox each round, clamped to 1-60 (default 30). "
        "An idle poller stops on SIGTERM within this long.",
    )
    p.add_argument("--workspace", help="override SPAWNR_WORKSPACE_ID for this run")
    p.add_argument(
        "--statuses",
        help="comma-separated statuses to treat as actionable, overriding the "
        "built-in default ("
        + ", ".join(sorted(_ACTIONABLE_STATUSES))
        + "). Use this for a workspace on a custom workflow whose 'not "
        "started' status keys aren't one of the built-in templates.",
    )
    args = p.parse_args()

    handler = _load_handler(args.handler)
    workspace = (
        args.workspace or os.environ.get("SPAWNR_WORKSPACE_ID", "").strip() or None
    )
    actionable_statuses = _resolve_actionable_statuses(args.statuses)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    wait_s = max(1, min(args.interval, 60))
    me: Optional[str] = None
    cursor: Optional[str] = None
    while not _stopping:
        try:
            me = me or _my_agent_id()
            tasks = _list_actionable_tasks(workspace, actionable_statuses)
        except SystemExit:
            # _request prints + exits on hard failure; convert to a sleep-retry
            time.sleep(wait_s * 2)
            continue

        for task in tasks:
            if _stopping:
                break
            _work_task_safely(task, handler)
        if _stopping:
            break

        try:
            cursor = _wait_and_claim(me, cursor, wait_s)
        except (SystemExit, socket.timeout):
            # A hard failure (403 agent_paused, 5xx past the retries) or a stalled socket:
            # sleep-retry with the same cursor. An offer delivered again is a 409 or an
            # idempotent claim, so nothing is lost or doubled.
            time.sleep(wait_s * 2)


if __name__ == "__main__":
    main()
