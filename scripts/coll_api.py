"""
FastAPI web application for CollMgm.

LAN-hosted, browser-based interface. Serves on 0.0.0.0:8100.
Run via:  run_server.bat
       or uvicorn scripts.coll_api:app --host 0.0.0.0 --port 8100 --reload
"""

import csv
import io
import json
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from coll_orchestrate import (
    StageError, ValidationError,
    prepare_submit_review, apply_submit_approval,
    ActiveReportState, check_active_beat_report, generate_collection_list, apply_start_approval,
    owning_salesman,
    set_start_verification, is_start_verification_complete,
    set_submit_verification, is_submit_verification_complete,
    apply_correction_request, _find_active_report_for_bill,
    compute_payment_dates, record_submit_payments, validate_payment,
    validate_payment_type, payment_type_totals, parse_return_items,
    validate_staged_report, format_submit_validation_errors,
    post_confirmed_report, return_post_stage,
    amend_voucher,
    raise_amendment_request, resolve_amendment_request,
    resolve_check,
    ADDV_FLAG_KINDS,
    addv_batch_status, addv_vouchers_for_salesman,
    clear_addv_review, raise_addv_flag, resolve_addv_flag, reject_addv_batch,
)
from coll_store import (
    STAGING_DIR,
    ASSIGNABLE_ROLES,
    RESET_WRONG_ANSWER,
    AmendmentConflict,
    CorrectionConflict,
    archive_files,
    _load_installments,
    _load_pending_start_reports,
    _load_pending_submit_reports,
    bill_no_sort_key,
    build_print_collection_html,
    cancel_staging_report,
    change_own_password,
    create_beat,
    create_user,
    delete_beat,
    delete_user,
    ensure_db,
    ensure_staging_dir,
    has_any_users,
    insert_correction,
    list_staging_reports,
    load_addv_batches,
    load_addv_staged_bill_nos,
    load_all_existing_bill_nos,
    load_amendment,
    load_amendments,
    load_amendment_request,
    load_amendment_requests,
    load_beats_raw,
    load_checks,
    check_summary_counts,
    load_correction,
    load_corrections,
    load_permissions,
    load_report_json,
    load_user,
    load_users_admin,
    open_amendment_requests_for_bills,
    open_corrections_for_bills,
    parse_decimal,
    read_finalize_checkpoint,
    get_distributor_secret_question,
    get_secret_question_for,
    register_first_distributor,
    reset_password_with_secret_answer,
    reset_user_password,
    set_secret_question,
    sanitize_filename_component,
    save_report_json,
    update_beat_salesman,
    update_user_role,
    verify_user,
    write_new_installments,
    write_new_vouchers,
)
from coll_data import (
    NUMOF_TOP_AGED_VOUCHERS,
    NUMOF_TOP_AMOUNT_VOUCHERS,
    _load_confirmed_start_reports,
    _load_submit_confirmed_reports,
    _load_vouchers_by_criterion,
    load_active_beat_statuses,
    load_beats,
    load_beats_pending_summary,
    load_salesmen,
    load_voucher_amounts,
    query_pending_by_age,
    query_pending_by_amount,
    query_pending_by_beat,
    query_pending_by_salesman,
    search_voucher,
    validate_addv_batch,
)

ROOT_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="CollMgm")
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

# In-memory sessions: token -> User namedtuple
_sessions: dict = {}
_SESSION_COOKIE = "collmgm_session"

# Serializes read-modify-write of a staging report between the verify endpoint
# and the approve-start action handler. Process-local only (like _sessions, a
# single-process deployment is assumed); a concurrent CLI process racing these
# writes is narrowed — not eliminated — by re-loading the report after the
# lock is taken.
_verify_lock = threading.Lock()

# Forgot-password lockout: every 5 wrong secret answers lock the whole reset
# flow, and each successive lock is longer (15 min, 1 h, then 24 h) until a
# successful reset — so slow-drip guessing is capped at 5 tries per lock
# instead of ~480 a day. Process-local like _sessions (a service restart
# clears it, which an attacker on the LAN cannot trigger). Global, not
# per-IP: there is one distributor, and LAN clients often share an address.
_RESET_MAX_FAILURES = 5
_RESET_LOCK_TIERS = (15 * 60, 60 * 60, 24 * 60 * 60)  # seconds; last tier repeats
_reset_state = {"failures": 0, "locks": 0, "locked_until": 0.0}
_reset_state_lock = threading.Lock()


