"""
FastAPI web application for CollMgm.

LAN-hosted, browser-based interface. Serves on 0.0.0.0:8100.
Run via:  run_server.bat
       or uvicorn scripts.coll_api:app --host 0.0.0.0 --port 8100 --reload
"""

import json
import secrets
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from coll_orchestrate import (
    prepare_submit_review, apply_submit_approval,
    ActiveReportState, check_active_beat_report, generate_collection_list, apply_start_approval,
    compute_payment_dates, record_submit_payments,
    post_confirmed_report, return_post_stage,
)
from coll_store import (
    STAGING_DIR,
    _load_installments,
    _load_pending_start_reports,
    _load_pending_submit_reports,
    bill_no_sort_key,
    cancel_staging_report,
    ensure_db,
    load_permissions,
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
                 beat=beat, salesman=salesman, vouchers=vouchers,
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
                        report_stem: str = Form(default=""),
                        beat: str = Form(default="")):
    user, err = _require(request, "coll_start")
    if err:
        return err
    json_path = STAGING_DIR / f"{report_stem}.json"
    if action == "cancel":
        if user.role == "salesman":
            if not json_path.exists():
                return _tmpl("error.html", request, user=user, message="Report not found.")
            data = json.loads(json_path.read_text(encoding="utf-8"))
            sel = data.get("selection", [])
            if len(sel) < 2 or sel[1] != user.name:
                return _tmpl("error.html", request, user=user, message="Report not found.")
        cancel_staging_report(json_path, beat.strip() or None)
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


@app.get("/coll/approve-start/{stem}", response_class=HTMLResponse)
def coll_approve_start_review(request: Request, stem: str):
    user, err = _require(request, "coll_approve_start")
    if err:
        return err
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    sel = data.get("selection", [])
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    total = sum(Decimal(v.get("balance", "0") or "0") for v in vouchers)
    return _tmpl("coll/approve_start_review.html", request, user=user,
                 stem=stem, data=data, vouchers=vouchers,
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_balance=total)


@app.post("/coll/approve-start/{stem}", response_class=HTMLResponse)
def coll_approve_start_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "coll_approve_start")
    if err:
        return err
    if action not in ("approve", "return", "cancel"):
        return _r("/coll/approve-start")
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    apply_start_approval(json_path, data, action)

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
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    sel = data.get("selection", [])
    if user.role == "salesman" and (len(sel) < 2 or sel[1] != user.name):
        return _tmpl("error.html", request, user=user, message="Report not found.")
    stages = data.get("stages", {})
    if stages.get("submit") == "submitted":
        return _tmpl("error.html", request, user=user,
                     message="This report is already submitted — awaiting supervisor approval.")
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    installments, _ = _load_installments(json_path)
    for v in vouchers:
        entry = installments.get(v["bill_no"])
        if entry:
            v["payment"] = entry.get("payment", "")
            v["payment_date"] = entry.get("date", "")
    total_collected = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    paid_count = sum(1 for v in vouchers if Decimal(v.get("payment", "0") or "0") > 0)
    return _tmpl("coll/submit_edit.html", request, user=user,
                 stem=stem, data=data, vouchers=vouchers,
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_collected=total_collected, paid_count=paid_count)


@app.post("/coll/submit/{stem}", response_class=HTMLResponse)
async def coll_submit_save(request: Request, stem: str):
    user, err = _require(request, "coll_submit")
    if err:
        return err
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    sel = data.get("selection", [])
    if user.role == "salesman" and (len(sel) < 2 or sel[1] != user.name):
        return _tmpl("error.html", request, user=user, message="Report not found.")

    form = await request.form()
    action = (form.get("action") or "save").strip()

    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    for v in vouchers:
        v["payment"] = (form.get(f"pay_{v['bill_no']}") or "").strip()

    prior_installments, _ = _load_installments(json_path)
    compute_payment_dates(vouchers, prior_installments)

    beat = sel[0] if sel else ""
    salesman = sel[1] if len(sel) > 1 else ""

    record_submit_payments(json_path, data, vouchers, submit_for_review=(action == "submit"),
                           beats=[beat] if beat else [], salesmen=[salesman] if salesman else [])

    if action == "submit":
        return _tmpl("message.html", request, user=user,
                     message="Collections submitted for supervisor review.", back="/coll/submit")

    return _tmpl("message.html", request, user=user,
                 message="Progress saved.", back=f"/coll/submit/{stem}")


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


@app.get("/coll/approve-submit/{stem}", response_class=HTMLResponse)
def coll_approve_submit_review(request: Request, stem: str):
    user, err = _require(request, "coll_approve_submit")
    if err:
        return err
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data = prepare_submit_review(json_path, data)
    vouchers = data["vouchers"]
    sel = data.get("selection", [])
    total_collected = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    paid_count = sum(1 for v in vouchers if Decimal(v.get("payment", "0") or "0") > 0)
    return _tmpl("coll/approve_submit_review.html", request, user=user,
                 stem=stem, data=data, vouchers=vouchers,
                 beat=sel[0] if sel else "",
                 salesman=sel[1] if len(sel) > 1 else "",
                 total_collected=total_collected, paid_count=paid_count)


@app.post("/coll/approve-submit/{stem}", response_class=HTMLResponse)
def coll_approve_submit_action(request: Request, stem: str, action: str = Form(default="")):
    user, err = _require(request, "coll_approve_submit")
    if err:
        return err
    if action not in ("approve", "return"):
        return _r("/coll/approve-submit")
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    apply_submit_approval(json_path, data, action)

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
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    sel = data.get("selection", [])
    vouchers = sorted(data.get("vouchers", []), key=lambda v: bill_no_sort_key(v["bill_no"]))
    total_collected = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    paid_count = sum(1 for v in vouchers if Decimal(v.get("payment", "0") or "0") > 0)
    return _tmpl("coll/post_review.html", request, user=user,
                 stem=stem, data=data, vouchers=vouchers,
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
    json_path = STAGING_DIR / f"{stem}.json"
    if not json_path.exists():
        return _tmpl("error.html", request, user=user, message="Report not found.")
    data = json.loads(json_path.read_text(encoding="utf-8"))

    if action == "return":
        return_post_stage(json_path, data)
        return _tmpl("message.html", request, user=user,
                     message="Returned to supervisor for re-approval.", back="/coll/post")

    outcome = post_confirmed_report(json_path, data)
    if not outcome.ok:
        step_note = f" at step {outcome.step_failed}" if outcome.step_failed else ""
        return _tmpl("error.html", request, user=user,
                     message=f"Post failed{step_note}: {outcome.error}. "
                              "A checkpoint remains — check data before retrying.")

    return _tmpl("message.html", request, user=user,
                 message=f"Posted. {outcome.paid_count} vouchers collected. Total: {outcome.total_collected}",
                 back="/menu")


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
