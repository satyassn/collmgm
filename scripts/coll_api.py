"""
FastAPI web application for CollMgm.

LAN-hosted, browser-based interface. Serves on 0.0.0.0:8100.
Run via:  run_server.bat
       or uvicorn scripts.coll_api:app --host 0.0.0.0 --port 8100 --reload
"""

import re
import secrets
import sys
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from coll_orchestrate import (
    StageError, ValidationError,
    prepare_submit_review, apply_submit_approval,
    ActiveReportState, check_active_beat_report, generate_collection_list, apply_start_approval,
    set_start_verification, is_start_verification_complete,
    set_submit_verification, is_submit_verification_complete,
    apply_correction_request, _find_active_report_for_bill,
    compute_payment_dates, record_submit_payments, validate_payment,
    validate_staged_report, format_submit_validation_errors,
    post_confirmed_report, return_post_stage,
)
from coll_store import (
    STAGING_DIR,
    CorrectionConflict,
    _load_installments,
    _load_pending_start_reports,
    _load_pending_submit_reports,
    bill_no_sort_key,
    build_print_collection_html,
    cancel_staging_report,
    ensure_db,
    insert_correction,
    list_staging_reports,
    load_correction,
    load_corrections,
    load_permissions,
    load_report_json,
    open_corrections_for_bills,
    parse_decimal,
    read_finalize_checkpoint,
    verify_user,
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


def _tmpl(name: str, request: Request, **ctx):
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


def _require(request: Request, permission: str = None):
    """Return (user, None) if authorised; (None, redirect/error response) otherwise."""
    user = _get_user(request)
    if not user:
        return None, _r("/login")
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
    return _tmpl("login.html", request)


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request,
               username: str = Form(default=""),
               password: str = Form(default="")):
    user = verify_user(username.strip(), password)
    if not user:
        return _tmpl("login.html", request, error="Invalid username or password.")
    resp = _r("/menu")
    _set_session(resp, user)
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = _r("/login")
    _clear_session(request, resp)
    return resp


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
    return _tmpl("menu.html", request, user=user, perms=role_perms)


# ---------------------------------------------------------------------------
# Generate Collection List  (coll-start)
# ---------------------------------------------------------------------------

@app.get("/coll/start", response_class=HTMLResponse)
def coll_start(request: Request):
    user, err = _require(request, "coll_start")
    if err:
        return err
    try:
        beats = load_beats(user)
        summary = load_beats_pending_summary(user)
        active = load_active_beat_statuses()
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    # Beats already locked by an in-flight report can't be generated again
    # (see the beat-lock rule) — push them to the bottom and disable them
    # in the template instead of listing them alongside selectable beats.
    beats = sorted(beats, key=lambda b: (b in active, b))
    return _tmpl("coll/start_beat.html", request, user=user,
                 beats=beats, summary=summary, active=active)


def _generate_collection_list_response(request, user, beat, salesman):
    """Create the staging report and render the Keep/Cancel preview.

    Shared by the explicit salesman-picker step and the auto-skip path used
    when a beat has only one possible salesman (always true for a salesman
    generating their own list, since RBAC restricts them to assigned beats).
    """
    selection_type = "beat_salesman"
    selection_values = [beat, salesman]

    state, _existing_path, _existing_data = check_active_beat_report(selection_type, selection_values)
    if state != ActiveReportState.NONE:
        return _tmpl("error.html", request, user=user,
                     message=f"An active collection already exists for {beat} / {salesman}. "
                              "Complete or cancel it before starting a new one.")

    vouchers = _load_vouchers_by_criterion(selection_type, selection_values, user)
    if not vouchers:
        return _tmpl("error.html", request, user=user,
                     message=f"No pending vouchers for {beat} / {salesman}.")

    outcome = generate_collection_list(beat, salesman, vouchers)
    if not outcome.ok:
        if outcome.reason == "lock_conflict":
            return _tmpl("error.html", request, user=user,
                         message=f"Beat '{beat}' is currently locked. Please retry later.")
        return _tmpl("error.html", request, user=user, message=f"Failed to create report: {outcome.error}")

    total = sum(Decimal(v["balance"]) for v in vouchers)
    return _tmpl("coll/start_preview.html", request, user=user,
                 beat=beat, salesman=salesman, vouchers=_enrich_vouchers(vouchers),
                 report_stem=outcome.json_path.stem, total_balance=total)


