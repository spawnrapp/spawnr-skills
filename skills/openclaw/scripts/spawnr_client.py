#!/usr/bin/env python3
"""
spawnr_client — thin CLI wrapper around the Spawnr agent API.

Reads `SPAWNR_API_URL` (the server address, as for the `spawnr` CLI; a trailing
`/api/v1` is accepted) and `SPAWNR_API_KEY` from the environment. Optional
`SPAWNR_WORKSPACE_ID` narrows list calls to a single workspace.

Usage:
  spawnr_client.py whoami
  spawnr_client.py list [--workspace <id>] [--status <s>]
  spawnr_client.py get <task_id>
  spawnr_client.py start <task_id>
  spawnr_client.py finish <task_id> --note "..."
  spawnr_client.py fail <task_id> --reason "..."
  spawnr_client.py handoff <task_id> --to <status> --done "..." --not-done "..."
      --decisions "..." --open-questions "..." --for-next "..."
  spawnr_client.py ask <task_id> "What should I do about X?"
  spawnr_client.py questions mine
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import urllib.error
import urllib.parse
import urllib.request


def _env(name: str, required: bool = True) -> str | None:
    v = os.environ.get(name, "").strip()
    if not v and required:
        sys.stderr.write(
            f"error: ${name} is not set. See SKILL.md for the env var matrix.\n"
        )
        sys.exit(2)
    return v or None


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    max_retries: int = 3,
    error_hints: dict[int, str] | None = None,
    treat_as_missing: frozenset[int] = frozenset(),
    missing_if_detail_contains: str | None = None,
    timeout: float = 30,
) -> Any:
    """HTTP call with X-API-Key auth, JSON in/out, 429-aware retries.

    `timeout` is the socket timeout in seconds (default 30). A long-poll such as
    `GET /agents/{id}/inbox/wait?timeout=N` holds the request open for up to N
    seconds, so its caller passes more than N, or the wait itself would time out.

    `error_hints` maps an HTTP status code to a short, call-site-specific
    explanation appended to the printed error — e.g. a 404 on a scoped
    resource means "not visible to you", not "endpoint missing". Optional
    and keyword-only so existing callers (including `poll_tasks.py`'s
    `from spawnr_client import _request`) are unaffected.

    `treat_as_missing` names status codes that mean "this resource legally
    doesn't exist" rather than "the request failed" — e.g.
    `GET /workflows/{item_type}` 404s for a workspace with no CUSTOM
    workflow, which `poll_tasks.py` needs to read as "any-to-any applies",
    not as a fatal error worth killing the process over. Checked first, so
    it short-circuits before the 429/5xx retry branches below — a code
    named here is never worth retrying, it's an expected, immediate answer.
    Empty by default: every other caller keeps today's exit-on-error
    behaviour unchanged.

    `missing_if_detail_contains` narrows a `treat_as_missing` code further,
    to the response's OWN error text — a code alone can be too coarse.
    `GET /workflows/{item_type}` 404s for TWO different reasons with the
    SAME status code (`app/api/routes/workflows.py`'s `get_workflow`):
    "Workspace not found" (a scope/visibility problem — the caller can't
    see this workspace at all) and "No custom workflow for {item_type}..."
    (a real, safe-to-treat-as-missing case). Conflating them would let a
    scope error silently become "any-to-any applies" instead of surfacing
    loudly — the opposite of what `treat_as_missing` is for. When set, the
    code is only treated as missing if this substring appears in the body;
    otherwise execution falls through to the normal error path below,
    reusing the body already read rather than reading the (single-use)
    response stream twice.
    """
    # SPAWNR_API_URL is the host root, as for the `spawnr` CLI. A trailing
    # `/api/v1` (this script's old form) is accepted and dropped, then added
    # back once, so both forms request the same URL.
    base = _env("SPAWNR_API_URL").rstrip("/").removesuffix("/api/v1")  # type: ignore[union-attr]
    key = _env("SPAWNR_API_KEY")

    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{base}/api/v1{path}"

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            body_text: str | None = None
            if e.code in treat_as_missing:
                body_text = e.read().decode("utf-8", errors="replace")
                if (
                    missing_if_detail_contains is None
                    or missing_if_detail_contains in body_text
                ):
                    return None
                # Not actually "missing" by the caller's own narrower
                # signal — fall through to the normal error path, reusing
                # the body already consumed above.
            if e.code == 429 and attempt < max_retries:
                retry_after = int(e.headers.get("Retry-After", "5"))
                time.sleep(min(retry_after, 60))
                continue
            if 500 <= e.code < 600 and attempt < max_retries:
                time.sleep(2**attempt)
                continue
            if body_text is None:
                body_text = e.read().decode("utf-8", errors="replace")
            hint = (error_hints or {}).get(e.code)
            prefix = f"HTTP {e.code} on {method} {path}"
            if hint:
                sys.stderr.write(
                    f"{prefix}: {hint}\n  (server said: {body_text[:300]})\n"
                )
            else:
                sys.stderr.write(f"{prefix}: {body_text[:300]}\n")
            sys.exit(1)
        except urllib.error.URLError as e:
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            sys.stderr.write(f"network error on {method} {path}: {e.reason}\n")
            sys.exit(1)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ─── Subcommands ─────────────────────────────────────────────────────────────


def cmd_whoami(_args: argparse.Namespace) -> None:
    _print_json(_request("GET", "/agents/me"))


# The WRITE-accepted status vocabulary, duplicated from `VALID_STATUSES` in
# backend/app/api/routes/tasks.py — i.e. what `PATCH /tasks/{id}` accepts.
#
# It is NOT the set of statuses a task can HAVE. The stored vocabulary is
# deliberately open-ended: workflow templates carry their own statuses and
# `apply_template` writes them via `update_many` with no validator, so tasks
# legitimately sit at `draft`/`ready` (story template) or
# `new`/`triaged`/`fixing`/`closed`/`wont_fix` (bug template) — none of which
# are in VALID_STATUSES (see the QA-112 note in app/api/routes/workflows.py).
# That is why `--status` WARNS rather than rejects: this set is a spellcheck
# hint, never an allowlist.
#
# Script is stdlib-only and can't import the backend (same constraint as
# `poll_tasks._SUMMARY_REQUIRED_STATUSES` vs `AGENT_SUMMARY_REQUIRED_STATUSES`,
# #1220), so it's duplicated and pinned by
# backend/tests/test_openclaw_status_vocabulary.py.
_TASK_STATUSES = frozenset(
    {
        # VALID_STATUSES — the write-accepted set.
        "open",
        "in_progress",
        "in_review",
        "blocked",
        "done",
        "canceled",
        # Built-in workflow template statuses. Outside VALID_STATUSES, but
        # tasks really carry these, so warning on them would be noise on a
        # standard bug or story board — and a warning that cries wolf on
        # normal input is one people learn to ignore.
        "draft",
        "ready",  # story/default
        "new",
        "triaged",
        "fixing",
        "closed",
        "wont_fix",  # bug/default
    }
)

# `GET /tasks/mine` (see list_my_tasks in backend/app/api/routes/tasks.py)
# caps its result at 100 tasks (`.to_list(length=100)`), newest-created
# first. That cap is independent of `--status` but interacts badly with it:
# client-side filtering (below) only ever sees the first 100, so an agent
# with more than 100 assignments whose newest 100 happen to all be closed
# would see zero results for `--status open` — indistinguishable, to an
# autonomous caller, from "no work to do".
_TASKS_MINE_SERVER_CAP = 100


def _warn_unknown_status(value: str) -> str:
    """Warn — never reject — on an unfamiliar `--status`.

    The problem being solved is SILENCE, not permissiveness. `GET /tasks/mine`
    has no server-side `status` param (#1223 moved filtering client-side), so a
    typo like `--status compelted` doesn't 422; it just matches nothing and
    looks exactly like "this agent has no open tasks". A warning removes that
    ambiguity.

    Rejecting would be wrong, because there is no closed set to reject
    against. ``_TASK_STATUSES`` is the WRITE-accepted vocabulary
    (``VALID_STATUSES``, what ``PATCH /tasks/{id}`` allows) — the STORED
    vocabulary is deliberately open-ended. Workflow templates carry their own
    statuses and ``apply_template`` writes them straight through
    ``update_many`` with no validator, so a task legitimately sits at
    ``draft``/``ready`` (story template) or
    ``new``/``triaged``/``fixing``/``closed``/``wont_fix`` (bug template) —
    see the QA-112 note in ``app/api/routes/workflows.py``. ``/tasks/mine``
    has no ``type`` filter, so those tasks come back here. QA-122 documents
    the general case: a stored status may sit outside the workspace's own
    workflow set. Exiting 2 on ``--status new`` would break a query that
    works today.
    """
    if value not in _TASK_STATUSES:
        sys.stderr.write(
            f"warning: --status {value!r} is not a status this client "
            "recognises; filtering anyway. Workflows can define any status, "
            "so this may well be correct — but check the spelling if the "
            f"result is empty. Recognised: {', '.join(sorted(_TASK_STATUSES))}.\n"
        )
    return value


def cmd_list(args: argparse.Namespace) -> None:
    # `GET /tasks/mine` accepts only `workspace_id` as a query param — it has
    # no `status` filter. FastAPI silently drops unknown query params rather
    # than rejecting them, so posting `?status=...` here would not 422; it
    # would just be ignored, making `--status` a flag that looks like it
    # works but doesn't. Filter client-side instead. Note this is filtering
    # *after* the server's own 100-task cap (`.to_list(length=100)`), so a
    # workspace with more than 100 tasks assigned to this agent may show
    # fewer `--status`-matching results than actually exist — see the
    # stderr warning below, which fires whenever that cap and `--status` are
    # both in play.
    if args.status:
        _warn_unknown_status(args.status)

    params: dict[str, str] = {}
    ws = args.workspace or os.environ.get("SPAWNR_WORKSPACE_ID", "").strip()
    if ws:
        params["workspace_id"] = ws
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    result = _request("GET", f"/tasks/mine{qs}") or {}
    tasks = result.get("tasks", [])
    if args.status:
        if len(tasks) == _TASKS_MINE_SERVER_CAP:
            # stdout is this script's machine-readable contract — never put
            # this on stdout. stderr is safe: it's for humans/log capture.
            sys.stderr.write(
                f"warning: /tasks/mine returned {_TASKS_MINE_SERVER_CAP} tasks "
                f"(its server-side cap) before --status filtering was applied; "
                "there may be more matching tasks the server never returned.\n"
            )
        tasks = [t for t in tasks if t.get("status") == args.status]
        result = {**result, "tasks": tasks}
    _print_json(result)


def _task_not_visible_hint(task_id: str) -> str:
    # #1186 — scope validation on a task-linked write/read means a 404 here
    # means "this task isn't visible to this agent" (wrong org, wrong
    # workspace, or it doesn't exist), never "the endpoint is missing".
    return f"task {task_id!r} was not found, or is outside this agent's scope."


def cmd_get(args: argparse.Namespace) -> None:
    _print_json(
        _request(
            "GET",
            f"/tasks/{args.task_id}",
            error_hints={404: _task_not_visible_hint(args.task_id)},
        )
    )


def cmd_start(args: argparse.Namespace) -> None:
    _print_json(
        _request(
            "PATCH",
            f"/tasks/{args.task_id}",
            {"status": "in_progress"},
            error_hints={404: _task_not_visible_hint(args.task_id)},
        )
    )


def _require_nonblank(value: str, flag: str) -> str:
    """Fail with a clear client-side message rather than a raw 422.

    #1130's `summary_required` check rejects `None` *and* whitespace-only
    text from an agent caller — this mirrors that rule before the request
    goes out.
    """
    if not value.strip():
        sys.stderr.write(f"error: {flag} cannot be blank\n")
        sys.exit(2)
    return value


def _require_within_summary_limit(value: str, flag: str) -> str:
    """Fail with a clear client-side message rather than a raw 422.

    `UpdateTaskRequest.summary` (backend/app/api/routes/tasks.py) is capped
    at `MAX_COMMENT_LEN` (5000, backend/app/api/routes/task_comments.py) —
    `--note`/`--reason` are posted straight into it by `cmd_finish`/
    `cmd_fail`. Unlike `_block_summary` below, which TRUNCATES because it is
    generating a wrapper string around text it did not author, `--note`/
    `--reason` is text the caller wrote on purpose. Silently dropping its
    tail could discard the actual point of the note (e.g. a root cause
    mentioned past char 5000) with no indication that happened — an error
    the caller can react to is safer than a quiet, lossy truncation of
    user-authored content.
    """
    if len(value) > _MAX_TASK_SUMMARY_LEN:
        sys.stderr.write(
            f"error: {flag} is {len(value)} chars, over the "
            f"{_MAX_TASK_SUMMARY_LEN}-char limit the server enforces on task "
            "summaries. Shorten it and try again.\n"
        )
        sys.exit(2)
    return value


def cmd_finish(args: argparse.Namespace) -> None:
    # UpdateTaskRequest (backend/app/api/routes/tasks.py) requires a
    # non-empty `summary` from an agent (X-API-Key) caller whenever `status`
    # moves to done/blocked/canceled/in_review (#1130) — `description` does
    # NOT satisfy that check, and posting it here would wholesale *replace*
    # the task's description field rather than append a note, contradicting
    # what this command has always claimed to do. `summary` is instead
    # recorded as a task comment, which is the actual "append a completion
    # note" behavior — so `--note` is now required to make that requirement
    # explicit rather than 422ing with no client-side explanation.
    note = _require_nonblank(args.note, "--note")
    note = _require_within_summary_limit(note, "--note")
    body: dict[str, Any] = {"status": "done", "summary": note}
    _print_json(
        _request(
            "PATCH",
            f"/tasks/{args.task_id}",
            body,
            error_hints={404: _task_not_visible_hint(args.task_id)},
        )
    )


def cmd_fail(args: argparse.Namespace) -> None:
    # Same #1130 requirement as cmd_finish — `summary`, not `description`.
    reason = _require_nonblank(args.reason, "--reason")
    reason = _require_within_summary_limit(reason, "--reason")
    body = {"status": "blocked", "summary": reason}
    _print_json(
        _request(
            "PATCH",
            f"/tasks/{args.task_id}",
            body,
            error_hints={404: _task_not_visible_hint(args.task_id)},
        )
    )


# The five fields of a handoff and the flag each one is passed with (US-031.5, epic 031
# D5). Under an agent flow a status change that enters, leaves or moves between the
# flow's phases needs all five, or the server answers 422 handoff_required.
_HANDOFF_FIELDS = (
    ("done", "--done"),
    ("not_done", "--not-done"),
    ("decisions", "--decisions"),
    ("open_questions", "--open-questions"),
    ("for_next", "--for-next"),
)


def cmd_handoff(args: argparse.Namespace) -> None:
    # One PATCH carries the status and the handoff, which stands in for the summary.
    # Each field is checked client-side so a blank one is a clear message rather than
    # a 422 round trip; "None." is the way to say a field does not apply.
    handoff = {
        field: _require_within_summary_limit(
            _require_nonblank(getattr(args, field), flag), flag
        )
        for field, flag in _HANDOFF_FIELDS
    }
    _print_json(
        _request(
            "PATCH",
            f"/tasks/{args.task_id}",
            {"status": args.to, "handoff": handoff},
            error_hints={404: _task_not_visible_hint(args.task_id)},
        )
    )


def cmd_ask(args: argparse.Namespace) -> None:
    # CreateQuestionRequest (backend/app/api/routes/questions.py) has no
    # `task_id` field and is `extra="forbid"` — the task linkage is
    # `context_type` + `context_ref`, per docs/user-guides/
    # openclaw-onboarding.md's corrected example (#1219).
    body = {
        "question": args.question,
        "context_type": "task",
        "context_ref": args.task_id,
    }
    # #1227: repeatable --option (up to 9) offers a multiple-choice pick —
    # the server assigns positional ids "1".."9". `getattr` (not `args.option`
    # directly) because callers constructing a bare argparse.Namespace in
    # tests may not set it.
    options = getattr(args, "option", None) or []
    if options:
        body["options"] = options
    q = _request(
        "POST",
        "/questions/",
        body,
        error_hints={
            # #1186 — ask-time scope validation resolves context_ref through
            # the same org/workspace-scoped lookup every task route uses; an
            # out-of-scope or nonexistent task 404s here, before the
            # question is ever created.
            404: _task_not_visible_hint(args.task_id),
        },
    )
    # Print the created question before the follow-up PATCH so a failure
    # blocking the task doesn't hide that the question itself was created.
    _print_json(q)
    _request(
        "PATCH",
        f"/tasks/{args.task_id}",
        {"status": "blocked", "summary": _block_summary(args.question)},
        error_hints={404: _task_not_visible_hint(args.task_id)},
    )


# UpdateTaskRequest.summary is capped at MAX_COMMENT_LEN (5000, task_comments.py)
# and CreateQuestionRequest.question at MAX_QUESTION_LEN (5000, questions.py) —
# the SAME limit. The overflow comes from the 27-char "Blocked pending an
# answer: " prefix below: a question at or near its own legal 5000 posts fine,
# then the prefixed summary exceeds 5000 and 422s the follow-up PATCH — the
# exact class of bug this issue exists to fix, just one field over.
_MAX_TASK_SUMMARY_LEN = 5000


def _block_summary(question: str) -> str:
    summary = f"Blocked pending an answer: {question}"
    if len(summary) <= _MAX_TASK_SUMMARY_LEN:
        return summary
    return summary[: _MAX_TASK_SUMMARY_LEN - 1] + "…"


def _my_agent_id() -> str:
    """Resolve this agent's own id from `/agents/me`.

    The id `/agents/me` returns is `str(agent["_id"])` (see backend
    agents.py `get_current_agent`), which is exactly the value the
    `/questions/` list route matches against its stored `agent_id`. Resolving
    it here keeps the "whose questions" decision in one place.
    """
    me = _request("GET", "/agents/me")
    agent_id = (me or {}).get("id")
    if not agent_id:
        sys.stderr.write("error: /agents/me did not return an id\n")
        sys.exit(1)
    return str(agent_id)


def cmd_questions_mine(_args: argparse.Namespace) -> None:
    # There is no `/questions/mine` route. "My questions" = questions this
    # agent created, which `/questions/` returns when filtered by this agent's
    # id (`?agent_id=<my_id>`). Resolve the id from `/agents/me` first.
    agent_id = _my_agent_id()
    qs = "?" + urllib.parse.urlencode({"agent_id": agent_id})
    _print_json(_request("GET", f"/questions/{qs}"))


def main() -> None:
    p = argparse.ArgumentParser(prog="spawnr_client", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami").set_defaults(func=cmd_whoami)

    pl = sub.add_parser("list")
    pl.add_argument("--workspace", help="workspace id (overrides SPAWNR_WORKSPACE_ID)")
    pl.add_argument(
        "--status",
        # Deliberately NOT "one of: ..." — any status is accepted, because
        # workflows can define their own and this client cannot know them.
        # Naming a closed set here would be the same false claim that was
        # corrected in README.md and reference/api.md.
        help="filter by status, client-side. Any value is accepted; "
        "an unrecognised one warns on stderr. Commonly: "
        + ", ".join(sorted(_TASK_STATUSES)),
    )
    pl.set_defaults(func=cmd_list)

    pg = sub.add_parser("get")
    pg.add_argument("task_id")
    pg.set_defaults(func=cmd_get)

    ps = sub.add_parser("start")
    ps.add_argument("task_id")
    ps.set_defaults(func=cmd_start)

    pf = sub.add_parser("finish")
    pf.add_argument("task_id")
    pf.add_argument(
        "--note",
        required=True,
        help="completion summary, posted as a task comment (required: the "
        "backend requires a non-empty summary from an agent caller "
        "closing a task, #1130)",
    )
    pf.set_defaults(func=cmd_finish)

    px = sub.add_parser("fail")
    px.add_argument("task_id")
    px.add_argument("--reason", required=True, help="failure reason (logged)")
    px.set_defaults(func=cmd_fail)

    ph = sub.add_parser(
        "handoff",
        help="change a task's phase under an agent flow, with the five-field handoff",
    )
    ph.add_argument("task_id")
    ph.add_argument("--to", required=True, help="the status to move the task to")
    for field, flag in _HANDOFF_FIELDS:
        ph.add_argument(flag, required=True, help=f"handoff {field} (write None. if n/a)")
    ph.set_defaults(func=cmd_handoff)

    pa = sub.add_parser("ask")
    pa.add_argument("task_id")
    pa.add_argument("question")
    pa.add_argument(
        "--option",
        action="append",
        help="offer a multiple-choice pick instead of free text; repeat "
        "for up to 9 options (server assigns positional ids 1-9)",
    )
    pa.set_defaults(func=cmd_ask)

    pq = sub.add_parser("questions", description="Question-related actions")
    pq_sub = pq.add_subparsers(dest="subcmd", required=True)
    pq_sub.add_parser("mine").set_defaults(func=cmd_questions_mine)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