def _reset_locked_minutes():
    """Whole minutes (>=1) left on the reset lockout, or 0 when not locked."""
    with _reset_state_lock:
        remaining = _reset_state["locked_until"] - time.time()
        if remaining <= 0:
            return 0
        return int(remaining // 60) + 1


def _reset_record_failure():
    with _reset_state_lock:
        _reset_state["failures"] += 1
        if _reset_state["failures"] >= _RESET_MAX_FAILURES:
            tier = min(_reset_state["locks"], len(_RESET_LOCK_TIERS) - 1)
            _reset_state["locks"] += 1
            _reset_state["failures"] = 0
            _reset_state["locked_until"] = time.time() + _RESET_LOCK_TIERS[tier]


def _reset_clear_failures():
    with _reset_state_lock:
        _reset_state.update(failures=0, locks=0, locked_until=0.0)


def _format_wait(minutes):
    if minutes >= 120:
        return f"{(minutes + 59) // 60} hours"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_user(request: Request):
    token = request.cookies.get(_SESSION_COOKIE)
    return _sessions.get(token) if token else None


def _set_session(response, user):
    token = secrets.token_urlsafe(32)
    _sessions[token] = user
    response.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax")


def _clear_session(request: Request, response):
    token = request.cookies.get(_SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(_SESSION_COOKIE)


def _r(url: str, code: int = 303):
    return RedirectResponse(url, status_code=code)


def _redirect_ok(url: str, msg: str):
    """Redirect to url with a one-shot success message for base.html's flash banner."""
    sep = "&" if "?" in url else "?"
    return _r(f"{url}{sep}ok={quote(msg)}")


def _tmpl(name: str, request: Request, **ctx):
    """Render a template, auto-injecting `nav_perms` (the rendering user's
    permission set) whenever a `user` is in context and the caller hasn't
    already supplied one — lets base.html's nav dropdowns gate themselves
    without every existing route having to pass this explicitly."""
    user = ctx.get("user")
    if user is not None and "nav_perms" not in ctx:
        try:
            ctx["nav_perms"] = load_permissions().get(user.role, frozenset())
        except FileNotFoundError:
            ctx["nav_perms"] = frozenset()
    return templates.TemplateResponse(request=request, name=name, context=ctx)


def _enrich_vouchers(vouchers):
    """Attach master voucher amount and derived total-paid for display.

    Render-time only — never persisted, so the staged-report schema (and its
    tamper validation) is unaffected. Bills missing from master show blanks.
    """
    amounts = load_voucher_amounts([v["bill_no"] for v in vouchers])
    for v in vouchers:
        amount = amounts.get(v["bill_no"].strip(), "")
        v["amount"] = amount
        try:
            v["total_paid"] = str(Decimal(amount) - Decimal(v["balance"]))
        except (InvalidOperation, ValueError, KeyError):
            v["total_paid"] = ""
    return vouchers


_FORCED_CHANGE_ALLOWED_PATHS = {"/profile", "/profile/change-password", "/logout"}


def _require(request: Request, permission: str = None):
    """Return (user, None) if authorised; (None, redirect/error response) otherwise.

    A user with a pending forced password change is redirected to /profile
    for every other route — the single chokepoint all protected routes
    already call, so nothing can route around the forced change.
    """
    user = _get_user(request)
    if not user:
        return None, _r("/login")
    if user.must_change_password and request.url.path not in _FORCED_CHANGE_ALLOWED_PATHS:
        return user, _r("/profile?forced=1")
    if permission:
        try:
            perms = load_permissions()
        except FileNotFoundError:
            return user, _tmpl("error.html", request, user=user,
                               message="permissions.csv not found — cannot check access.")
        if permission not in perms.get(user.role, frozenset()):
            return user, _tmpl("error.html", request, user=user,
                               message="You don't have permission for this action.")
    return user, None


def _report_label(data: dict) -> str:
    sel_type = data.get("selection_type", "beat")
    sel = data.get("selection", [])
    if sel_type == "beat_salesman" and len(sel) >= 2:
        return f"{sel[0]} / {sel[1]}"
    return ", ".join(sel)


def _flag_salesman_mismatches(vouchers, assigned_salesman):
    """Mark each voucher whose own `salesman` differs from the beat's
    assigned salesman — render-only, never persisted, mirrors the
    correction_open/correction_applied pattern used elsewhere. Purely
    informational: nothing is gated or auto-raised here, a human reviews and
    raises an Amendment Request themselves (via the voucher lookup screen) if
    warranted. Always a no-op for a "beat_salesman" report, since every
    voucher's salesman equals `assigned_salesman` there by construction."""
    for v in vouchers:
        v["salesman_mismatch"] = v.get("salesman", "") != assigned_salesman


# Staging report stems are built from sanitize_filename_component output, so a
# legitimate stem never needs anything outside this alphabet. Rejecting the
# rest blocks path traversal: '/' can't appear in a path segment, but an
# URL-encoded backslash can — and pathlib treats it as a separator on Windows.
_STEM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _load_staging_report(stem: str):
    """Resolve a client-supplied report stem to (json_path, report_data).

    Returns (None, None) for a malformed stem, a missing file, or unreadable/
    non-dict JSON — callers render the same 'Report not found.' either way.
    """
    if not stem or not _STEM_RE.match(stem):
        return None, None
    path = STAGING_DIR / f"{stem}.json"
    if not path.exists():
        return None, None
    try:
        data = load_report_json(path)
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return path, data


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    ensure_db()


# ---------------------------------------------------------------------------
# Root / Login / Logout
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if _get_user(request):
        return _r("/menu")
    return _r("/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _get_user(request):
        return _r("/menu")
    return _tmpl("login.html", request, **_login_flags())


def _login_flags():
    """Which first-run/recovery links the login screen offers. The forgot link
    only appears once setup is done AND the distributor has a secret question
    on file, so it never leads to a dead end."""
    registered = has_any_users()
    return {"show_register": not registered,
            "show_forgot": registered and get_distributor_secret_question() is not None}


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request,
               username: str = Form(default=""),
               password: str = Form(default="")):
    user = verify_user(username.strip(), password)
    if not user:
        return _tmpl("login.html", request, error="Invalid username or password.",
                     **_login_flags())
    resp = _r("/profile?forced=1" if user.must_change_password else "/menu")
    _set_session(resp, user)
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = _r("/login")
    _clear_session(request, resp)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if _get_user(request):
        return _r("/menu")
    if has_any_users():
        return _r("/login")
    return _tmpl("register.html", request)


@app.post("/register", response_class=HTMLResponse)
def register_post(request: Request,
                  name: str = Form(default=""),
                  password: str = Form(default=""),
                  confirm_password: str = Form(default=""),
                  secret_question: str = Form(default=""),
                  secret_answer: str = Form(default="")):
    if has_any_users():
        return _r("/login")
    try:
        # Both are required here (empty strings are validated, not skipped):
        # the question is what makes "Forgot password?" possible later.
        register_first_distributor(name.strip(), password, confirm_password,
                                   secret_question=secret_question, secret_answer=secret_answer)
    except ValueError as e:
        return _tmpl("register.html", request, error=str(e), submitted_name=name,
                     submitted_question=secret_question)
    return _redirect_ok("/login", f"Distributor account '{name.strip()}' created — sign in to continue.")


# ---------------------------------------------------------------------------
# Forgot password (distributor only, secret question)
# ---------------------------------------------------------------------------

_RESET_UNAVAILABLE_MSG = ("Password reset isn't available for this account. "
                          "Ask the distributor to reset it in Manage Users.")


def _forgot_locked_page(request, minutes):
    return _tmpl("forgot_password.html", request, step=1,
                 error=f"Too many incorrect attempts. Try again in {_format_wait(minutes)}.")


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    if _get_user(request):
        return _r("/menu")
    if get_distributor_secret_question() is None:
        return _r("/login")
    return _tmpl("forgot_password.html", request, step=1)


@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_lookup(request: Request, username: str = Form(default="")):
    if _get_user(request):
        return _r("/menu")
    minutes = _reset_locked_minutes()
    if minutes:
        return _forgot_locked_page(request, minutes)
    username = username.strip()
    question = get_secret_question_for(username)
    if question is None:
        # One message for "no such user", "not the distributor" and "no
        # question set". Note the flip side is unavoidable: a username that DOES
        # get the question page is the distributor, so anyone on the LAN can
        # learn the distributor's username and question (not the answer).
        return _tmpl("forgot_password.html", request, step=1, error=_RESET_UNAVAILABLE_MSG,
                     submitted_name=username)
    return _tmpl("forgot_password.html", request, step=2, username=username, question=question)


@app.post("/forgot-password/reset", response_class=HTMLResponse)
def forgot_password_reset(request: Request,
                          username: str = Form(default=""),
                          secret_answer: str = Form(default=""),
                          new_password: str = Form(default=""),
                          confirm_password: str = Form(default="")):
    if _get_user(request):
        return _r("/menu")
    minutes = _reset_locked_minutes()
    if minutes:
        return _forgot_locked_page(request, minutes)
    username = username.strip()
    question = get_secret_question_for(username)
    if question is None:
        return _tmpl("forgot_password.html", request, step=1, error=_RESET_UNAVAILABLE_MSG,
                     submitted_name=username)

    def step2_error(msg):
        return _tmpl("forgot_password.html", request, step=2, username=username,
                     question=question, error=msg)

    # The store checks the answer before the new password, so a wrong answer
    # can never skip the attempt counter by also being a bad password.
    try:
        reset_password_with_secret_answer(username, secret_answer, new_password, confirm_password)
    except ValueError as e:
        if str(e) == RESET_WRONG_ANSWER:
            _reset_record_failure()
            minutes = _reset_locked_minutes()
            if minutes:
                return _forgot_locked_page(request, minutes)
            return step2_error("That answer is incorrect.")
        return step2_error(str(e).capitalize() + ".")
    _reset_clear_failures()
    for token in [t for t, u in list(_sessions.items()) if u.name == username]:
        _sessions.pop(token, None)
    return _redirect_ok("/login", "Password reset — sign in with your new password.")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

@app.get("/menu", response_class=HTMLResponse)
def menu(request: Request):
    user, err = _require(request)
    if err:
        return err
    try:
        perms = load_permissions()
    except FileNotFoundError:
        perms = {}
    role_perms = perms.get(user.role, frozenset())
    check_summary = check_summary_counts() if "view_checks" in role_perms else None
    return _tmpl("menu.html", request, user=user, perms=role_perms,
                 check_summary=check_summary)


# ---------------------------------------------------------------------------
# Generate Collection List  (coll-start)
# ---------------------------------------------------------------------------

def _render_start_beat_form(request, user, error=None, selected_beat=""):
    """Render the Generate Collection List picker, optionally with an inline
    error and the user's prior beat choice preserved (used both for the
    normal GET and for recoverable validation failures on generate)."""
    try:
        beats = load_beats(user)
        summary = load_beats_pending_summary(user)
        active = load_active_beat_statuses()
        beats_map = {b["name"]: b["salesman"] for b in load_beats_raw()}
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    # Beats already locked by an in-flight report can't be generated again
    # (see the beat-lock rule) — push them to the bottom and disable them
    # in the template instead of listing them alongside selectable beats.
    beats = sorted(beats, key=lambda b: (b in active, b))
    return _tmpl("coll/start_beat.html", request, user=user,
                 beats=beats, summary=summary, active=active, beats_map=beats_map,
                 error=error, selected_beat=selected_beat)


@app.get("/coll/start", response_class=HTMLResponse)
def coll_start(request: Request):
    user, err = _require(request, "coll_start")
    if err:
        return err
    return _render_start_beat_form(request, user)


def _generate_collection_list_response(request, user, beat):
    """Create the staging report and render the Keep/Cancel preview.

    Generates a beat-wide combined list — every pending voucher for the
    beat, regardless of which salesman is recorded on the individual
    voucher — since the web app no longer picks a salesman at generation
    time. A voucher whose own salesman differs from the beat's assigned
    salesman is not auto-corrected or auto-raised as an Amendment Request;
    it's flagged (see _flag_salesman_mismatches) for a human to review and
    raise one themselves if warranted.
    """
    selection_type = "beat"
    selection_values = [beat]

    state, _existing_path, _existing_data = check_active_beat_report(selection_type, selection_values)
    if state != ActiveReportState.NONE:
        return _render_start_beat_form(
            request, user,
            error=f"An active collection list already exists for beat '{beat}'. "
                   "Complete or cancel it before starting a new one.",
            selected_beat=beat)

    vouchers = _load_vouchers_by_criterion(selection_type, selection_values, user)
    if not vouchers:
        return _render_start_beat_form(
            request, user,
            error=f"No pending vouchers for beat '{beat}'.",
            selected_beat=beat)

    beats_map = {b["name"]: b["salesman"] for b in load_beats_raw()}
    assigned_salesman = beats_map.get(beat, "")

    outcome = generate_collection_list(selection_type, selection_values, vouchers)
    if not outcome.ok:
        if outcome.reason == "lock_conflict":
            return _render_start_beat_form(
                request, user,
                error=f"Beat '{beat}' is currently locked. Please retry later.",
                selected_beat=beat)
        return _tmpl("error.html", request, user=user, message=f"Failed to create report: {outcome.error}")

    total = sum(Decimal(v["balance"]) for v in vouchers)
    enriched = _enrich_vouchers(vouchers)
    _flag_salesman_mismatches(enriched, assigned_salesman)
    return _tmpl("coll/start_preview.html", request, user=user,
                 beat=beat, salesman=assigned_salesman, vouchers=enriched,
                 report_stem=outcome.json_path.stem, total_balance=total)


@app.post("/coll/start/generate", response_class=HTMLResponse)
def coll_start_generate(request: Request, beat: str = Form(default="")):
    user, err = _require(request, "coll_start")
    if err:
        return err
    beat = beat.strip()
    if not beat:
        return _render_start_beat_form(
            request, user, error="Select a beat.", selected_beat=beat)

    if user.role == "salesman" and beat not in load_beats(user):
        return _tmpl("error.html", request, user=user,
                     message="You are not assigned to that beat.")

    return _generate_collection_list_response(request, user, beat)


@app.post("/coll/start/confirm", response_class=HTMLResponse)
def coll_start_confirm(request: Request,
                        action: str = Form(default="keep"),
                        report_stem: str = Form(default="")):
    user, err = _require(request, "coll_start")
    if err:
        return err
    if action == "cancel":
        json_path, data = _load_staging_report(report_stem)
        if json_path is None:
            return _tmpl("error.html", request, user=user, message="Report not found.")
        sel = data.get("selection", [])
        sel_type = data.get("selection_type", "beat_salesman")
        if user.role == "salesman" and owning_salesman(sel_type, sel) != user.name:
            return _tmpl("error.html", request, user=user, message="Report not found.")
        if data.get("stages", {}).get("start") != "new":
            return _tmpl("error.html", request, user=user,
                         message="This collection list has already been approved — "
                                 "it can no longer be cancelled here.")
        # Beat comes from the report itself, not the form — a forged beat value
        # must not release another beat's lock.
        cancel_staging_report(json_path, sel[0] if sel else None)
        return _redirect_ok("/menu", "Collection list cancelled.")
    return _redirect_ok("/menu", "Collection list saved — awaiting supervisor approval.")


# ---------------------------------------------------------------------------
# Approve Collection List  (coll-approve-start)
# ---------------------------------------------------------------------------

@app.get("/coll/approve-start", response_class=HTMLResponse)
def coll_approve_start(request: Request):
    user, err = _require(request, "coll_approve_start")
    if err:
        return err
    pending = _load_pending_start_reports()
    reports = [{"stem": p.stem, "label": _report_label(d), "data": d} for p, d in pending]
    return _tmpl("coll/approve_start.html", request, user=user, reports=reports)


def _render_start_review(request, user, stem, data, error=None):
    """Render the Approve Collection List review screen.

    Shared by the GET route and the approve action's gate-failure path so a
    blocked approve keeps the supervisor on the review with an error banner
    instead of dead-ending on error.html. The per-voucher `verified` and
    correction flags are render-time only, like _enrich_vouchers' fields —
    never persisted.
    """
    sel = data.get("selection", [])
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    total = sum(parse_decimal(v.get("balance")) for v in vouchers)
    marked = set(data.get("verification", {}).get("bill_nos", []))
    bill_nos = [v["bill_no"] for v in vouchers]
    open_map = open_corrections_for_bills(bill_nos)
    applied_bills = {c["bill_no"] for c in load_corrections(statuses=["applied"])
                     if c["bill_no"] in set(bill_nos)}
    for v in vouchers:
        v["verified"] = v["bill_no"] in marked
        v["correction_open"] = v["bill_no"] in open_map
        v["correction_applied"] = v["bill_no"] in applied_bills
    open_count = sum(len(reqs) for reqs in open_map.values())
    salesman = owning_salesman(data.get("selection_type", "beat_salesman"), sel)
    _flag_salesman_mismatches(vouchers, salesman)
    return _tmpl("coll/approve_start_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=salesman,
                 total_balance=total,
                 verified_count=sum(1 for v in vouchers if v["verified"]),
                 count_verified=bool(data.get("verification", {}).get("count")),
                 all_verified=is_start_verification_complete(data) and not open_count,
                 open_corrections=open_count,
                 error=error)


@app.get("/coll/approve-start/{stem}", response_class=HTMLResponse)
def coll_approve_start_review(request: Request, stem: str):
    user, err = _require(request, "coll_approve_start")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    return _render_start_review(request, user, stem, data)


@app.post("/coll/approve-start/{stem}/verify")
def coll_approve_start_verify(request: Request, stem: str,
                              bill_no: str = Form(default=""),
                              verified: str = Form(default=""),
                              count: str = Form(default="")):
    """Persist one verification toggle (a voucher checkbox or the count box).

    Called by page-script fetch, so responses are JSON with real status codes
    (_require's HTML error pages would come back 200 and be indistinguishable
    from success to the client script).
    """
    user = _get_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    try:
        perms = load_permissions()
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    if "coll_approve_start" not in perms.get(user.role, frozenset()):
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    bill_no = bill_no.strip()
    if bool(bill_no) == bool(count):  # exactly one of the two per request
        return JSONResponse({"ok": False, "error": "params"}, status_code=400)
    # A voucher with an open correction cannot be verified — its numbers are
    # disputed until the distributor applies or rejects the request.
    if bill_no and open_corrections_for_bills([bill_no]):
        return JSONResponse({"ok": False, "error": "correction"}, status_code=409)
    with _verify_lock:
        json_path, data = _load_staging_report(stem)  # fresh copy under the lock
        if json_path is None:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        try:
            if bill_no:
                set_start_verification(json_path, data, bill_no=bill_no,
                                       verified=(verified == "1"))
            else:
                set_start_verification(json_path, data,
                                       count_verified=(count == "1"))
        except StageError:
            return JSONResponse({"ok": False, "error": "stage"}, status_code=409)
        except ValueError:
            return JSONResponse({"ok": False, "error": "unknown_bill"}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/coll/approve-start/{stem}", response_class=HTMLResponse)
def coll_approve_start_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "coll_approve_start")
    if err:
        return err
    if action not in ("approve", "return", "cancel"):
        return _r("/coll/approve-start")
    with _verify_lock:
        json_path, data = _load_staging_report(stem)
        if json_path is None:
            return _tmpl("error.html", request, user=user, message="Report not found.")
        # Web-only hard gate: physical-voucher verification must be complete
        # and no correction request may be pending before approval. The CLI
        # approve flow (apply_start_approval) is deliberately not gated —
        # checkboxes are a screen interaction.
        if action == "approve":
            open_map = open_corrections_for_bills(
                [v.get("bill_no") for v in data.get("vouchers", [])
                 if isinstance(v, dict)])
            if open_map:
                n = sum(len(reqs) for reqs in open_map.values())
                return _render_start_review(
                    request, user, stem, data,
                    error=f"Cannot approve — {n} correction request"
                          f"{'s are' if n != 1 else ' is'} awaiting the distributor.")
            if not is_start_verification_complete(data):
                return _render_start_review(
                    request, user, stem, data,
                    error="Cannot approve — verify every voucher and the voucher "
                          "count against the physical bundle first.")
        try:
            apply_start_approval(json_path, data, action)
        except StageError as e:
            return _tmpl("error.html", request, user=user, message=str(e))

    if action in ("return", "cancel"):
        msg = ("Collection list returned — salesman must regenerate." if action == "return"
               else "Collection list cancelled.")
        return _redirect_ok("/coll/approve-start", msg)
    return _redirect_ok("/coll/approve-start", "Collection list approved.")


# ---------------------------------------------------------------------------
# Submit Collections  (coll-submit)
# ---------------------------------------------------------------------------

@app.get("/coll/submit", response_class=HTMLResponse)
def coll_submit(request: Request):
    user, err = _require(request, "coll_submit")
    if err:
        return err
    all_confirmed = _load_confirmed_start_reports()
    if user.role == "salesman":
        confirmed = [(p, d) for p, d in all_confirmed
                     if owning_salesman(d.get("selection_type", "beat_salesman"),
                                        d.get("selection", [])) == user.name]
    else:
        confirmed = all_confirmed
    reports = [{"stem": p.stem, "label": _report_label(d), "data": d} for p, d in confirmed]
    return _tmpl("coll/submit.html", request, user=user, reports=reports)


@app.get("/coll/submit/{stem}", response_class=HTMLResponse)
def coll_submit_edit(request: Request, stem: str):
    user, err = _require(request, "coll_submit")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    sel = data.get("selection", [])
    sel_type = data.get("selection_type", "beat_salesman")
    salesman = owning_salesman(sel_type, sel)
    if user.role == "salesman" and salesman != user.name:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    stages = data.get("stages", {})
    if stages.get("submit") in ("submitted", "confirmed"):
        return _tmpl("error.html", request, user=user,
                     message="This report is already submitted — payments can no longer be edited.")
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    installments, _ = _load_installments(json_path)
    for v in vouchers:
        entry = installments.get(v["bill_no"])
        if entry:
            v["payment"] = entry.get("payment", "")
            v["payment_date"] = entry.get("date", "")
            v["payment_type"] = entry.get("payment_type", "cash")
            v["upi_txn_id"] = entry.get("upi_txn_id", "")
            v["check_bank"] = entry.get("check_bank", "")
            v["check_branch"] = entry.get("check_branch", "")
            v["check_no"] = entry.get("check_no", "")
            v["check_date"] = entry.get("check_date", "")
            v["return_items"] = entry.get("return_items", [])
    total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
    paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
    _flag_salesman_mismatches(vouchers, salesman)
    return _tmpl("coll/submit_edit.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=salesman,
                 total_collected=total_collected, paid_count=paid_count,
                 type_totals=payment_type_totals(vouchers))


@app.post("/coll/submit/{stem}", response_class=HTMLResponse)
async def coll_submit_save(request: Request, stem: str):
    user, err = _require(request, "coll_submit")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    sel = data.get("selection", [])
    sel_type = data.get("selection_type", "beat_salesman")
    salesman = owning_salesman(sel_type, sel)
    if user.role == "salesman" and salesman != user.name:
        return _tmpl("error.html", request, user=user, message="Report not found.")

    form = await request.form()
    action = (form.get("action") or "save").strip()

    beat = sel[0] if sel else ""

    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    invalid = 0
    for v in vouchers:
        bill_no = v["bill_no"]
        raw = (form.get(f"pay_{bill_no}") or "").strip()
        v["payment_type"] = (form.get(f"paytype_{bill_no}") or "cash").strip() or "cash"
        v["upi_txn_id"] = (form.get(f"upitxn_{bill_no}") or "").strip()
        v["check_bank"] = (form.get(f"checkbank_{bill_no}") or "").strip()
        v["check_branch"] = (form.get(f"checkbranch_{bill_no}") or "").strip()
        v["check_no"] = (form.get(f"checkno_{bill_no}") or "").strip()
        v["check_date"] = (form.get(f"checkdate_{bill_no}") or "").strip()
        v["return_items"] = []
        items_reason = None
        if v["payment_type"] == "returns":
            # The payment IS the items total: recomputed server-side from
            # qty x price, the typed/auto-filled pay_ value is ignored.
            rows = [{"item": i, "qty": q, "price": p} for i, q, p in zip(
                form.getlist(f"retitem_{bill_no}"),
                form.getlist(f"retqty_{bill_no}"),
                form.getlist(f"retprice_{bill_no}"))]
            items, total, items_reason = parse_return_items(rows)
            v["return_items"] = items if not items_reason else rows
            raw = total if not items_reason else ""
        if items_reason:
            normalized, reason = None, items_reason
        else:
            normalized, reason = validate_payment(raw, v.get("balance"))
        if not reason and normalized:
            v["payment"] = normalized  # validate_payment_type cross-checks it (returns total)
            reason = validate_payment_type(v)
        if reason:
            invalid += 1
            v["error"] = reason  # template renders an inline bubble on this row
            v["payment"] = raw  # keep what was typed so the form re-renders with it
        else:
            v["payment"] = normalized
    if invalid:
        total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
        paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
        _flag_salesman_mismatches(vouchers, salesman)
        return _tmpl("coll/submit_edit.html", request, user=user,
                     stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                     beat=beat, salesman=salesman,
                     total_collected=total_collected, paid_count=paid_count,
                     type_totals=payment_type_totals(vouchers),
                     error=f"Nothing saved — {invalid} payment(s) need correction")

    prior_installments, _ = _load_installments(json_path)
    compute_payment_dates(vouchers, prior_installments)

    try:
        record_submit_payments(json_path, data, vouchers, submit_for_review=(action == "submit"),
                               beats=[beat] if beat else [], salesmen=[salesman] if salesman else [])
    except StageError as e:
        return _tmpl("error.html", request, user=user, message=str(e))

    if action == "submit":
        return _redirect_ok("/coll/submit", "Collections submitted for supervisor review.")

    return _redirect_ok("/menu", "Progress saved.")


# ---------------------------------------------------------------------------
# Print Collection List  (coll_print)
# ---------------------------------------------------------------------------

def _print_candidates():
    return [{"stem": p.stem, "label": _report_label(d), "data": d}
            for p, d in _load_confirmed_start_reports()]


@app.get("/coll/print", response_class=HTMLResponse)
def coll_print(request: Request):
    user, err = _require(request, "coll_print")
    if err:
        return err
    return _tmpl("coll/print.html", request, user=user, reports=_print_candidates())


@app.post("/coll/print", response_class=HTMLResponse)
async def coll_print_generate(request: Request):
    user, err = _require(request, "coll_print")
    if err:
        return err
    form = await request.form()
    stems = form.getlist("stems")

    def _retry(message):
        return _tmpl("coll/print.html", request, user=user,
                     reports=_print_candidates(), error=message)

    if not stems:
        return _retry("Select at least one collection list.")
    if len(stems) > 3:
        return _retry("Select at most 3 collection lists.")
    # Only lists the selection page offers may be printed — a crafted POST
    # must not reach reports in other stages.
    candidates = {p.stem: d for p, d in _load_confirmed_start_reports()}
    chosen = []
    for stem in stems:
        data = candidates.get(stem)
        if data is None:
            return _retry("Report not found.")
        chosen.append(data)
    return HTMLResponse(build_print_collection_html(chosen, auto_print=True))


# ---------------------------------------------------------------------------
# Approve Collections  (coll-approve-submit)
# ---------------------------------------------------------------------------

@app.get("/coll/approve-submit", response_class=HTMLResponse)
def coll_approve_submit(request: Request):
    user, err = _require(request, "coll_approve_submit")
    if err:
        return err
    pending = _load_pending_submit_reports()
    reports = [{"stem": p.stem, "label": _report_label(d), "data": d} for p, d in pending]
    return _tmpl("coll/approve_submit.html", request, user=user, reports=reports)


def _render_submit_review(request, user, stem, json_path, data, error=None):
    """Render the Approve Collections review screen.

    Shared by the GET route and the approve action's gate-failure path
    (blocked approve keeps the supervisor on the review with an error
    banner). Correction and verification flags are render-time only —
    verification itself IS persisted (via the /verify endpoint) but the
    per-voucher `verified` flag attached here is just a read of that state,
    same pattern as _render_start_review.
    """
    data = prepare_submit_review(json_path, data)
    vouchers = data["vouchers"]
    sel = data.get("selection", [])
    total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
    paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
    bill_nos = [v["bill_no"] for v in vouchers]
    open_map = open_corrections_for_bills(bill_nos)
    applied_bills = {c["bill_no"] for c in load_corrections(statuses=["applied"])
                     if c["bill_no"] in set(bill_nos)}
    marked = set(data.get("verification", {}).get("bill_nos", []))
    for v in vouchers:
        v["correction_open"] = v["bill_no"] in open_map
        v["correction_applied"] = v["bill_no"] in applied_bills
        v["verified"] = v["bill_no"] in marked
    open_count = sum(len(reqs) for reqs in open_map.values())
    salesman = owning_salesman(data.get("selection_type", "beat_salesman"), sel)
    _flag_salesman_mismatches(vouchers, salesman)
    return _tmpl("coll/approve_submit_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=salesman,
                 total_collected=total_collected, paid_count=paid_count,
                 type_totals=payment_type_totals(vouchers),
                 open_corrections=open_count,
                 verified_count=sum(1 for v in vouchers if v["verified"]),
                 count_verified=bool(data.get("verification", {}).get("count")),
                 all_verified=is_submit_verification_complete(data) and not open_count,
                 error=error)


@app.get("/coll/approve-submit/{stem}", response_class=HTMLResponse)
def coll_approve_submit_review(request: Request, stem: str):
    user, err = _require(request, "coll_approve_submit")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    return _render_submit_review(request, user, stem, json_path, data)


@app.post("/coll/approve-submit/{stem}/verify")
def coll_approve_submit_verify(request: Request, stem: str,
                               bill_no: str = Form(default=""),
                               verified: str = Form(default=""),
                               count: str = Form(default="")):
    """Persist one verification toggle (a voucher checkbox or the count box).

    Mirror of coll_approve_start_verify for the evening cross-check. JSON
    responses for the page-script fetch caller; shares _verify_lock with the
    start-stage endpoint (single-process deployment, same serialization
    concern for either report's read-modify-write).
    """
    user = _get_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    try:
        perms = load_permissions()
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    if "coll_approve_submit" not in perms.get(user.role, frozenset()):
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    bill_no = bill_no.strip()
    if bool(bill_no) == bool(count):
        return JSONResponse({"ok": False, "error": "params"}, status_code=400)
    if bill_no and open_corrections_for_bills([bill_no]):
        return JSONResponse({"ok": False, "error": "correction"}, status_code=409)
    with _verify_lock:
        json_path, data = _load_staging_report(stem)
        if json_path is None:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        try:
            if bill_no:
                set_submit_verification(json_path, data, bill_no=bill_no,
                                        verified=(verified == "1"))
            else:
                set_submit_verification(json_path, data,
                                        count_verified=(count == "1"))
        except StageError:
            return JSONResponse({"ok": False, "error": "stage"}, status_code=409)
        except ValueError:
            return JSONResponse({"ok": False, "error": "unknown_bill"}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/coll/approve-submit/{stem}", response_class=HTMLResponse)
def coll_approve_submit_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "coll_approve_submit")
    if err:
        return err
    if action not in ("approve", "return"):
        return _r("/coll/approve-submit")
    with _verify_lock:
        json_path, data = _load_staging_report(stem)
        if json_path is None:
            return _tmpl("error.html", request, user=user, message="Report not found.")
        # Gate order on approve — but ONLY when the report is actually in the
        # 'submitted' stage apply_submit_approval requires: validate_payment
        # and the two web-only gates below are meaningless (and, worse,
        # misleading) for a report already confirmed/returned/in-progress —
        # that case must fall straight through to apply_submit_approval's own
        # StageError so the existing wrong-stage message still surfaces.
        # Within 'submitted': data validity first (a genuinely bad payment
        # must never be masked by a "please verify" message — same defense
        # apply_submit_approval performs, checked here early so it renders
        # via error.html exactly as before this feature existed), then the
        # two web-only gates — open correction requests, then verification
        # completeness. Return stays available either way — it is the
        # documented remedy for both (a correction auto-settles on a
        # matching resubmission; verification simply restarts on the
        # resubmitted report, which the stage guard already clears).
        if action == "approve" and data.get("stages", {}).get("submit") == "submitted":
            errors = validate_staged_report(data)
            if errors:
                return _tmpl("error.html", request, user=user,
                             message=format_submit_validation_errors(errors))
            open_map = open_corrections_for_bills(
                [v.get("bill_no") for v in data.get("vouchers", []) if isinstance(v, dict)])
            if open_map:
                n = sum(len(reqs) for reqs in open_map.values())
                return _render_submit_review(
                    request, user, stem, json_path, data,
                    error=f"Cannot approve — {n} correction request"
                          f"{'s are' if n != 1 else ' is'} awaiting resolution.")
            if not is_submit_verification_complete(data):
                return _render_submit_review(
                    request, user, stem, json_path, data,
                    error="Cannot approve — verify every voucher and confirm the "
                          "returned-voucher count against your notes first.")
        try:
            apply_submit_approval(json_path, data, action)
        except (StageError, ValidationError) as e:
            return _tmpl("error.html", request, user=user, message=str(e))

    if action == "return":
        return _redirect_ok("/coll/approve-submit", "Collections returned to salesman for revision.")
    return _redirect_ok("/coll/approve-submit", "Collections approved — ready to post.")


# ---------------------------------------------------------------------------
# Post Collections  (coll-post)
# ---------------------------------------------------------------------------

@app.get("/coll/post", response_class=HTMLResponse)
def coll_post(request: Request):
    user, err = _require(request, "coll_post")
    if err:
        return err
    stale = read_finalize_checkpoint()
    reports = [(p.stem, _report_label(d), d) for p, d in _load_submit_confirmed_reports()]
    return _tmpl("coll/post.html", request, user=user, reports=reports, stale_checkpoint=stale)


@app.get("/coll/post/{stem}", response_class=HTMLResponse)
def coll_post_review(request: Request, stem: str):
    user, err = _require(request, "coll_post")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    sel = data.get("selection", [])
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
    paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
    salesman = owning_salesman(data.get("selection_type", "beat_salesman"), sel)
    _flag_salesman_mismatches(vouchers, salesman)
    return _tmpl("coll/post_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=salesman,
                 total_collected=total_collected, paid_count=paid_count,
                 type_totals=payment_type_totals(vouchers))


@app.post("/coll/post/{stem}", response_class=HTMLResponse)
def coll_post_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "coll_post")
    if err:
        return err
    if action not in ("post", "return"):
        return _r("/coll/post")
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")

    if action == "return":
        try:
            return_post_stage(json_path, data)
        except StageError as e:
            return _tmpl("error.html", request, user=user, message=str(e))
        return _redirect_ok("/coll/post", "Returned to supervisor for re-approval.")

    outcome = post_confirmed_report(json_path, posted_by=user.name)
    if not outcome.ok:
        if outcome.step_failed:
            message = (f"Post failed at step {outcome.step_failed}: {outcome.error}. "
                       "A checkpoint remains — check data before retrying.")
        else:
            message = f"Post failed: {outcome.error}"
        return _tmpl("error.html", request, user=user, message=message)

    return _redirect_ok(
        "/menu", f"Posted. {outcome.paid_count} vouchers collected. Total: {outcome.total_collected}")


# ---------------------------------------------------------------------------
# Voucher detail
# ---------------------------------------------------------------------------

@app.get("/voucher/{bill_no}", response_class=HTMLResponse)
def voucher_detail(request: Request, bill_no: str, fragment: int = 0,
                   correct: str = ""):
    # No permission key: any logged-in user may look up a voucher, same as
    # the Voucher Search report this view reuses.
    user, err = _require(request)
    if err:
        return err
    result = search_voucher(bill_no)
    if result is None:
        if fragment:
            return HTMLResponse('<p class="alert alert-error">Voucher not found.</p>',
                                status_code=404)
        return _tmpl("error.html", request, user=user,
                     message=f"No voucher found for: {bill_no.strip()}")
    voucher, installments, is_completed = result
    # `correct` (a validated return path) opts the inline fragment into a
    # Raise Correction button — only set by the approval review screens, and
    # only rendered for roles holding raise_correction on active vouchers.
    correct_from = _safe_from(correct) if correct else ""
    can_raise = False
    if correct_from and not is_completed:
        try:
            can_raise = "raise_correction" in load_permissions().get(user.role, frozenset())
        except FileNotFoundError:
            can_raise = False
    # Standalone — unlike Raise Correction, not tied to an approval-review
    # context: the Amend Voucher editor itself isn't stage-gated, so its
    # request mechanism shouldn't be either.
    can_request_amend = False
    if not is_completed:
        try:
            can_request_amend = "raise_amendment_request" in load_permissions().get(user.role, frozenset())
        except FileNotFoundError:
            can_request_amend = False
    # Inline expand gets the slim installments-only partial; the standalone
    # page (and Voucher Search's include) keep the full card.
    template = "_voucher_inline.html" if fragment else "voucher.html"
    return _tmpl(template, request, user=user,
                 voucher=voucher, installments=installments, is_completed=is_completed,
                 correct_from=correct_from, can_raise=can_raise,
                 can_request_amend=can_request_amend)


# ---------------------------------------------------------------------------
# Correction Requests
# ---------------------------------------------------------------------------

# Return-path whitelist for the raise form's from= param: relative /coll/...
# paths only, so a crafted link can't bounce the user off-site.
_FROM_RE = re.compile(r"^/coll/[A-Za-z0-9_/.\-]*$")

_MASTER_KIND_LABELS = {
    "installment_amount": "Change an installment amount",
    "installment_delete": "Delete an installment",
    "installment_add": "Add a missing installment",
    "voucher_amount": "Change the voucher amount",
}

# Display map for ALL kinds (list/detail pages show any stored request).
_KIND_LABELS = dict(_MASTER_KIND_LABELS)
_KIND_LABELS["collection_amount"] = "Change the collection amount"


def _kinds_for_stage(origin_stage):
    """Kinds raiseable from a given approval screen (user decision):
    Approve Collections may correct ONLY this cycle's collection amount —
    never the voucher amount or past installments; those master-data kinds
    belong to the Approve Collection List review alone."""
    if origin_stage == "submit":
        return {"collection_amount": _KIND_LABELS["collection_amount"]}
    return _MASTER_KIND_LABELS


def _safe_from(from_path):
    return from_path if from_path and _FROM_RE.match(from_path) else "/menu"


def _correction_context(from_path):
    """Derive the audit-only (report_stem, origin_stage) from the from= path."""
    m = re.match(r"^/coll/approve-(start|submit)/([A-Za-z0-9_.\-]+)$", from_path or "")
    if not m:
        return "", ""
    return m.group(2), m.group(1)


def _valid_amount(raw):
    """Strict positive money amount in the DB's written format, or None."""
    s = (raw or "").strip()
    if not s or re.search(r"[^0-9.]", s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite() or d <= 0:
        return None
    return str(d.quantize(Decimal("0.01")))


def _valid_past_date(raw):
    """ISO YYYY-MM-DD, not in the future (mirrors the payment_date rule), or None."""
    s = (raw or "").strip()
    try:
        if datetime.strptime(s, "%Y-%m-%d").date() > datetime.now().date():
            return None
    except ValueError:
        return None
    return s


def _can_act_on(corr, user):
    """Per-kind resolution authority (user decision): collection_amount is
    staged approval data, so anyone who can approve collections may resolve
    it (coll_approve_submit: supervisor + distributor); the master-data
    kinds stay distributor-only (apply_correction)."""
    key = ("coll_approve_submit" if corr["kind"] == "collection_amount"
           else "apply_correction")
    try:
        return key in load_permissions().get(user.role, frozenset())
    except FileNotFoundError:
        return False


def _render_correction_form(request, user, voucher, installments, back, error=None):
    _, origin_stage = _correction_context(back)
    kinds = _kinds_for_stage(origin_stage)
    staged_payment = None
    if "collection_amount" in kinds:
        _, _, sv = _find_active_report_for_bill(voucher["bill_no"])
        if sv is not None:
            staged_payment = (sv.get("payment") or "").strip()
    existing = [c for c in load_corrections()
                if c["bill_no"] == voucher["bill_no"]][:10]
    # The amendment-request cross-link only makes sense alongside the
    # master-data kinds (Approve Collection List) — Approve Collections only
    # ever offers collection_amount, which an amendment never touches.
    can_request_amend = False
    if "collection_amount" not in kinds:
        try:
            can_request_amend = "raise_amendment_request" in load_permissions().get(user.role, frozenset())
        except FileNotFoundError:
            can_request_amend = False
    return _tmpl("coll/correction_form.html", request, user=user,
                 voucher=voucher, installments=installments, back=back,
                 kinds=kinds, kind_labels=_KIND_LABELS,
                 staged_payment=staged_payment,
                 existing=existing, error=error,
                 can_request_amend=can_request_amend)


def _load_correction_target(request, user, bill_no):
    """Resolve an active (non-completed) voucher for correction, or an error page."""
    result = search_voucher(bill_no)
    if result is None:
        return None, _tmpl("error.html", request, user=user,
                           message=f"No voucher found for: {bill_no.strip()}")
    voucher, installments, is_completed = result
    if is_completed:
        return None, _tmpl("error.html", request, user=user,
                           message="Completed vouchers cannot be corrected here.")
    return (voucher, installments), None


@app.get("/coll/correct/{bill_no}", response_class=HTMLResponse)
def coll_correction_form(request: Request, bill_no: str,
                         from_path: str = Query(default="/menu", alias="from")):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    target, err = _load_correction_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target
    return _render_correction_form(request, user, voucher, installments,
                                   back=_safe_from(from_path))


@app.post("/coll/correct/{bill_no}", response_class=HTMLResponse)
def coll_correction_submit(request: Request, bill_no: str,
                           action: str = Form(default="raise"),
                           kind: str = Form(default=""),
                           installment_id: str = Form(default=""),
                           new_amount: str = Form(default=""),
                           new_date: str = Form(default=""),
                           note: str = Form(default=""),
                           corr_id: str = Form(default=""),
                           from_path: str = Form(default="/menu", alias="from")):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    back = _safe_from(from_path)
    target, err = _load_correction_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target

    def form_error(msg):
        return _render_correction_form(request, user, voucher, installments,
                                       back=back, error=msg)

    if action == "withdraw":
        corr = load_correction(int(corr_id)) if corr_id.isdigit() else None
        if (corr is None or corr["bill_no"] != voucher["bill_no"]
                or corr["requested_by"] != user.name):
            return form_error("Only your own requests for this voucher can be withdrawn.")
        try:
            apply_correction_request(corr["id"], "withdraw", user.name)
        except ValueError as e:
            return form_error(str(e))
        return _r(f"/coll/correct/{voucher['bill_no']}?from={back}")

    # Kinds are context-restricted: only what this approval screen may raise.
    if kind not in _kinds_for_stage(_correction_context(back)[1]):
        return form_error("Choose what kind of correction to request.")

    record = {"kind": kind, "bill_no": voucher["bill_no"], "note": note.strip(),
              "requested_by": user.name,
              "requested_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    record["report_stem"], record["origin_stage"] = _correction_context(back)

    if kind == "collection_amount":
        _, rdata, sv = _find_active_report_for_bill(voucher["bill_no"])
        if sv is None or (rdata.get("stages", {}).get("submit") or "") != "submitted":
            return form_error("This voucher is not in a collection report awaiting approval.")
        if (sv.get("payment_type") or "") == "returns":
            return form_error(
                "This voucher is paid by returned stock — the payment is the items total, "
                "so it can't be corrected by amount. Return the report to the salesman to revise the items.")
        staged_payment = (sv.get("payment") or "").strip()
        corrected = (new_amount or "").strip()
        if corrected:
            # voucher comes from master (search_voucher) -> current master balance.
            corrected, reason = validate_payment(corrected, voucher["balance"])
            if reason:
                return form_error(f"Corrected collection {reason}.")
            if Decimal(corrected) == 0:
                corrected = ""  # zero and empty both mean: no collection
        if corrected == staged_payment:
            return form_error("The corrected collection is the same as the entered one.")
        record["old"] = {"payment": staged_payment,
                         "date": (sv.get("payment_date") or "").strip()}
        record["new"] = {"payment": corrected}
    elif kind in ("installment_amount", "installment_delete"):
        inst = next((i for i in installments
                     if str(i.get("id")) == installment_id.strip()), None)
        if inst is None:
            return form_error("Pick the installment this correction applies to.")
        record["installment_id"] = inst["id"]
        record["old"] = {"date": inst["date"], "amount": inst["amount"]}
        if kind == "installment_amount":
            amount = _valid_amount(new_amount)
            if amount is None:
                return form_error("Enter a valid corrected amount (positive, max 2 decimals).")
            if amount == str(parse_decimal(inst["amount"]).quantize(Decimal("0.01"))):
                return form_error("The corrected amount is the same as the recorded one.")
            record["new"] = {"amount": amount}
    elif kind == "installment_add":
        amount = _valid_amount(new_amount)
        if amount is None:
            return form_error("Enter a valid installment amount (positive, max 2 decimals).")
        date = _valid_past_date(new_date)
        if date is None:
            return form_error("Enter a valid installment date (YYYY-MM-DD, not in the future).")
        record["new"] = {"date": date, "amount": amount}
    else:  # voucher_amount
        amount = _valid_amount(new_amount)
        if amount is None:
            return form_error("Enter a valid voucher amount (positive, max 2 decimals).")
        if amount == str(parse_decimal(voucher["amount"]).quantize(Decimal("0.01"))):
            return form_error("The corrected amount is the same as the recorded one.")
        record["old"] = {"amount": voucher["amount"]}
        record["new"] = {"amount": amount}

    insert_correction(record)
    return _r(back)


def _workflow_link(data, stem):
    """Resolve the gated-workflow link/label for a report a correction blocks."""
    stages = data.get("stages", {})
    if stages.get("start") == "new":
        return f"/coll/approve-start/{stem}", "Awaiting Collection List approval"
    submit = stages.get("submit", "")
    if submit == "submitted":
        return f"/coll/approve-submit/{stem}", "Awaiting Collections approval"
    if submit == "confirmed":
        return f"/coll/post/{stem}", "Awaiting posting"
    if submit in ("inprogress", "returned"):
        return None, "With salesman (submission in progress)"
    return None, "Approved list awaiting submission"


@app.get("/coll/corrections", response_class=HTMLResponse)
def coll_corrections(request: Request):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    # Load active staging reports ONCE; both the active/history split and the
    # gated-workflow links derive from this lookup by bill_no. Nothing is
    # stored: an applied record drops to history the moment its report posts
    # or is cancelled, and resurfaces if the bill re-enters a new list.
    bill_map = {}
    for path in list_staging_reports():
        try:
            data = load_report_json(path)
        except Exception:
            continue
        for v in data.get("vouchers", []):
            if isinstance(v, dict) and v.get("bill_no"):
                bill_map.setdefault(v["bill_no"], (path.stem, data))

    def entry(corr):
        hit = bill_map.get(corr["bill_no"])
        link = label = None
        if hit:
            link, label = _workflow_link(hit[1], hit[0])
        return {"corr": corr, "link": link, "label": label,
                "can_act": _can_act_on(corr, user)}

    active, history = [], []
    for corr in load_corrections():
        if corr["status"] == "open" or (corr["status"] == "applied"
                                        and corr["bill_no"] in bill_map):
            active.append(entry(corr))
        else:
            history.append(corr)
    return _tmpl("coll/corrections.html", request, user=user,
                 active=active, history=history[:20], kinds=_KIND_LABELS)


@app.get("/coll/corrections/{cid}", response_class=HTMLResponse)
def coll_correction_review(request: Request, cid: int, next: str = ""):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    corr = load_correction(cid)
    if corr is None:
        return _tmpl("error.html", request, user=user, message="Correction request not found.")
    result = search_voucher(corr["bill_no"])
    voucher = installments = None
    if result is not None:
        voucher, installments, _completed = result
    staged_payment = None
    if corr["kind"] == "collection_amount":
        _, _, sv = _find_active_report_for_bill(corr["bill_no"])
        if sv is not None:
            staged_payment = (sv.get("payment") or "").strip()
    # `next` lets a caller other than the Correction Requests list (e.g. the
    # amend gate below) send the distributor back to itself once resolved.
    back = _safe_from(next) if next else "/coll/corrections"
    return _tmpl("coll/correction_review.html", request, user=user,
                 corr=corr, voucher=voucher, installments=installments,
                 staged_payment=staged_payment, back=back,
                 kinds=_KIND_LABELS, can_apply=_can_act_on(corr, user))


@app.post("/coll/corrections/{cid}", response_class=HTMLResponse)
def coll_correction_action(request: Request, cid: int,
                           action: str = Form(default=""),
                           resolution_note: str = Form(default=""),
                           next: str = Form(default="/coll/corrections")):
    # Resolution authority is per kind (_can_act_on), not one permission key:
    # supervisors may resolve collection_amount requests but not master kinds.
    user, err = _require(request)
    if err:
        return err
    back = _safe_from(next)
    corr = load_correction(cid)
    if corr is None:
        return _tmpl("error.html", request, user=user, message="Correction request not found.")
    if not _can_act_on(corr, user):
        return _tmpl("error.html", request, user=user,
                     message="You don't have permission for this action.")
    if action not in ("apply", "reject"):
        return _r(back)
    try:
        apply_correction_request(cid, action, user.name, resolution_note.strip())
    except CorrectionConflict as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    except ValueError as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    msg = (("Correction applied — the staged collection was updated."
            if corr["kind"] == "collection_amount"
            else "Correction applied — master data updated and staged balances refreshed.")
           if action == "apply" else "Correction rejected.")
    return _redirect_ok(back, msg)


# ---------------------------------------------------------------------------
# Voucher Amendment
# ---------------------------------------------------------------------------

def _load_amend_target(request, user, bill_no):
    """Resolve an active (non-completed) voucher for amendment, or an error page."""
    result = search_voucher(bill_no)
    if result is None:
        return None, _tmpl("error.html", request, user=user,
                           message=f"No voucher found for: {bill_no.strip()}")
    voucher, installments, is_completed = result
    if is_completed:
        return None, _tmpl("error.html", request, user=user,
                           message="Completed vouchers cannot be amended.")
    return (voucher, installments), None


def _amend_snapshot(voucher, installments):
    """The exact {voucher, installments} shape re-checked verbatim at apply
    time (coll_store.apply_voucher_amendment) — round-trips through a hidden
    form field, so it must carry only what that check compares."""
    return {"voucher": {"date": voucher["date"], "amount": voucher["amount"],
                        "balance": voucher["balance"], "beat": voucher["beat"],
                        "salesman": voucher["salesman"]},
           "installments": [{"id": i["id"], "date": i["date"], "amount": i["amount"],
                             "salesman": i["salesman"]} for i in installments]}


def _render_amend_form(request, user, bill_no, voucher, installments, snapshot, error=None):
    try:
        beats = load_beats()
        salesmen = load_salesmen()
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    history = load_amendments(bill_no=bill_no, limit=10)
    open_requests = open_amendment_requests_for_bills([bill_no]).get(bill_no, [])
    return _tmpl("coll/amend_form.html", request, user=user,
                 bill_no=bill_no, voucher=voucher, installments=installments,
                 beats=beats, salesmen=salesmen,
                 snapshot_json=json.dumps(snapshot), history=history, error=error,
                 open_requests=open_requests)


@app.get("/coll/amend", response_class=HTMLResponse)
def coll_amend_pick(request: Request, q: str = ""):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    error = None
    if q.strip():
        bill_no = q.strip()
        result = search_voucher(bill_no)
        if result is None:
            error = f"No voucher found for: {bill_no}"
        elif result[2]:
            error = "Completed vouchers cannot be amended."
        else:
            return _r(f"/coll/amend/{bill_no}")
    return _tmpl("coll/amend_pick.html", request, user=user, q=q, error=error)


@app.get("/coll/amend/{bill_no}", response_class=HTMLResponse)
def coll_amend_form(request: Request, bill_no: str):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    target, err = _load_amend_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target

    # Gate (iteration4 decision 4): every open MASTER-DATA correction on this
    # bill must be resolved before the raw editor is reachable — an
    # amendment could make its snapshot stale. collection_amount requests
    # don't gate: they concern only this cycle's staged payment, which an
    # amendment never touches. Bounce straight to the oldest one; the
    # existing correction routes send the distributor back here via `next`.
    opens = open_corrections_for_bills([bill_no]).get(bill_no, [])
    blocking = [c for c in opens if c["kind"] in _MASTER_KIND_LABELS]
    if blocking:
        return _r(f"/coll/corrections/{blocking[0]['id']}?next=/coll/amend/{bill_no}")

    snapshot = _amend_snapshot(voucher, installments)
    return _render_amend_form(request, user, bill_no, voucher, installments, snapshot)


@app.post("/coll/amend/{bill_no}", response_class=HTMLResponse)
async def coll_amend_submit(request: Request, bill_no: str):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    target, err = _load_amend_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target

    form = await request.form()
    snapshot_raw = form.get("snapshot", "")
    try:
        snapshot = json.loads(snapshot_raw)
    except (ValueError, TypeError):
        return _render_amend_form(
            request, user, bill_no, voucher, installments,
            _amend_snapshot(voucher, installments),
            error="Could not read the loaded form state — reload and try again.")

    try:
        beats_list = load_beats()
        salesmen_list = load_salesmen()
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))

    v_date_raw = (form.get("v_date") or "").strip()
    v_amount_raw = (form.get("v_amount") or "").strip()
    v_beat = (form.get("v_beat") or "").strip()
    v_salesman = (form.get("v_salesman") or "").strip()
    note = (form.get("note") or "").strip()

    # Row encoding: one token per installment row (existing rows: the
    # installment's own id; new rows added client-side: new1, new2, ...).
    # Fields are named by that token, so a variable row count round-trips
    # without parallel-array misalignment.
    submitted_installments = []
    for k in form.getlist("inst_row"):
        submitted_installments.append({
            "id": int(k) if k.isdigit() else None,
            "token": k,  # preserves the field-name suffix across an error re-render
            "date": (form.get(f"inst_date_{k}") or "").strip(),
            "amount": (form.get(f"inst_amount_{k}") or "").strip(),
            "salesman": (form.get(f"inst_salesman_{k}") or "").strip(),
            "_deleted": bool(form.get(f"inst_delete_{k}")),
        })
    submitted_voucher = {**voucher, "date": v_date_raw, "amount": v_amount_raw,
                         "beat": v_beat, "salesman": v_salesman}

    def rerender(error):
        return _render_amend_form(request, user, bill_no, submitted_voucher,
                                  submitted_installments, snapshot, error=error)

    v_date = _valid_past_date(v_date_raw)
    if v_date is None:
        return rerender("Voucher date is required and cannot be in the future.")
    v_amount = _valid_amount(v_amount_raw)
    if v_amount is None:
        return rerender("Voucher amount must be a positive number.")
    if v_beat not in beats_list:
        return rerender(f"Unknown beat: {v_beat}")
    if v_salesman not in salesmen_list:
        return rerender(f"Unknown salesman: {v_salesman}")

    new_installments = []
    for row in submitted_installments:
        if row["_deleted"]:
            continue
        rdate = _valid_past_date(row["date"])
        if rdate is None:
            return rerender("Every installment needs a valid, non-future date.")
        ramount = _valid_amount(row["amount"])
        if ramount is None:
            return rerender("Every installment amount must be a positive number.")
        if row["salesman"] not in salesmen_list:
            return rerender(f"Unknown salesman on an installment: {row['salesman']}")
        new_installments.append({"id": row["id"], "date": rdate, "amount": ramount,
                                 "salesman": row["salesman"]})

    new_state = {"voucher": {"date": v_date, "amount": v_amount,
                             "beat": v_beat, "salesman": v_salesman},
                "installments": new_installments}

    try:
        amendment = amend_voucher(bill_no, snapshot, new_state, user.name, note)
    except AmendmentConflict:
        fresh = search_voucher(bill_no)
        if fresh is None or fresh[2]:
            return _tmpl("error.html", request, user=user,
                         message="This voucher changed while you were editing and is"
                                 " no longer available for amendment.")
        fresh_voucher, fresh_installments, _completed = fresh
        return _render_amend_form(
            request, user, bill_no, fresh_voucher, fresh_installments,
            _amend_snapshot(fresh_voucher, fresh_installments),
            error="This voucher changed while you were editing — showing the current"
                  " data below; please re-enter your changes.")
    except ValueError as e:
        return rerender(str(e))

    balance = amendment["new"]["voucher"]["balance"]
    return _redirect_ok("/coll/amend", f"Amendment applied — new balance {balance}.")


@app.get("/coll/amendments", response_class=HTMLResponse)
def coll_amendments(request: Request):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    history = load_amendments(limit=50)
    return _tmpl("coll/amendments.html", request, user=user, history=history)


@app.get("/coll/amendments/{aid}", response_class=HTMLResponse)
def coll_amendment_review(request: Request, aid: int):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    amendment = load_amendment(aid)
    if amendment is None:
        return _tmpl("error.html", request, user=user, message="Amendment not found.")
    return _tmpl("coll/amendment_review.html", request, user=user, amendment=amendment)


# ---------------------------------------------------------------------------
# Amendment Requests
# ---------------------------------------------------------------------------

def _can_view_amend_requests(user):
    try:
        perms = load_permissions().get(user.role, frozenset())
    except FileNotFoundError:
        return False
    return "raise_amendment_request" in perms or "amend_voucher" in perms


def _require_amend_requests_view(request):
    """Like _require(), but for the OR of raise_amendment_request/amend_voucher
    — both raisers and the distributor who resolves them may view."""
    user = _get_user(request)
    if not user:
        return None, _r("/login")
    if not _can_view_amend_requests(user):
        return user, _tmpl("error.html", request, user=user,
                           message="You don't have permission for this action.")
    return user, None


def _load_amend_request_target(request, user, bill_no):
    """Resolve an active (non-completed) voucher for an amendment request, or an error page."""
    result = search_voucher(bill_no)
    if result is None:
        return None, _tmpl("error.html", request, user=user,
                           message=f"No voucher found for: {bill_no.strip()}")
    voucher, installments, is_completed = result
    if is_completed:
        return None, _tmpl("error.html", request, user=user,
                           message="Completed vouchers cannot have an amendment requested.")
    return (voucher, installments), None


def _render_amend_request_form(request, user, voucher, installments, back, error=None):
    existing = [r for r in load_amendment_requests()
                if r["bill_no"] == voucher["bill_no"]][:10]
    return _tmpl("coll/amend_request_form.html", request, user=user,
                 voucher=voucher, installments=installments, back=back,
                 existing=existing, error=error)


@app.get("/coll/amend-request", response_class=HTMLResponse)
def coll_amend_request_pick(request: Request, q: str = ""):
    user, err = _require(request, "raise_amendment_request")
    if err:
        return err
    error = None
    if q.strip():
        bill_no = q.strip()
        result = search_voucher(bill_no)
        if result is None:
            error = f"No voucher found for: {bill_no}"
        elif result[2]:
            error = "Completed vouchers cannot have an amendment requested."
        else:
            return _r(f"/coll/amend-request/{bill_no}")
    return _tmpl("coll/amend_request_pick.html", request, user=user, q=q, error=error)


@app.get("/coll/amend-request/{bill_no}", response_class=HTMLResponse)
def coll_amend_request_form(request: Request, bill_no: str,
                            from_path: str = Query(default="/coll/amend-requests", alias="from")):
    user, err = _require(request, "raise_amendment_request")
    if err:
        return err
    target, err = _load_amend_request_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target
    return _render_amend_request_form(request, user, voucher, installments,
                                      back=_safe_from(from_path))


@app.post("/coll/amend-request/{bill_no}", response_class=HTMLResponse)
def coll_amend_request_submit(request: Request, bill_no: str,
                              action: str = Form(default="raise"),
                              note: str = Form(default=""),
                              req_id: str = Form(default=""),
                              from_path: str = Form(default="/coll/amend-requests", alias="from")):
    user, err = _require(request, "raise_amendment_request")
    if err:
        return err
    back = _safe_from(from_path)
    target, err = _load_amend_request_target(request, user, bill_no)
    if err:
        return err
    voucher, installments = target

    def form_error(msg):
        return _render_amend_request_form(request, user, voucher, installments,
                                          back=back, error=msg)

    if action == "withdraw":
        req = load_amendment_request(int(req_id)) if req_id.isdigit() else None
        if (req is None or req["bill_no"] != voucher["bill_no"]
                or req["requested_by"] != user.name):
            return form_error("Only your own requests for this voucher can be withdrawn.")
        try:
            resolve_amendment_request(req["id"], "withdraw", user.name)
        except ValueError as e:
            return form_error(str(e))
        return _r(f"/coll/amend-request/{voucher['bill_no']}?from={back}")

    note = note.strip()
    if not note:
        return form_error("Describe what needs to be amended.")
    try:
        raise_amendment_request(voucher["bill_no"], user.name, note)
    except ValueError as e:
        return form_error(str(e))
    return _r(back)


@app.get("/coll/amend-requests", response_class=HTMLResponse)
def coll_amend_requests(request: Request):
    user, err = _require_amend_requests_view(request)
    if err:
        return err
    perms = load_permissions().get(user.role, frozenset())
    can_resolve = "amend_voucher" in perms
    can_raise = "raise_amendment_request" in perms
    active, history = [], []
    for req in load_amendment_requests():
        if req["status"] == "open":
            active.append(req)
        else:
            history.append(req)
    return _tmpl("coll/amend_requests.html", request, user=user,
                 active=active, history=history[:20],
                 can_resolve=can_resolve, can_raise=can_raise)


@app.get("/coll/amend-requests/{req_id}", response_class=HTMLResponse)
def coll_amend_request_review(request: Request, req_id: int, next: str = ""):
    user, err = _require_amend_requests_view(request)
    if err:
        return err
    req = load_amendment_request(req_id)
    if req is None:
        return _tmpl("error.html", request, user=user, message="Amendment request not found.")
    result = search_voucher(req["bill_no"])
    voucher = installments = None
    if result is not None:
        voucher, installments, _completed = result
    can_resolve = "amend_voucher" in load_permissions().get(user.role, frozenset())
    back = _safe_from(next) if next else "/coll/amend-requests"
    return _tmpl("coll/amend_request_review.html", request, user=user,
                 req=req, voucher=voucher, installments=installments,
                 back=back, can_resolve=can_resolve)


@app.post("/coll/amend-requests/{req_id}", response_class=HTMLResponse)
def coll_amend_request_action(request: Request, req_id: int,
                              action: str = Form(default=""),
                              resolution_note: str = Form(default=""),
                              next: str = Form(default="/coll/amend-requests")):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    back = _safe_from(next)
    if action != "reject":
        return _r(back)
    try:
        resolve_amendment_request(req_id, "reject", user.name, resolution_note.strip())
    except ValueError as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    return _redirect_ok(back, "Amendment request rejected.")


# ---------------------------------------------------------------------------
# Checks (view: everyone with view_checks; resolve: distributor only)
# ---------------------------------------------------------------------------

@app.get("/coll/checks", response_class=HTMLResponse)
def coll_checks(request: Request):
    user, err = _require(request, "view_checks")
    if err:
        return err
    perms = load_permissions().get(user.role, frozenset())
    can_resolve = "resolve_check" in perms
    today = datetime.now().date()
    today_iso = today.isoformat()
    due_by_iso = (today + timedelta(days=2)).isoformat()
    due_soon, overdue, pending_later, bounced, encashed = [], [], [], [], []
    for c in load_checks():
        if c["status"] == "pending":
            if c["check_date"] < today_iso:
                overdue.append(c)
            elif c["check_date"] <= due_by_iso:
                due_soon.append(c)
            else:
                pending_later.append(c)
        elif c["status"] == "bounced":
            bounced.append(c)
        else:
            encashed.append(c)
    return _tmpl("coll/checks.html", request, user=user, can_resolve=can_resolve,
                 due_soon=due_soon, overdue=overdue, pending_later=pending_later,
                 bounced=bounced, encashed=encashed)


@app.post("/coll/checks/{check_id}", response_class=HTMLResponse)
def coll_checks_action(request: Request, check_id: int,
                       action: str = Form(default=""),
                       resolution_note: str = Form(default="")):
    user, err = _require(request, "resolve_check")
    if err:
        return err
    if action not in ("encash", "bounce"):
        return _r("/coll/checks")
    try:
        resolve_check(check_id, action, user.name, resolution_note.strip())
    except ValueError as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    verb = "encashed" if action == "encash" else "marked bounced"
    return _redirect_ok("/coll/checks", f"Check {verb}.")


# ---------------------------------------------------------------------------
# Onboarding New Vouchers (web-only): Import -> Salesman Review ->
# Distributor Resolve -> Post. See CLAUDE.md "Voucher Amendment" /
# "Amendment Requests" sections for the live-data analogues this mirrors;
# unlike those, everything here lives inside the addv*.json staging file
# itself (batch_data["flags"], per-voucher "review_status") since staged
# vouchers aren't posted to master data yet — no DB, no snapshot/conflict
# checks needed, the whole batch is one JSON file held for the request.
# ---------------------------------------------------------------------------

def _load_addv_report(stem: str):
    """Resolve a client-supplied addv batch stem to (json_path, report_data).
    Same contract as _load_staging_report: (None, None) for a malformed
    stem, a missing file, or unreadable/non-dict JSON."""
    if not stem or not _STEM_RE.match(stem):
        return None, None
    path = STAGING_DIR / f"{stem}.json"
    if not path.exists():
        return None, None
    try:
        data = load_report_json(path)
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    return path, data


def _require_any(request: Request, permissions):
    """Like _require(), but authorised if the user holds ANY of several
    permission keys — the onboarding hub is a shared landing page for every
    role in this pipeline (importer, reviewer, resolver, poster)."""
    user = _get_user(request)
    if not user:
        return None, _r("/login")
    if user.must_change_password and request.url.path not in _FORCED_CHANGE_ALLOWED_PATHS:
        return user, _r("/profile?forced=1")
    try:
        perms = load_permissions().get(user.role, frozenset())
    except FileNotFoundError:
        perms = frozenset()
    if not any(p in perms for p in permissions):
        return user, _tmpl("error.html", request, user=user,
                           message="You don't have permission for this action.")
    return user, None


@app.get("/coll/import-vouchers", response_class=HTMLResponse)
def coll_import_vouchers_form(request: Request):
    user, err = _require(request, "import_vouchers")
    if err:
        return err
    return _tmpl("coll/import_vouchers.html", request, user=user, error=None)


@app.post("/coll/import-vouchers", response_class=HTMLResponse)
async def coll_import_vouchers_submit(request: Request,
                                      vouchers_file: UploadFile = File(...),
                                      installments_file: UploadFile = File(default=None)):
    user, err = _require(request, "import_vouchers")
    if err:
        return err

    def form_error(msg):
        return _tmpl("coll/import_vouchers.html", request, user=user, error=msg)

    def read_csv_upload(upload):
        text = upload.file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        return list(reader.fieldnames or []), list(reader)

    try:
        v_fields, voucher_rows = read_csv_upload(vouchers_file)
    except Exception as e:
        return form_error(f"Error reading vouchers CSV: {e}")

    required_v = {"bill_no", "date", "amount", "beat", "salesman"}
    missing_v = required_v - set(v_fields)
    if missing_v:
        return form_error(f"Vouchers CSV missing required columns: {', '.join(sorted(missing_v))}")
    if not voucher_rows:
        return form_error("Vouchers CSV has no data rows.")

    inst_rows = []
    if installments_file is not None and installments_file.filename:
        try:
            i_fields, inst_rows = read_csv_upload(installments_file)
        except Exception as e:
            return form_error(f"Error reading installments CSV: {e}")
        required_i = {"bill_no", "date", "amount", "salesman"}
        missing_i = required_i - set(i_fields)
        if missing_i:
            return form_error(f"Installments CSV missing required columns: {', '.join(sorted(missing_i))}")

    beats = load_beats(user)
    salesmen = load_salesmen()
    existing_bill_nos = load_all_existing_bill_nos() | load_addv_staged_bill_nos()
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    errors, vouchers, installments = validate_addv_batch(
        voucher_rows, inst_rows, existing_bill_nos,
        set(beats), set(salesmen), user.name, now_str,
    )
    if errors:
        return form_error("; ".join(errors[:20]) + (" …" if len(errors) > 20 else ""))

    for v in vouchers:
        v["review_status"] = "pending"

    ensure_staging_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_user = sanitize_filename_component(user.name)
    json_path = STAGING_DIR / f"addv{timestamp}-{safe_user}.json"
    report_data = {
        "type": "add_vouchers",
        "mode": "batch",
        "created_by": user.name,
        "created_at": now_str,
        "stage": "added",
        "stages": {"add": "done", "confirm": "", "post": ""},
        "vouchers": vouchers,
        "installments": installments,
        "flags": [],
    }
    save_report_json(json_path, report_data)
    return _redirect_ok("/coll/new-vouchers", f"Imported {len(vouchers)} voucher(s) for review.")


_ADDV_STATUS_LABELS = {
    "pending_review": ("Pending Review", "warn"),
    "awaiting_resolution": ("Awaiting Resolution", "warn"),
    "ready_to_post": ("Ready to Post", "ok"),
    "posted": ("Posted", "muted"),
}


@app.get("/coll/new-vouchers", response_class=HTMLResponse)
def coll_new_vouchers_hub(request: Request):
    user, err = _require_any(request, ("import_vouchers", "raise_correction",
                                       "amend_voucher", "post_new_vouchers"))
    if err:
        return err
    perms = load_permissions().get(user.role, frozenset())
    batches = []
    for path, data in load_addv_batches():
        st = addv_batch_status(data)
        label, badge = _ADDV_STATUS_LABELS[st["status"]]
        batches.append({
            "stem": path.stem, "data": data, "status": st["status"],
            "label": label, "badge": badge,
            "total": st["total"], "reviewed": st["reviewed"], "open_flags": st["open_flags"],
        })
    return _tmpl("coll/new_vouchers.html", request, user=user, batches=batches,
                 can_reject="post_new_vouchers" in perms)


@app.post("/coll/new-vouchers/{stem}/reject", response_class=HTMLResponse)
def coll_new_vouchers_reject(request: Request, stem: str):
    user, err = _require(request, "post_new_vouchers")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    reject_addv_batch(json_path)
    return _redirect_ok("/coll/new-vouchers", "Batch rejected and discarded.")


# --- Salesman review -------------------------------------------------------

@app.get("/coll/new-vouchers/{stem}/review", response_class=HTMLResponse)
def coll_new_vouchers_review_list(request: Request, stem: str):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    mine = addv_vouchers_for_salesman(data, user.name)
    installments_by_bill = {}
    for inst in data.get("installments", []):
        installments_by_bill.setdefault(inst.get("bill_no"), []).append(inst)
    return _tmpl("coll/new_vouchers_review.html", request, user=user,
                 stem=stem, vouchers=mine, installments_by_bill=installments_by_bill)


def _render_addv_review_item(request, user, stem, data, bill_no, error=None):
    voucher = next((v for v in data.get("vouchers", []) if v.get("bill_no") == bill_no), None)
    if voucher is None:
        return _tmpl("error.html", request, user=user, message="Voucher not found in this batch.")
    installments = [i for i in data.get("installments", []) if i.get("bill_no") == bill_no]
    my_flags = [f for f in data.get("flags", []) if f.get("bill_no") == bill_no]
    return _tmpl("coll/new_vouchers_review_item.html", request, user=user,
                 stem=stem, voucher=voucher, installments=installments,
                 kinds=ADDV_FLAG_KINDS, flags=my_flags, error=error)


@app.get("/coll/new-vouchers/{stem}/review/{bill_no}", response_class=HTMLResponse)
def coll_new_vouchers_review_item(request: Request, stem: str, bill_no: str):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    return _render_addv_review_item(request, user, stem, data, bill_no)


@app.post("/coll/new-vouchers/{stem}/review/{bill_no}", response_class=HTMLResponse)
def coll_new_vouchers_review_submit(request: Request, stem: str, bill_no: str,
                                    action: str = Form(default="clear"),
                                    kind: str = Form(default=""),
                                    installment_index: str = Form(default=""),
                                    new_amount: str = Form(default=""),
                                    new_date: str = Form(default=""),
                                    note: str = Form(default="")):
    user, err = _require(request, "raise_correction")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")

    def item_error(msg):
        return _render_addv_review_item(request, user, stem, data, bill_no, error=msg)

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    if action == "clear":
        try:
            clear_addv_review(data, bill_no, user.name)
        except ValueError as e:
            return item_error(str(e))
        save_report_json(json_path, data)
        return _r(f"/coll/new-vouchers/{stem}/review")

    if kind not in ADDV_FLAG_KINDS:
        return item_error("Choose what kind of issue to raise.")

    installments = [i for i in data.get("installments", []) if i.get("bill_no") == bill_no]
    target = new = None
    if kind in ("installment_amount", "installment_delete"):
        if not installment_index.isdigit() or int(installment_index) >= len(installments):
            return item_error("Pick the installment this issue applies to.")
        inst = installments[int(installment_index)]
        target = {"date": inst["date"], "amount": inst["amount"]}
        if kind == "installment_amount":
            amount = _valid_amount(new_amount)
            if amount is None:
                return item_error("Enter a valid corrected amount (positive, max 2 decimals).")
            new = {"amount": amount}
    elif kind == "installment_add":
        amount = _valid_amount(new_amount)
        if amount is None:
            return item_error("Enter a valid installment amount (positive, max 2 decimals).")
        date = _valid_past_date(new_date)
        if date is None:
            return item_error("Enter a valid installment date (YYYY-MM-DD, not in the future).")
        new = {"date": date, "amount": amount}
    elif kind == "voucher_amount":
        amount = _valid_amount(new_amount)
        if amount is None:
            return item_error("Enter a valid voucher amount (positive, max 2 decimals).")
        new = {"amount": amount}

    try:
        raise_addv_flag(data, bill_no, kind, note, user.name, now_str, target=target, new=new)
    except ValueError as e:
        return item_error(str(e))
    save_report_json(json_path, data)
    return _r(f"/coll/new-vouchers/{stem}/review")


@app.post("/coll/new-vouchers/{stem}/review/{bill_no}/clear")
def coll_new_vouchers_review_clear_ajax(request: Request, stem: str, bill_no: str):
    """JSON sibling of the "clear" branch above, called by the review-list
    page script so ticking off vouchers doesn't reload the page (and lose
    scroll position) for every single "Looks Good" click."""
    user = _get_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    try:
        perms = load_permissions()
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    if "raise_correction" not in perms.get(user.role, frozenset()):
        return JSONResponse({"ok": False, "error": "perms"}, status_code=403)
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    try:
        clear_addv_review(data, bill_no, user.name)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    save_report_json(json_path, data)
    return JSONResponse({"ok": True})


# --- Distributor resolve ----------------------------------------------------

@app.get("/coll/new-vouchers/{stem}/resolve", response_class=HTMLResponse)
def coll_new_vouchers_resolve_list(request: Request, stem: str):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    open_flags = [f for f in data.get("flags", []) if f.get("status") == "open"]
    return _tmpl("coll/new_vouchers_resolve.html", request, user=user,
                 stem=stem, flags=open_flags, kinds=ADDV_FLAG_KINDS)


def _render_addv_resolve_item(request, user, stem, data, flag_id, error=None):
    flag = next((f for f in data.get("flags", []) if f.get("id") == flag_id), None)
    if flag is None:
        return _tmpl("error.html", request, user=user, message="Flag not found in this batch.")
    voucher = next((v for v in data.get("vouchers", []) if v.get("bill_no") == flag["bill_no"]), None)
    installments = [i for i in data.get("installments", []) if i.get("bill_no") == flag["bill_no"]]
    return _tmpl("coll/new_vouchers_resolve_item.html", request, user=user,
                 stem=stem, flag=flag, kind_label=ADDV_FLAG_KINDS.get(flag["kind"], flag["kind"]),
                 voucher=voucher, installments=installments,
                 beats=load_beats_raw(), salesmen=load_salesmen(), error=error)


@app.get("/coll/new-vouchers/{stem}/resolve/{flag_id}", response_class=HTMLResponse)
def coll_new_vouchers_resolve_item(request: Request, stem: str, flag_id: int):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    return _render_addv_resolve_item(request, user, stem, data, flag_id)


@app.post("/coll/new-vouchers/{stem}/resolve/{flag_id}", response_class=HTMLResponse)
def coll_new_vouchers_resolve_submit(request: Request, stem: str, flag_id: int,
                                     voucher_date: str = Form(default=""),
                                     voucher_amount: str = Form(default=""),
                                     voucher_beat: str = Form(default=""),
                                     voucher_salesman: str = Form(default=""),
                                     inst_date: List[str] = Form(default=[]),
                                     inst_amount: List[str] = Form(default=[]),
                                     inst_remove: List[str] = Form(default=[])):
    user, err = _require(request, "amend_voucher")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")

    def item_error(msg):
        return _render_addv_resolve_item(request, user, stem, data, flag_id, error=msg)

    amount = _valid_amount(voucher_amount)
    if amount is None:
        return item_error("Enter a valid voucher amount (positive, max 2 decimals).")
    date = _valid_past_date(voucher_date)
    if date is None:
        return item_error("Enter a valid voucher date (YYYY-MM-DD, not in the future).")
    if not voucher_beat or not voucher_salesman:
        return item_error("Beat and salesman are required.")

    installments = []
    for i, (d, a) in enumerate(zip(inst_date, inst_amount)):
        if str(i) in inst_remove or not d.strip() or not a.strip():
            continue
        row_amount = _valid_amount(a)
        row_date = _valid_past_date(d)
        if row_amount is None or row_date is None:
            return item_error("Every installment row needs a valid date and amount.")
        installments.append({"date": row_date, "amount": row_amount})

    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        resolve_addv_flag(
            data, flag_id,
            {"date": date, "amount": amount, "beat": voucher_beat, "salesman": voucher_salesman},
            installments, user.name, now_str,
        )
    except ValueError as e:
        return item_error(str(e))
    save_report_json(json_path, data)
    return _r(f"/coll/new-vouchers/{stem}/resolve")


# --- Post New Vouchers -------------------------------------------------------

@app.get("/coll/new-vouchers/{stem}/post", response_class=HTMLResponse)
def coll_new_vouchers_post_review(request: Request, stem: str):
    user, err = _require(request, "post_new_vouchers")
    if err:
        return err
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    st = addv_batch_status(data)
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    total_amount = sum(parse_decimal(v.get("amount")) for v in vouchers)
    total_installments = sum(parse_decimal(i.get("amount")) for i in data.get("installments", []))
    return _tmpl("coll/new_vouchers_post.html", request, user=user,
                 stem=stem, data=data, vouchers=vouchers, status=st,
                 total_amount=total_amount, total_installments=total_installments)


@app.post("/coll/new-vouchers/{stem}/post", response_class=HTMLResponse)
def coll_new_vouchers_post_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "post_new_vouchers")
    if err:
        return err
    if action != "post":
        return _r(f"/coll/new-vouchers/{stem}/post")
    json_path, data = _load_addv_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Batch not found.")
    st = addv_batch_status(data)
    if st["status"] != "ready_to_post":
        return _tmpl("error.html", request, user=user,
                     message="This batch isn't ready to post yet — every voucher must be "
                             "reviewed and every raised issue resolved first.")

    vouchers = data.get("vouchers", [])
    installments = data.get("installments", [])
    write_new_vouchers(vouchers)
    write_new_installments(installments)

    data["stages"]["confirm"] = "confirmed"
    data["stages"]["post"] = "confirmed"
    data["stage"] = "finalized"
    save_report_json(json_path, data)
    archive_files([json_path])

    return _redirect_ok("/coll/new-vouchers",
                        f"Posted. {len(vouchers)} voucher(s), {len(installments)} installment(s) written.")


