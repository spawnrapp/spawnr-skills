"""Tests for skills/openclaw/scripts/spawnr_client.py.

Not collected by any CI job or git hook today: the backend pytest gate
(``.github/workflows/ci.yml``'s ``test-backend`` job, and the pre-push
hook's ``scripts/run-backend-pytest.sh``) always ``cd``s into ``backend/``
before running, and there is no repo-root ``pytest.ini``/``pyproject.toml``
that would pick up ``skills/`` either. Run it explicitly, from the repo root:

    python3 -m pytest skills/openclaw/tests/test_spawnr_client.py -v

The script under test has zero third-party imports (stdlib only — see its
own module docstring), so any Python 3.10+ interpreter with pytest
installed works. If this checkout has a populated ``backend/.venv``
(created per the repo CLAUDE.md — not every worktree has one),
``backend/.venv/bin/python -m pytest ...`` works too and is the
persistent, dependency-pinned option; it is not required for this file.

Every request body asserted here is checked against the corresponding
Pydantic model as of this commit:
  - CreateQuestionRequest       backend/app/api/routes/questions.py
  - UpdateTaskRequest            backend/app/api/routes/tasks.py
  - AGENT_SUMMARY_REQUIRED_STATUSES / _check_agent_summary_required (#1130)
                                  backend/app/api/routes/tasks.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import urllib.error

import pytest

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import spawnr_client as sc  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # _env() exits 2 if either is unset — every _request call needs both.
    monkeypatch.setenv("SPAWNR_API_URL", "https://spawnr.example.com/api/v1")
    monkeypatch.setenv("SPAWNR_API_KEY", "spawnr_test_key")


class _Recorder:
    """Captures every call made through `_request` without touching the network.

    Records `error_hints` (and any other kwarg) alongside method/path/body —
    a call site can build a perfectly valid body and still fail AC4 by
    forgetting to pass `error_hints`, so that has to be observable too.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.responses: list[object] = []

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        return self.responses.pop(0) if self.responses else {}


# ─── AC1: cmd_ask posts a body CreateQuestionRequest accepts ────────────────