@app.post("/coll/start/beat", response_class=HTMLResponse)
def coll_start_pick_beat(request: Request, beat: str = Form(default="")):
    user, err = _require(request, "coll_start")
    if err:
        return err
    beat = beat.strip()
    if not beat:
        return _r("/coll/start")

    if user.role == "salesman" and beat not in load_beats(user):
        return _tmpl("error.html", request, user=user,
                     message="You are not assigned to that beat.")

    try:
        beat_vouchers = _load_vouchers_by_criterion("beat", [beat], user)
    except Exception as e:
        return _tmpl("error.html", request, user=user, message=str(e))
    if not beat_vouchers:
        try:
            beats = load_beats(user)
            summary = load_beats_pending_summary(user)
            active = load_active_beat_statuses()
            beats = sorted(beats, key=lambda b: (b in active, b))
        except Exception:
            beats, summary, active = [], {}, {}
        return _tmpl("coll/start_beat.html", request, user=user,
                     beats=beats, summary=summary, active=active,
                     error=f"No pending vouchers for beat: {beat}")
    if user.role == "salesman":
        salesmen = [user.name] if user.name in {v["salesman"] for v in beat_vouchers} else []
    else:
        salesmen = sorted({v["salesman"] for v in beat_vouchers})

    if len(salesmen) == 1:
        return _generate_collection_list_response(request, user, beat, salesmen[0])

    counts = {sm: sum(1 for v in beat_vouchers if v["salesman"] == sm) for sm in salesmen}
    return _tmpl("coll/start_salesman.html", request, user=user,
                 beat=beat, salesmen=salesmen, counts=counts)