# ---------------------------------------------------------------------------
# Manage Users (web-only, distributor-only)
# ---------------------------------------------------------------------------

@app.get("/manage/users", response_class=HTMLResponse)
def manage_users_list(request: Request):
    user, err = _require(request, "manage_users")
    if err:
        return err
    return _tmpl("manage/users_list.html", request, user=user, users=load_users_admin())


@app.get("/manage/users/new", response_class=HTMLResponse)
def manage_users_new_form(request: Request):
    user, err = _require(request, "manage_users")
    if err:
        return err
    return _tmpl("manage/user_form.html", request, user=user, mode="create",
                 roles=ASSIGNABLE_ROLES, target=None, error=None)


@app.post("/manage/users/new", response_class=HTMLResponse)
def manage_users_new_submit(request: Request,
                            name: str = Form(default=""),
                            role: str = Form(default=""),
                            password: str = Form(default=""),
                            confirm_password: str = Form(default="")):
    user, err = _require(request, "manage_users")
    if err:
        return err
    try:
        create_user(name.strip(), role, password, confirm_password)
    except ValueError as e:
        return _tmpl("manage/user_form.html", request, user=user, mode="create",
                     roles=ASSIGNABLE_ROLES, target={"name": name, "role": role}, error=str(e))
    return _redirect_ok("/manage/users", f"User '{name.strip()}' created.")