def test_ask_posts_context_type_and_context_ref_not_task_id(monkeypatch):
    """The exact bug in #1220: `task_id` is not a CreateQuestionRequest field
    and the model is `extra="forbid"`, so the old body 422s. The fix maps it
    onto `context_type`/`context_ref`."""
    rec = _Recorder()
    rec.responses = [{"id": "q1", "status": "pending"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    args = sc.argparse.Namespace(task_id="TASK-42", question="What should I do?")
    sc.cmd_ask(args)

    assert len(rec.calls) == 2
    method, path, body, kwargs = rec.calls[0]
    assert (method, path) == ("POST", "/questions/")
    assert "task_id" not in body
    assert body["context_type"] == "task"
    assert body["context_ref"] == "TASK-42"
    assert body["question"] == "What should I do?"
    # AC4: the POST must also carry a 404 hint — a task-not-visible 404 can
    # happen here too (#1186 resolves context_ref before creating the row).
    assert "outside this agent's scope" in kwargs["error_hints"][404]


def test_ask_blocks_the_task_with_a_summary(monkeypatch):
    """The follow-up PATCH must also satisfy #1130's summary_required check
    (status -> blocked requires a non-empty `summary` for an agent caller)."""
    rec = _Recorder()
    rec.responses = [{"id": "q1"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_ask(sc.argparse.Namespace(task_id="TASK-42", question="Deploy how?"))

    method, path, body, kwargs = rec.calls[1]
    assert (method, path) == ("PATCH", "/tasks/TASK-42")
    assert body["status"] == "blocked"
    assert body.get("summary", "").strip()
    assert "Deploy how?" in body["summary"]
    # AC4: this PATCH is the other place a scope-related 404 can surface.
    assert "outside this agent's scope" in kwargs["error_hints"][404]


def test_ask_truncates_a_long_question_in_the_block_summary(monkeypatch):
    """`question` (CreateQuestionRequest) allows up to 5000 chars; the
    follow-up PATCH's `summary` (UpdateTaskRequest, via MAX_COMMENT_LEN) is
    ALSO capped at 5000 but the summary text wraps the question in a longer
    string ("Blocked pending an answer: " + question) — so a maximal-length
    question would overflow the summary field unless truncated client-side."""
    rec = _Recorder()
    rec.responses = [{"id": "q1"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    long_question = "x" * 5000
    sc.cmd_ask(sc.argparse.Namespace(task_id="TASK-42", question=long_question))

    _method, _path, body, _kwargs = rec.calls[1]
    assert len(body["summary"]) <= sc._MAX_TASK_SUMMARY_LEN


def test_ask_prints_the_question_before_blocking(monkeypatch, capsys):
    """If the PATCH fails, the created question's id must already be visible."""
    rec = _Recorder()
    rec.responses = [{"id": "q1"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_ask(sc.argparse.Namespace(task_id="TASK-42", question="Deploy how?"))

    out = capsys.readouterr().out
    assert '"id": "q1"' in out


def test_ask_with_options_forwards_options_list(monkeypatch):
    """#1227: --option is repeatable (argparse `action="append"`); cmd_ask
    forwards it verbatim as `options` — the server assigns positional ids."""
    rec = _Recorder()
    rec.responses = [{"id": "q1", "status": "pending"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    args = sc.argparse.Namespace(
        task_id="TASK-42",
        question="Which deploy strategy?",
        option=["Blue-green", "Rolling"],
    )
    sc.cmd_ask(args)

    _method, _path, body, _kwargs = rec.calls[0]
    assert body["options"] == ["Blue-green", "Rolling"]


def test_ask_without_options_omits_the_key(monkeypatch):
    """No --option given (bare Namespace, no `option` attr) -> no `options`
    key at all, never an empty-list placeholder."""
    rec = _Recorder()
    rec.responses = [{"id": "q1"}, {"id": "t1"}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_ask(sc.argparse.Namespace(task_id="TASK-42", question="Deploy how?"))

    _method, _path, body, _kwargs = rec.calls[0]
    assert "options" not in body


# ─── AC2: audit other request bodies against their models ──────────────────


def test_finish_posts_summary_not_description(monkeypatch):
    """#1130: UpdateTaskRequest.description is a wholesale field REPLACE, not
    an append, and does not satisfy the agent-summary-required check for
    status=done. `summary` is the field that's both required and recorded
    as a task comment (an actual append)."""
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_finish(sc.argparse.Namespace(task_id="T1", note="Fixed the bug."))

    method, path, body, kwargs = rec.calls[0]
    assert (method, path) == ("PATCH", "/tasks/T1")
    assert body == {"status": "done", "summary": "Fixed the bug."}
    assert "description" not in body
    # AC4: same scope-check applies to any status-changing PATCH.
    assert "outside this agent's scope" in kwargs["error_hints"][404]


def test_finish_rejects_blank_note_client_side(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    with pytest.raises(SystemExit) as exc:
        sc.cmd_finish(sc.argparse.Namespace(task_id="T1", note="   "))

    assert exc.value.code == 2
    assert rec.calls == []  # never made the network call


def test_fail_posts_summary_not_description(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_fail(sc.argparse.Namespace(task_id="T1", reason="Missing credentials."))

    method, path, body, kwargs = rec.calls[0]
    assert (method, path) == ("PATCH", "/tasks/T1")
    assert body == {"status": "blocked", "summary": "Missing credentials."}
    assert "description" not in body
    assert "outside this agent's scope" in kwargs["error_hints"][404]


# ─── US-031.5: handoff on a phase change under an agent flow ────────────────


def _handoff_args(**over):
    base = dict(
        task_id="SPW-12",
        to="testing",
        done="Parser written.",
        not_done="Error messages.",
        decisions="Kept the old grammar.",
        open_questions="None.",
        for_next="role:tester: run the edge cases.",
    )
    return sc.argparse.Namespace(**{**base, **over})


def test_handoff_sends_status_and_all_five_fields_in_one_patch(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_handoff(_handoff_args())

    assert len(rec.calls) == 1
    method, path, body, kwargs = rec.calls[0]
    assert (method, path) == ("PATCH", "/tasks/SPW-12")
    assert body == {
        "status": "testing",
        "handoff": {
            "done": "Parser written.",
            "not_done": "Error messages.",
            "decisions": "Kept the old grammar.",
            "open_questions": "None.",
            "for_next": "role:tester: run the edge cases.",
        },
    }
    assert "summary" not in body  # the handoff stands in for it
    assert "outside this agent's scope" in kwargs["error_hints"][404]


@pytest.mark.parametrize(
    "field", ["done", "not_done", "decisions", "open_questions", "for_next"]
)
def test_handoff_rejects_a_blank_field_client_side(monkeypatch, field):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    with pytest.raises(SystemExit) as exc:
        sc.cmd_handoff(_handoff_args(**{field: "  "}))

    assert exc.value.code == 2
    assert rec.calls == []  # never made the network call


def test_start_body_is_unaffected_by_summary_required(monkeypatch):
    """status=in_progress is not in AGENT_SUMMARY_REQUIRED_STATUSES — this
    call site's body was already correct; confirm it stays that way. It also
    now carries the same 404 hint as the other task PATCHes."""
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_start(sc.argparse.Namespace(task_id="T1"))

    method, path, body, kwargs = rec.calls[0]
    assert (method, path, body) == ("PATCH", "/tasks/T1", {"status": "in_progress"})
    assert "outside this agent's scope" in kwargs["error_hints"][404]


def test_list_status_flag_is_filtered_client_side(monkeypatch):
    """GET /tasks/mine has no `status` query param (list_my_tasks only
    accepts `workspace_id`); FastAPI silently drops unknown params, so this
    flag previously did nothing server-side."""
    rec = _Recorder()
    rec.responses = [
        {
            "tasks": [
                {"id": "1", "status": "open"},
                {"id": "2", "status": "blocked"},
                {"id": "3", "status": "open"},
            ]
        }
    ]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status="open"))

    # No `status` sent over the wire — see test_list_status_flag_filters_
    # printed_output below for the client-side filtering assertion.
    _method, path, _body, _kwargs = rec.calls[0]
    assert "status" not in path


def test_list_status_flag_filters_printed_output(monkeypatch, capsys):
    rec = _Recorder()
    rec.responses = [
        {
            "tasks": [
                {"id": "1", "status": "open"},
                {"id": "2", "status": "blocked"},
            ]
        }
    ]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status="open"))

    out = capsys.readouterr().out
    assert '"id": "1"' in out
    assert '"id": "2"' not in out


# ─── AC1/AC2 (#1225): --status 100-cap warning + unknown-value rejection ────


def test_list_status_warns_on_stderr_when_server_hits_the_100_cap(monkeypatch, capsys):
    """#1225 — GET /tasks/mine caps at 100 (`.to_list(length=100)`), newest
    first. If those 100 are all filtered out client-side, an agent with more
    than 100 assignments can't tell "no open tasks" from "truncated before
    my open tasks were ever returned". The warning must land on stderr —
    stdout is this script's machine-readable contract."""
    rec = _Recorder()
    rec.responses = [{"tasks": [{"id": str(i), "status": "done"} for i in range(100)]}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status="open"))

    out, err = capsys.readouterr()
    assert "100" in err
    assert "cap" in err.lower()
    # stdout must stay clean JSON — parseable, and containing only the
    # (empty, after filtering) result, no warning text mixed in.
    import json as _json

    parsed = _json.loads(out)
    assert parsed["tasks"] == []


def test_list_status_no_warning_below_the_cap(monkeypatch, capsys):
    rec = _Recorder()
    rec.responses = [{"tasks": [{"id": str(i), "status": "open"} for i in range(5)]}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status="open"))

    assert capsys.readouterr().err == ""


def test_list_no_warning_at_the_cap_when_status_flag_is_unset(monkeypatch, capsys):
    """The cap alone isn't the bug — it's the cap combined with client-side
    filtering silently dropping results. Without --status nothing is being
    filtered, so no warning is warranted."""
    rec = _Recorder()
    rec.responses = [{"tasks": [{"id": str(i), "status": "open"} for i in range(100)]}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status=None))

    assert capsys.readouterr().err == ""


def test_list_warns_on_unknown_status_but_still_filters(monkeypatch, capsys):
    """A typo like `--status compelted` must not be SILENT — but it must not
    be fatal either. The script has no closed set to reject against: the
    stored vocabulary is open-ended (workflow templates write their own
    statuses through `apply_template` with no validator), so exiting 2 would
    break `--status new` and `--status draft`, which return real tasks today.
    Warn, and run the query anyway."""
    rec = _Recorder()
    rec.responses = [{"tasks": []}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status="compelted"))

    err = capsys.readouterr().err
    assert "compelted" in err
    assert "open" in err  # the standard set is offered as a spelling hint
    assert rec.calls, "the query must still run — a warning is not a rejection"


@pytest.mark.parametrize("status", ["new", "draft", "triaged", "ready", "wont_fix"])
def test_list_still_queries_workflow_template_statuses(monkeypatch, status):
    """The regression this replaced a hard reject to avoid.

    `new`/`triaged`/`fixing`/`closed`/`wont_fix` (bug template) and
    `draft`/`ready` (story template) are all outside VALID_STATUSES, yet tasks
    legitimately carry them — `apply_template` `$set`s them via `update_many`
    with no validator, and `/tasks/mine` has no `type` filter, so they come
    back here. Rejecting these would break a working query.
    """
    rec = _Recorder()
    rec.responses = [{"tasks": [{"id": "T1", "status": status}]}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status=status))

    assert rec.calls, f"--status {status} must still query, not exit"


@pytest.mark.parametrize("status", sorted(sc._TASK_STATUSES))
def test_list_accepts_every_known_status(monkeypatch, capsys, status):
    """Parametrized off `sc._TASK_STATUSES` itself, not a third hardcoded
    copy of the vocabulary — the non-tautological drift check against the
    backend's `VALID_STATUSES` lives in
    backend/tests/test_openclaw_status_vocabulary.py.

    Asserts stderr is EMPTY, not just that the request went through: now
    that no status exits, `len(rec.calls) == 1` alone holds for literally
    any string, which would make this test vacuous. The silence is the
    claim — a recognised status must not warn."""
    rec = _Recorder()
    rec.responses = [{"tasks": []}]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_list(sc.argparse.Namespace(workspace=None, status=status))

    assert len(rec.calls) == 1  # the request went through
    assert capsys.readouterr().err == "", f"{status!r} is recognised; must not warn"


# ─── AC3 (#1225): --note / --reason checked against the 5000 cap ───────────


def test_finish_rejects_a_note_over_the_5000_cap_client_side(monkeypatch):
    """UpdateTaskRequest.summary (MAX_COMMENT_LEN) caps at 5000 server-side;
    this must be rejected before the request goes out — errored, not
    truncated, because unlike `_block_summary`'s generated wrapper text,
    `--note` is user-authored and silently dropping its tail could discard
    the actual point of the note."""
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    with pytest.raises(SystemExit) as exc:
        sc.cmd_finish(sc.argparse.Namespace(task_id="T1", note="x" * 5001))

    assert exc.value.code == 2
    assert rec.calls == []


def test_finish_allows_a_note_at_exactly_the_cap(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_finish(sc.argparse.Namespace(task_id="T1", note="x" * 5000))

    assert len(rec.calls) == 1
    assert rec.calls[0][2]["summary"] == "x" * 5000


def test_fail_rejects_a_reason_over_the_5000_cap_client_side(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    with pytest.raises(SystemExit) as exc:
        sc.cmd_fail(sc.argparse.Namespace(task_id="T1", reason="x" * 5001))

    assert exc.value.code == 2
    assert rec.calls == []


def test_fail_allows_a_reason_at_exactly_the_cap(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_fail(sc.argparse.Namespace(task_id="T1", reason="x" * 5000))

    assert len(rec.calls) == 1
    assert rec.calls[0][2]["summary"] == "x" * 5000


def test_questions_mine_uses_agent_id_from_agents_me(monkeypatch):
    """GET /questions/ does declare `agent_id` as a Query param — this call
    site was already correct; confirm it stays that way."""
    rec = _Recorder()
    rec.responses = [
        {"id": "agent-123", "name": "openclaw-reviewer"},
        {"questions": []},
    ]
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_questions_mine(sc.argparse.Namespace())

    assert rec.calls[0] == ("GET", "/agents/me", None, {})
    method, path, _body, _kwargs = rec.calls[1]
    assert method == "GET"
    assert path.startswith("/questions/?")
    assert "agent_id=agent-123" in path


def test_get_and_whoami_send_no_body(monkeypatch):
    """Bodies were already correct on both GETs; nothing to fix there.
    `cmd_get` additionally now carries the same scope-404 hint as the
    write paths (`/agents/me` is never task-scoped, so no hint applies)."""
    rec = _Recorder()
    monkeypatch.setattr(sc, "_request", rec)

    sc.cmd_get(sc.argparse.Namespace(task_id="T1"))
    sc.cmd_whoami(sc.argparse.Namespace())

    (method1, path1, body1, kwargs1), (method2, path2, body2, kwargs2) = rec.calls
    assert (method1, path1, body1) == ("GET", "/tasks/T1", None)
    assert "outside this agent's scope" in kwargs1["error_hints"][404]
    assert (method2, path2, body2, kwargs2) == ("GET", "/agents/me", None, {})


# ─── AC4: a 404 on a scoped task surfaces clearly, not generically ──────────


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, payload=b'{"detail": "Task not found"}'):
        import io

        super().__init__(
            url="https://spawnr.example.com/api/v1/questions/",
            code=code,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(payload),
        )
        self._payload = payload

    def read(self):
        return self._payload


def test_request_404_with_hint_names_scope_not_missing_endpoint(monkeypatch, capsys):
    """#1186 — ask-time scope validation 404s when context_ref resolves
    outside the agent's org/workspace. That must read as "not visible to
    you", not a generic/ambiguous failure."""

    def _raise_404(*a, **kw):
        raise _FakeHTTPError(404)

    monkeypatch.setattr(sc.urllib.request, "urlopen", _raise_404)

    with pytest.raises(SystemExit) as exc:
        sc._request(
            "POST",
            "/questions/",
            {"question": "q", "context_type": "task", "context_ref": "OUT-OF-SCOPE"},
            error_hints={404: sc._task_not_visible_hint("OUT-OF-SCOPE")},
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "outside this agent's scope" in err
    assert "endpoint" not in err.lower()


def test_request_404_without_hint_falls_back_to_raw_body(monkeypatch, capsys):
    """No `error_hints` passed (e.g. cmd_get) -> unchanged, pre-existing
    behavior: print the raw server error body."""

    def _raise_404(*a, **kw):
        raise _FakeHTTPError(404, payload=b'{"detail": "nope"}')

    monkeypatch.setattr(sc.urllib.request, "urlopen", _raise_404)

    with pytest.raises(SystemExit):
        sc._request("GET", "/tasks/does-not-exist")

    err = capsys.readouterr().err
    assert '"detail": "nope"' in err


# ─── #1233 review: `treat_as_missing` / `missing_if_detail_contains` ────────
#
# `poll_tasks._get_workflow` relies on these to read a 404 as "no custom
# workflow" WITHOUT also swallowing a "workspace not found" 404 (same status
# code, different meaning) into the same silent fallback.


def test_treat_as_missing_returns_none_with_no_detail_filter(monkeypatch):
    """Bare `treat_as_missing={code}` (no `missing_if_detail_contains`)
    still short-circuits to `None` on any body — the original, coarser
    behavior, kept for callers that don't need the narrower signal."""

    def _raise_404(*a, **kw):
        raise _FakeHTTPError(404, payload=b'{"detail": "anything at all"}')

    monkeypatch.setattr(sc.urllib.request, "urlopen", _raise_404)

    assert sc._request("GET", "/x", treat_as_missing=frozenset({404})) is None


def test_missing_if_detail_contains_matching_substring_returns_none(monkeypatch):
    def _raise_404(*a, **kw):
        raise _FakeHTTPError(
            404, payload=b'{"detail": "No custom workflow for task. Default applies."}'
        )

    monkeypatch.setattr(sc.urllib.request, "urlopen", _raise_404)

    result = sc._request(
        "GET",
        "/x",
        treat_as_missing=frozenset({404}),
        missing_if_detail_contains="No custom workflow",
    )
    assert result is None


def test_missing_if_detail_contains_non_matching_body_falls_through_and_exits(
    monkeypatch, capsys
):
    """The review's narrowing fix: a 404 with the SAME status code but a
    DIFFERENT reason ("Workspace not found", not "No custom workflow") must
    NOT be silently swallowed — it must reach the normal error path and
    exit, exactly like any other unrecognised failure. Also proves the body
    isn't read twice (which would raise, not just assert-fail) once the
    treat_as_missing branch falls through."""

    def _raise_404(*a, **kw):
        raise _FakeHTTPError(404, payload=b'{"detail": "Workspace not found"}')

    monkeypatch.setattr(sc.urllib.request, "urlopen", _raise_404)

    with pytest.raises(SystemExit) as exc:
        sc._request(
            "GET",
            "/x",
            treat_as_missing=frozenset({404}),
            missing_if_detail_contains="No custom workflow",
        )

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Workspace not found" in err


# ─── US-031.6: `_request(timeout=)` reaches urlopen (the inbox wait outlasts 30 s) ──


class _OkResponse:
    def __init__(self, raw: bytes = b"{}"):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._raw


def test_request_timeout_defaults_to_30_and_a_caller_value_reaches_urlopen(monkeypatch):
    seen = []

    def fake_urlopen(_req, timeout=None):
        seen.append(timeout)
        return _OkResponse()

    monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)

    sc._request("GET", "/x")
    sc._request("GET", "/y", timeout=35)

    assert seen == [30, 35]


# ---------------------------------------------------------------------------
# poll_tasks._ensure_summary — #1130 guard on the shared outcome path.
#
# poll_tasks imports `from spawnr_client import _request`, so importing it
# needs the same sys.path entry already inserted above.
# ---------------------------------------------------------------------------

import poll_tasks as pt  # noqa: E402


class TestEnsureSummary:
    """A missing summary on a required status is FATAL, not cosmetic:
    `_request` exits the process on 422, and `_work_task` runs outside
    `main()`'s `except SystemExit` — so the worker dies on its first finished
    task and strands it `in_progress`. These pin the guard that prevents it.
    """

    @pytest.mark.parametrize("status", ["done", "blocked", "canceled", "in_review"])
    def test_missing_summary_is_backfilled_for_every_required_status(self, status):
        out = pt._normalize_outcome({"status": status}, "T-1")
        assert out["summary"], f"{status} must never go out without a summary"
        assert out["status"] == status

    def test_the_old_documented_handler_contract_no_longer_422s(self):
        """`return {"status": "done"}` is exactly what this skill's README
        taught before #1220, so handlers in the wild still return it."""
        assert pt._normalize_outcome({"status": "done"}, "T-1")["summary"]

    def test_whitespace_only_summary_is_treated_as_missing(self):
        # _check_agent_summary_required rejects whitespace-only too.
        assert pt._normalize_outcome({"status": "done", "summary": "   "}, "T-1")[
            "summary"
        ].strip()

    def test_a_real_summary_is_preserved_verbatim(self):
        out = pt._normalize_outcome(
            {"status": "done", "summary": "Rebuilt the index."}, "T-1"
        )
        assert out["summary"] == "Rebuilt the index."

    def test_oversized_summary_is_truncated_to_the_server_cap(self):
        out = pt._normalize_outcome({"status": "done", "summary": "x" * 6000}, "T-1")
        assert len(out["summary"]) == 5000

    def test_non_required_status_is_passed_through_untouched(self):
        # in_progress needs no summary; don't invent one.
        assert pt._normalize_outcome({"status": "in_progress"}, "T-1") == {
            "status": "in_progress"
        }

    def test_input_is_not_mutated(self):
        original = {"status": "done"}
        pt._normalize_outcome(original, "T-1")
        assert original == {"status": "done"}

    # -- description must never reach the API -------------------------------
    #
    # `description` IS a legal UpdateTaskRequest field, so once `summary` is
    # backfilled the PATCH stops 422ing and starts SUCCEEDING — and the backend
    # $sets description wholesale. Backfilling without stripping would trade a
    # loud crash for permanent, silent destruction of the task's description.

    def test_legacy_description_is_folded_into_summary_and_stripped(self):
        """The exact pre-#1220 documented contract for a failure."""
        out = pt._normalize_outcome(
            {"status": "blocked", "description": "S3 credentials missing"}, "T-1"
        )
        assert "description" not in out
        assert out["summary"] == "S3 credentials missing"

    def test_description_is_stripped_even_on_a_non_required_status(self):
        # in_progress needs no summary, but description overwrites just as hard.
        out = pt._normalize_outcome(
            {"status": "in_progress", "description": "halfway"}, "T-1"
        )
        assert out == {"status": "in_progress"}

    def test_a_real_summary_wins_over_a_legacy_description(self):
        out = pt._normalize_outcome(
            {"status": "done", "summary": "Real summary.", "description": "clobber"},
            "T-1",
        )
        assert out["summary"] == "Real summary."
        assert "description" not in out

    def test_generic_fallback_warns_on_stderr(self, capsys):
        """Backfill yes, SILENT backfill no — a handler returning nothing at
        all otherwise has no way to learn it's non-compliant."""
        pt._normalize_outcome({"status": "done"}, "T-42")
        err = capsys.readouterr().err
        assert "T-42" in err and "summary" in err

    def test_no_warning_when_the_handler_supplied_something(self, capsys):
        pt._normalize_outcome({"status": "done", "summary": "Real."}, "T-1")
        pt._normalize_outcome({"status": "blocked", "description": "Real."}, "T-2")
        assert capsys.readouterr().err == ""

    def test_work_task_actually_routes_the_outcome_through_the_guard(self, monkeypatch):
        """Pins the CALL SITE, not just the helper.

        Without this, deleting `_ensure_summary(...)` from `_work_task`'s PATCH
        leaves every test above green while the process-killing 422 comes
        straight back — verified by mutation.
        """
        calls = []
        monkeypatch.setattr(pt, "_request", lambda m, p, b=None, **kw: calls.append(b))

        # The pre-#1220 documented contract: no summary.
        pt._work_task({"id": "T-1"}, lambda _t: {"status": "done"})

        outcome = calls[-1]
        assert outcome["status"] == "done"
        assert outcome.get("summary"), "outcome PATCH reached the API without a summary"


class TestPhasedTasks:
    """US-031.5: a task the agent holds in an agent-flow phase is not claimed again;
    the handler runs and ONE PATCH moves it on with the handoff the server requires."""

    FLOW = {
        "agent_flow": {
            "phases": [
                {"status_key": "planning", "role": "role:planner"},
                {"status_key": "coding", "role": "role:coder"},
                {"status_key": "testing", "role": "role:tester"},
            ]
        }
    }

    def _run(self, monkeypatch, task, handler, detail=None):
        calls = []

        def fake(method, path, body=None, **kw):
            calls.append((method, path, body))
            return (detail or self.FLOW) if method == "GET" else {}

        monkeypatch.setattr(pt, "_request", fake)
        pt._work_task(task, handler)
        return calls

    def test_a_held_phase_task_is_listed_whatever_its_status(self, monkeypatch):
        tasks = [
            {"id": "A", "status": "coding", "phase_role": "role:coder"},
            {"id": "B", "status": "coding"},
            {"id": "C", "status": "open"},
        ]
        monkeypatch.setattr(pt, "_request", lambda *a, **k: {"tasks": tasks})

        kept = pt._list_actionable_tasks(None)

        assert [t["id"] for t in kept] == ["A", "C"]

    def test_no_claim_walk_and_the_handler_status_and_handoff_go_in_one_patch(
        self, monkeypatch
    ):
        handoff = {
            "done": "Built it.",
            "not_done": "Docs.",
            "decisions": "SQLite.",
            "open_questions": "None.",
            "for_next": "role:tester",
        }
        task = {"id": "T-1", "status": "coding", "phase_role": "role:coder"}

        calls = self._run(
            monkeypatch, task, lambda _t: {"status": "testing", "handoff": handoff}
        )

        assert calls == [
            ("GET", "/tasks/T-1", None),
            ("PATCH", "/tasks/T-1", {"status": "testing", "handoff": handoff}),
        ]

    def test_no_status_means_the_next_phase_and_a_fallback_handoff(self, monkeypatch):
        task = {"id": "T-1", "status": "coding", "phase_role": "role:coder"}

        calls = self._run(monkeypatch, task, lambda _t: {"summary": "Did the work."})

        assert calls[0] == ("GET", "/tasks/T-1", None)
        method, path, body = calls[1]
        assert (method, path, body["status"]) == ("PATCH", "/tasks/T-1", "testing")
        assert body["handoff"] == {
            "done": "Did the work.",
            "not_done": "Not reported by the handler.",
            "decisions": "Not reported by the handler.",
            "open_questions": "Not reported by the handler.",
            "for_next": "role:tester",
        }

    def test_the_last_phase_goes_to_done(self, monkeypatch):
        task = {"id": "T-1", "status": "testing", "phase_role": "role:tester"}

        calls = self._run(monkeypatch, task, lambda _t: {})

        assert calls[1][2]["status"] == "done"

    def test_a_handler_that_raises_blocks_the_task_with_the_error_in_not_done(
        self, monkeypatch
    ):
        task = {"id": "T-1", "status": "coding", "phase_role": "role:coder"}

        def boom(_t):
            raise RuntimeError("no network")

        calls = self._run(monkeypatch, task, boom)

        assert len(calls) == 2  # the detail read, then one PATCH
        _m, _p, body = calls[1]
        assert body["status"] == "blocked"
        assert body["handoff"]["not_done"] == "Handler raised RuntimeError: no network"
        assert body["handoff"]["for_next"]  # never blank: the server refuses it
        assert all(v.strip() for v in body["handoff"].values())

    def test_the_handler_gets_the_task_detail_with_the_pinned_handoff(self, monkeypatch):
        """`/tasks/mine` rows carry `phase_role` but not `handoff`: the poller reads the
        task detail once, so a handler starts from the last phase's handoff."""
        pinned = {
            "done": "Parser written.",
            "not_done": "Error messages.",
            "decisions": "Kept the old grammar.",
            "open_questions": "None.",
            "for_next": "role:tester: run the edge cases.",
        }
        row = {"id": "T-1", "status": "testing", "phase_role": "role:tester"}
        seen = []

        def handler(t):
            seen.append(t)
            return {"status": "done", "handoff": pinned}

        calls = self._run(
            monkeypatch, row, handler, detail={**self.FLOW, "handoff": pinned}
        )

        assert seen[0]["handoff"] == pinned
        assert seen[0]["id"] == "T-1"  # the list row's fields survive the merge
        assert [c[:2] for c in calls] == [("GET", "/tasks/T-1"), ("PATCH", "/tasks/T-1")]

    def test_a_blocked_by_cap_refusal_on_the_handoff_is_not_retried(
        self, monkeypatch, capsys
    ):
        """A status change on a task Spawnr blocked at a cap is 409 `blocked_by_cap`
        (AgentTaskTracker#2437). It is sent once, reported, and the loop goes on."""
        methods = []
        refusal = json.dumps(
            {
                "detail": {
                    "code": "blocked_by_cap",
                    "details": {"cap": "exchanges", "question_id": "Q-1"},
                }
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            methods.append(req.get_method())
            if req.get_method() == "GET":
                return _OkResponse(json.dumps(self.FLOW).encode())
            raise _FakeHTTPError(409, refusal)

        monkeypatch.setattr(sc.urllib.request, "urlopen", fake_urlopen)
        task = {"id": "T-1", "status": "coding", "phase_role": "role:coder"}

        pt._work_task_safely(task, lambda _t: {"status": "testing"})  # must not raise

        assert methods.count("PATCH") == 1
        assert "blocked_by_cap" in capsys.readouterr().err


class TestInboxWait:
    """US-031.6: the poller waits on `GET /agents/{id}/inbox/wait` instead of sleeping.
    An offered phase is claimed, a task assigned to the agent is left to the next
    `/tasks/mine` listing, and comment and answer events are printed for the operator."""

    ME = "agent-me"
    CLAIM = {"assignee_id": "agent-me", "assignee_type": "agent", "status": "in_progress"}
    OFFER_UPDATED = {
        "id": "10-0",
        "type": "task.updated",
        "data": {
            "task_id": "T-9",
            "changes": {
                "phase_role": {"old": "role:coder", "new": "role:tester"},
                "assignee": {"old": "agent-a", "new": None},
            },
        },
    }
    OFFER_PHASE_ONLY = {
        "id": "11-0",
        "type": "task.updated",
        "data": {
            "task_id": "T-9",
            "changes": {"phase_role": {"old": None, "new": "role:tester"}},
        },
    }
    OFFER_CREATED = {
        "id": "12-0",
        "type": "task.created",
        "data": {"task_id": "T-9", "title": "Proposal", "status": "open"},
    }
    ASSIGNED = {
        "id": "13-0",
        "type": "task.updated",
        "data": {
            "task_id": "T-9",
            "changes": {"assignee": {"old": None, "new": "Coder Bot"}},
        },
    }
    COMMENT = {
        "id": "14-0",
        "type": "task.comment_added",
        "data": {"task_id": "T-9", "comment_id": "C-1", "body": "Use SQLite."},
    }
    ANSWER = {
        "id": "15-0",
        "type": "question.answered",
        "data": {"question_id": "Q-1", "agent_id": "agent-me", "answer": "Yes."},
    }
    # a task created already assigned to this agent: the server adds `changes.assignee`
    CREATED_FOR_ME = {
        "id": "16-0",
        "type": "task.created",
        "data": {
            "task_id": "T-9",
            "status": "open",
            "changes": {"assignee": {"old": None, "new": "Coder Bot"}},
        },
    }

    def _wait(self, monkeypatch, responses, cursor=None, wait_s=25):
        calls = []
        script = list(responses)

        def fake(method, path, body=None, **kw):
            calls.append((method, path, body, kw))
            return script.pop(0) if script else {}

        monkeypatch.setattr(pt, "_request", fake)
        return calls, pt._wait_and_claim(self.ME, cursor, wait_s)

    def test_the_first_wait_sends_no_since_and_reads_for_longer_than_it_waits(
        self, monkeypatch
    ):
        calls, _ = self._wait(monkeypatch, [{"events": [], "cursor": "5-0"}])

        method, path, body, kw = calls[0]
        assert (method, path, body) == (
            "GET",
            f"/agents/{self.ME}/inbox/wait?timeout=25",
            None,
        )
        assert kw["timeout"] == 35  # the socket outlasts the server's wait

    def test_the_cursor_the_server_gave_is_returned_and_sent_back_as_since(
        self, monkeypatch
    ):
        _, cursor = self._wait(monkeypatch, [{"events": [], "cursor": "5-0"}])
        assert cursor == "5-0"

        calls, _ = self._wait(monkeypatch, [{"events": [], "cursor": "6-0"}], cursor)

        assert calls[0][1] == f"/agents/{self.ME}/inbox/wait?timeout=25&since=5-0"

    def test_a_response_without_a_cursor_keeps_the_one_we_had(self, monkeypatch):
        _, cursor = self._wait(monkeypatch, [{"events": []}], cursor="5-0")
        assert cursor == "5-0"

    @pytest.mark.parametrize("offer", [OFFER_UPDATED, OFFER_PHASE_ONLY, OFFER_CREATED])
    def test_an_offered_phase_is_claimed_with_the_claim_body_and_a_409_is_not_fatal(
        self, monkeypatch, offer
    ):
        calls, _ = self._wait(monkeypatch, [{"events": [offer], "cursor": "20-0"}])

        assert len(calls) == 2
        method, path, body, kw = calls[1]
        assert (method, path, body) == ("PATCH", "/tasks/T-9", self.CLAIM)
        assert kw["treat_as_missing"] == frozenset({409})

    def test_a_409_on_one_claim_does_not_stop_the_next_event(self, monkeypatch):
        second = dict(self.OFFER_CREATED, data={"task_id": "T-10"})
        # the 409 is the PATCH answering None (`treat_as_missing`), the next gets {}
        calls, cursor = self._wait(
            monkeypatch,
            [{"events": [self.OFFER_UPDATED, second], "cursor": "20-0"}, None],
        )

        assert [c[1] for c in calls[1:]] == ["/tasks/T-9", "/tasks/T-10"]
        assert cursor == "20-0"

    def test_a_task_assigned_to_the_agent_is_left_to_the_next_listing(self, monkeypatch):
        calls, cursor = self._wait(
            monkeypatch, [{"events": [self.ASSIGNED], "cursor": "21-0"}]
        )

        assert len(calls) == 1  # only the wait: no PATCH, `/tasks/mine` finds it
        assert cursor == "21-0"

    def test_a_task_created_for_the_agent_is_not_claimed_again(self, monkeypatch):
        calls, cursor = self._wait(
            monkeypatch, [{"events": [self.CREATED_FOR_ME], "cursor": "24-0"}]
        )

        assert len(calls) == 1  # only the wait: no PATCH, `/tasks/mine` finds it
        assert calls[0][0] == "GET"
        assert cursor == "24-0"

    def test_comment_and_answer_events_are_printed_and_never_patched(
        self, monkeypatch, capsys
    ):
        calls, _ = self._wait(
            monkeypatch,
            [{"events": [self.COMMENT, self.ANSWER], "cursor": "22-0"}],
        )

        assert len(calls) == 1
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert lines == [
            {"event": "task.comment_added", "data": self.COMMENT["data"]},
            {"event": "question.answered", "data": self.ANSWER["data"]},
        ]

    def test_other_event_types_are_ignored(self, monkeypatch, capsys):
        status_changed = {"id": "23-0", "type": "task.status_changed", "data": {}}

        calls, _ = self._wait(
            monkeypatch, [{"events": [status_changed], "cursor": "23-0"}]
        )

        assert len(calls) == 1
        assert capsys.readouterr().out == ""

    def test_a_hard_failure_on_one_event_does_not_lose_the_rest_or_the_cursor(
        self, monkeypatch, capsys
    ):
        second = dict(self.OFFER_CREATED, data={"task_id": "T-10"})
        calls = []
        script = [{"events": [self.OFFER_UPDATED, second], "cursor": "20-0"}]

        def fake(method, path, body=None, **kw):
            calls.append(path)
            if script:
                return script.pop(0)
            if path == "/tasks/T-9":
                raise SystemExit(1)  # `_request` exits on a 403/5xx
            return {}

        monkeypatch.setattr(pt, "_request", fake)

        cursor = pt._wait_and_claim(self.ME, None, 25)

        assert calls[1:] == ["/tasks/T-9", "/tasks/T-10"]
        assert cursor == "20-0"
        assert "T-9" in capsys.readouterr().err

    def _run_main(self, monkeypatch, seconds, failure, flag="--timeout"):
        """Run `main()` for two rounds: the first wait raises `failure`, the second stops it."""
        waits, sleeps, ids = [], [], []

        def fake_wait(me, cursor, wait_s):
            waits.append((me, cursor, wait_s))
            if len(waits) == 1:
                raise failure
            pt._stopping = True
            return "9-0"

        monkeypatch.setattr(pt, "_stopping", False)
        monkeypatch.setattr(pt, "_my_agent_id", lambda: ids.append(1) or self.ME)
        monkeypatch.setattr(pt, "_list_actionable_tasks", lambda *a, **k: [])
        monkeypatch.setattr(pt, "_wait_and_claim", fake_wait)
        monkeypatch.setattr(pt.time, "sleep", sleeps.append)
        monkeypatch.setattr(pt.signal, "signal", lambda *a: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["poll_tasks", "--handler", "os.path:basename", flag, str(seconds)],
        )
        pt.main()
        return waits, sleeps, ids

    @pytest.mark.parametrize("seconds, waited", [(0, 1), (25, 25), (120, 60)])
    def test_main_waits_timeout_seconds_clamped_and_retries_a_failed_wait(
        self, monkeypatch, seconds, waited
    ):
        # SystemExit is what `_request` raises on e.g. 403 agent_paused: keep trying
        waits, sleeps, ids = self._run_main(monkeypatch, seconds, SystemExit(1))

        assert waits == [(self.ME, None, waited), (self.ME, None, waited)]
        assert sleeps == [waited * 2]
        assert len(ids) == 1  # the agent id is resolved once, not per cycle

    def test_the_old_interval_flag_still_sets_the_wait(self, monkeypatch):
        """The in-app OpenClaw docs card still sends `--interval`: it is an alias."""
        waits, _sleeps, _ids = self._run_main(
            monkeypatch, 25, SystemExit(1), flag="--interval"
        )

        assert waits[0][2] == 25

    @pytest.mark.parametrize(
        "failure",
        [
            socket.timeout("timed out"),
            # urllib wraps only `request()` errors in URLError, so a backend that drops
            # the held long-poll (a restart or deploy) escapes `_request` as these:
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            ConnectionResetError(),
        ],
        ids=["socket-timeout", "remote-disconnected", "connection-reset"],
    )
    def test_a_stalled_or_dropped_wait_is_retried_not_fatal(self, monkeypatch, failure):
        waits, sleeps, _ids = self._run_main(monkeypatch, 30, failure)

        assert len(waits) == 2
        assert sleeps == [60]

    def test_nothing_is_claimed_after_a_stop_but_comments_still_print(
        self, monkeypatch, capsys
    ):
        """A SIGTERM that lands while the wait holds the socket must not hand the process
        a task it is about to abandon; the cursor still moves past the events."""
        monkeypatch.setattr(pt, "_stopping", False)
        calls = []

        def fake(method, path, body=None, **kw):
            calls.append(method)
            monkeypatch.setattr(pt, "_stopping", True)  # the signal arrived mid-wait
            return {"events": [self.OFFER_UPDATED, self.COMMENT], "cursor": "20-0"}

        monkeypatch.setattr(pt, "_request", fake)

        cursor = pt._wait_and_claim(self.ME, None, 25)

        assert calls == ["GET"]  # no claim PATCH
        assert cursor == "20-0"
        assert json.loads(capsys.readouterr().out)["event"] == "task.comment_added"