@app.post("/coll/start/generate", response_class=HTMLResponse)
def coll_start_generate(request: Request,
                         beat: str = Form(default=""),
                         salesman: str = Form(default="")):
    user, err = _require(request, "coll_start")
    if err:
        return err
    beat, salesman = beat.strip(), salesman.strip()
    if not beat or not salesman:
        return _r("/coll/start")

    if user.role == "salesman":
        if salesman != user.name:
            return _tmpl("error.html", request, user=user,
                         message="You can only generate a collection list for yourself.")
        if beat not in load_beats(user):
            return _tmpl("error.html", request, user=user,
                         message="You are not assigned to that beat.")

    return _generate_collection_list_response(request, user, beat, salesman)


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
        if user.role == "salesman" and (len(sel) < 2 or sel[1] != user.name):
            return _tmpl("error.html", request, user=user, message="Report not found.")
        if data.get("stages", {}).get("start") != "new":
            return _tmpl("error.html", request, user=user,
                         message="This collection list has already been approved — "
                                 "it can no longer be cancelled here.")
        # Beat comes from the report itself, not the form — a forged beat value
        # must not release another beat's lock.
        cancel_staging_report(json_path, sel[0] if sel else None)
        return _tmpl("message.html", request, user=user,
                     message="Collection list cancelled.", back="/menu")
    return _tmpl("message.html", request, user=user,
                 message="Collection list saved — awaiting supervisor approval.", back="/menu")


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
    return _tmpl("coll/approve_start_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
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
        return _tmpl("message.html", request, user=user, message=msg, back="/coll/approve-start")
    return _tmpl("message.html", request, user=user,
                 message="Collection list approved.", back="/coll/approve-start")


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
                     if d.get("selection", [None, None])[1] == user.name]
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
    if user.role == "salesman" and (len(sel) < 2 or sel[1] != user.name):
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
    total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
    paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
    return _tmpl("coll/submit_edit.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_collected=total_collected, paid_count=paid_count)


@app.post("/coll/submit/{stem}", response_class=HTMLResponse)
async def coll_submit_save(request: Request, stem: str):
    user, err = _require(request, "coll_submit")
    if err:
        return err
    json_path, data = _load_staging_report(stem)
    if json_path is None:
        return _tmpl("error.html", request, user=user, message="Report not found.")
    sel = data.get("selection", [])
    if user.role == "salesman" and (len(sel) < 2 or sel[1] != user.name):
        return _tmpl("error.html", request, user=user, message="Report not found.")

    form = await request.form()
    action = (form.get("action") or "save").strip()

    beat = sel[0] if sel else ""
    salesman = sel[1] if len(sel) > 1 else ""

    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    invalid = 0
    for v in vouchers:
        raw = (form.get(f"pay_{v['bill_no']}") or "").strip()
        normalized, reason = validate_payment(raw, v.get("balance"))
        if reason:
            invalid += 1
            v["error"] = reason  # template renders an inline bubble on this row
            v["payment"] = raw  # keep what was typed so the form re-renders with it
        else:
            v["payment"] = normalized
    if invalid:
        total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
        paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)
        return _tmpl("coll/submit_edit.html", request, user=user,
                     stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                     beat=beat, salesman=salesman,
                     total_collected=total_collected, paid_count=paid_count,
                     error=f"Nothing saved — {invalid} payment(s) need correction")

    prior_installments, _ = _load_installments(json_path)
    compute_payment_dates(vouchers, prior_installments)

    try:
        record_submit_payments(json_path, data, vouchers, submit_for_review=(action == "submit"),
                               beats=[beat] if beat else [], salesmen=[salesman] if salesman else [])
    except StageError as e:
        return _tmpl("error.html", request, user=user, message=str(e))

    if action == "submit":
        return _tmpl("message.html", request, user=user,
                     message="Collections submitted for supervisor review.", back="/coll/submit")

    return _tmpl("message.html", request, user=user,
                 message="Progress saved.", back="/menu")


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
    return _tmpl("coll/approve_submit_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_collected=total_collected, paid_count=paid_count,
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
        return _tmpl("message.html", request, user=user,
                     message="Collections returned to salesman for revision.",
                     back="/coll/approve-submit")
    return _tmpl("message.html", request, user=user,
                 message="Collections approved — ready to post.", back="/coll/approve-submit")


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
    return _tmpl("coll/post_review.html", request, user=user,
                 stem=stem, data=data, vouchers=_enrich_vouchers(vouchers),
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_collected=total_collected, paid_count=paid_count)


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
        return _tmpl("message.html", request, user=user,
                     message="Returned to supervisor for re-approval.", back="/coll/post")

    outcome = post_confirmed_report(json_path, posted_by=user.name)
    if not outcome.ok:
        if outcome.step_failed:
            message = (f"Post failed at step {outcome.step_failed}: {outcome.error}. "
                       "A checkpoint remains — check data before retrying.")
        else:
            message = f"Post failed: {outcome.error}"
        return _tmpl("error.html", request, user=user, message=message)

    return _tmpl("message.html", request, user=user,
                 message=f"Posted. {outcome.paid_count} vouchers collected. Total: {outcome.total_collected}",
                 back="/menu")


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
    # Inline expand gets the slim installments-only partial; the standalone
    # page (and Voucher Search's include) keep the full card.
    template = "_voucher_inline.html" if fragment else "voucher.html"
    return _tmpl(template, request, user=user,
                 voucher=voucher, installments=installments, is_completed=is_completed,
                 correct_from=correct_from, can_raise=can_raise)


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
    return _tmpl("coll/correction_form.html", request, user=user,
                 voucher=voucher, installments=installments, back=back,
                 kinds=kinds, kind_labels=_KIND_LABELS,
                 staged_payment=staged_payment,
                 existing=existing, error=error)


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
def coll_correction_review(request: Request, cid: int):
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
    return _tmpl("coll/correction_review.html", request, user=user,
                 corr=corr, voucher=voucher, installments=installments,
                 staged_payment=staged_payment,
                 kinds=_KIND_LABELS, can_apply=_can_act_on(corr, user))


@app.post("/coll/corrections/{cid}", response_class=HTMLResponse)
def coll_correction_action(request: Request, cid: int,
                           action: str = Form(default=""),
                           resolution_note: str = Form(default="")):
    # Resolution authority is per kind (_can_act_on), not one permission key:
    # supervisors may resolve collection_amount requests but not master kinds.
    user, err = _require(request)
    if err:
        return err
    corr = load_correction(cid)
    if corr is None:
        return _tmpl("error.html", request, user=user, message="Correction request not found.")
    if not _can_act_on(corr, user):
        return _tmpl("error.html", request, user=user,
                     message="You don't have permission for this action.")
    if action not in ("apply", "reject"):
        return _r("/coll/corrections")
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
    return _tmpl("message.html", request, user=user, message=msg, back="/coll/corrections")


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