@app.get("/manage/users/{name}/edit", response_class=HTMLResponse)
def manage_users_edit_form(request: Request, name: str):
    user, err = _require(request, "manage_users")
    if err:
        return err
    target = load_user(name)
    if target is None:
        return _tmpl("error.html", request, user=user, message=f"User '{name}' not found.")
    return _tmpl("manage/user_form.html", request, user=user, mode="edit",
                 roles=ASSIGNABLE_ROLES, target=target, error=None)


@app.post("/manage/users/{name}/edit", response_class=HTMLResponse)
def manage_users_edit_submit(request: Request, name: str, role: str = Form(default="")):
    user, err = _require(request, "manage_users")
    if err:
        return err
    try:
        update_user_role(name, role)
    except ValueError as e:
        target = load_user(name) or {"name": name, "role": role, "must_change_password": False}
        return _tmpl("manage/user_form.html", request, user=user, mode="edit",
                     roles=ASSIGNABLE_ROLES, target=target, error=str(e))
    return _redirect_ok("/manage/users", f"User '{name}' updated.")


@app.post("/manage/users/{name}/delete", response_class=HTMLResponse)
def manage_users_delete(request: Request, name: str):
    user, err = _require(request, "manage_users")
    if err:
        return err
    try:
        delete_user(name, current_user_name=user.name)
    except ValueError as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    return _redirect_ok("/manage/users", f"User '{name}' deleted.")


@app.get("/manage/users/{name}/reset-password", response_class=HTMLResponse)
def manage_users_reset_password_form(request: Request, name: str):
    user, err = _require(request, "manage_users")
    if err:
        return err
    target = load_user(name)
    if target is None:
        return _tmpl("error.html", request, user=user, message=f"User '{name}' not found.")
    return _tmpl("manage/user_reset_password.html", request, user=user, target=target, error=None)


@app.post("/manage/users/{name}/reset-password", response_class=HTMLResponse)
def manage_users_reset_password_submit(request: Request, name: str,
                                       new_password: str = Form(default=""),
                                       confirm_password: str = Form(default="")):
    user, err = _require(request, "manage_users")
    if err:
        return err
    try:
        reset_user_password(name, new_password, confirm_password)
    except ValueError as e:
        target = load_user(name) or {"name": name}
        return _tmpl("manage/user_reset_password.html", request, user=user, target=target, error=str(e))
    return _redirect_ok("/manage/users",
                        f"Password reset for '{name}' — they must set a new password at next login.")


# ---------------------------------------------------------------------------
# Manage Beats (web-only, distributor-only)
# ---------------------------------------------------------------------------

@app.get("/manage/beats", response_class=HTMLResponse)
def manage_beats_list(request: Request):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    return _tmpl("manage/beats_list.html", request, user=user, beats=load_beats_raw())


@app.get("/manage/beats/new", response_class=HTMLResponse)
def manage_beats_new_form(request: Request):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    return _tmpl("manage/beat_form.html", request, user=user, mode="create",
                 salesmen=load_salesmen(), target=None, error=None)


@app.post("/manage/beats/new", response_class=HTMLResponse)
def manage_beats_new_submit(request: Request,
                            name: str = Form(default=""),
                            salesman: str = Form(default="")):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    try:
        create_beat(name.strip(), salesman)
    except ValueError as e:
        return _tmpl("manage/beat_form.html", request, user=user, mode="create",
                     salesmen=load_salesmen(), target={"name": name, "salesman": salesman}, error=str(e))
    return _redirect_ok("/manage/beats", f"Beat '{name.strip()}' created.")


@app.get("/manage/beats/{name}/edit", response_class=HTMLResponse)
def manage_beats_edit_form(request: Request, name: str):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    target = next((b for b in load_beats_raw() if b["name"] == name), None)
    if target is None:
        return _tmpl("error.html", request, user=user, message=f"Beat '{name}' not found.")
    return _tmpl("manage/beat_form.html", request, user=user, mode="edit",
                 salesmen=load_salesmen(), target=target, error=None)


@app.post("/manage/beats/{name}/edit", response_class=HTMLResponse)
def manage_beats_edit_submit(request: Request, name: str, salesman: str = Form(default="")):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    try:
        update_beat_salesman(name, salesman)
    except ValueError as e:
        target = {"name": name, "salesman": salesman}
        return _tmpl("manage/beat_form.html", request, user=user, mode="edit",
                     salesmen=load_salesmen(), target=target, error=str(e))
    return _redirect_ok("/manage/beats", f"Beat '{name}' updated.")


@app.post("/manage/beats/{name}/delete", response_class=HTMLResponse)
def manage_beats_delete(request: Request, name: str):
    user, err = _require(request, "manage_beats")
    if err:
        return err
    try:
        delete_beat(name)
    except ValueError as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    return _redirect_ok("/manage/beats", f"Beat '{name}' deleted.")


# ---------------------------------------------------------------------------
# Profile (any authenticated user) — self-service password change
# ---------------------------------------------------------------------------

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request, forced: str = ""):
    user, err = _require(request)
    if err:
        return err
    return _profile_page(request, user, forced=bool(forced))


def _profile_page(request, user, forced=False, error=None, sq_error=None):
    """Render /profile. The Secret Question card is distributor-only; the
    current question is shown (never the answer)."""
    current_question = (get_secret_question_for(user.name)
                        if user.role == "distributor" else None)
    return _tmpl("profile.html", request, user=user, forced=forced, error=error,
                 sq_error=sq_error, current_question=current_question)


@app.post("/profile/set-secret-question", response_class=HTMLResponse)
def profile_set_secret_question(request: Request,
                                current_password: str = Form(default=""),
                                secret_question: str = Form(default=""),
                                secret_answer: str = Form(default="")):
    user, err = _require(request)
    if err:
        return err
    try:
        set_secret_question(user.name, current_password, secret_question, secret_answer)
    except ValueError as e:
        return _profile_page(request, user, sq_error=str(e).capitalize() + ".")
    return _redirect_ok("/profile", "Secret question saved.")


@app.post("/profile/change-password", response_class=HTMLResponse)
def profile_change_password(request: Request,
                            current_password: str = Form(default=""),
                            new_password: str = Form(default=""),
                            confirm_password: str = Form(default=""),
                            forced: str = Form(default="")):
    user, err = _require(request)
    if err:
        return err
    try:
        change_own_password(user.name, current_password, new_password, confirm_password)
    except ValueError as e:
        return _profile_page(request, user, forced=bool(forced), error=str(e))
    token = request.cookies.get(_SESSION_COOKIE)
    if token in _sessions:
        _sessions[token] = user._replace(must_change_password=False)
    return _redirect_ok("/profile", "Password changed.")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@app.get("/reports", response_class=HTMLResponse)
def reports_index(request: Request):
    user, err = _require(request)
    if err:
        return err
    return _tmpl("reports/index.html", request, user=user)


@app.get("/reports/salesman", response_class=HTMLResponse)
def reports_salesman(request: Request):
    user, err = _require(request)
    if err:
        return err
    if user.role == "salesman":
        return _r(f"/reports/salesman/{user.name}")
    try:
        salesmen = load_salesmen()
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    return _tmpl("reports/salesman_pick.html", request, user=user, salesmen=salesmen)


@app.get("/reports/salesman/{name}", response_class=HTMLResponse)
def reports_salesman_detail(request: Request, name: str):
    user, err = _require(request)
    if err:
        return err
    if user.role == "salesman" and name != user.name:
        return _tmpl("error.html", request, user=user,
                     message="You can only view your own pending collections.")
    grouped = query_pending_by_salesman(name)
    totals = {beat: sum(Decimal(v["balance"]) for v in vs) for beat, vs in grouped.items()}
    grand = sum(totals.values())
    return _tmpl("reports/salesman.html", request, user=user,
                 salesman=name, grouped=grouped, totals=totals, grand=grand)


@app.get("/reports/beat", response_class=HTMLResponse)
def reports_beat(request: Request):
    user, err = _require(request)
    if err:
        return err
    try:
        beats = load_beats(user)
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    return _tmpl("reports/beat_pick.html", request, user=user, beats=beats)


@app.get("/reports/beat/{name}", response_class=HTMLResponse)
def reports_beat_detail(request: Request, name: str):
    user, err = _require(request)
    if err:
        return err
    if user.role == "salesman" and name not in load_beats(user):
        return _tmpl("error.html", request, user=user,
                     message="You are not assigned to that beat.")
    grouped = query_pending_by_beat(name)
    totals = {sm: sum(Decimal(v["balance"]) for v in vs) for sm, vs in grouped.items()}
    grand = sum(totals.values())
    return _tmpl("reports/beat.html", request, user=user,
                 beat=name, grouped=grouped, totals=totals, grand=grand)


@app.get("/reports/age", response_class=HTMLResponse)
def reports_age(request: Request):
    user, err = _require(request)
    if err:
        return err
    top, total_count = query_pending_by_age(NUMOF_TOP_AGED_VOUCHERS, user)
    return _tmpl("reports/age.html", request, user=user,
                 vouchers=top, total_count=total_count, limit=NUMOF_TOP_AGED_VOUCHERS)


@app.get("/reports/amount", response_class=HTMLResponse)
def reports_amount(request: Request):
    user, err = _require(request)
    if err:
        return err
    top, total_count = query_pending_by_amount(NUMOF_TOP_AMOUNT_VOUCHERS, user)
    return _tmpl("reports/amount.html", request, user=user,
                 vouchers=top, total_count=total_count, limit=NUMOF_TOP_AMOUNT_VOUCHERS)


@app.get("/reports/search", response_class=HTMLResponse)
def reports_search(request: Request, q: str = ""):
    user, err = _require(request)
    if err:
        return err
    result = None
    error = None
    if q.strip():
        result = search_voucher(q.strip())
        if result is None:
            error = f"No voucher found for: {q.strip()}"
    return _tmpl("reports/search.html", request, user=user, q=q, result=result, error=error)
