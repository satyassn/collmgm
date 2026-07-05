"""
Shared orchestration layer for the 5-stage collection workflow.

Pure state-transition logic used by BOTH the CLI (coll_workflow.py) and the
web app (coll_api.py), so the two UIs cannot drift on stage-transition rules.

No print()/input() calls. No import of coll_cli (the CLI stays the only
caller of that module). Imports only from coll_store/coll_data, same
constraint already enforced on coll_data.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, NamedTuple, Optional

from coll_data import _find_any_active_beat_report
from coll_store import (
    STAGING_DIR,
    save_report_json, write_collection_text, bill_no_sort_key,
    ensure_staging_dir, sanitize_filename_component,
    acquire_beat_lock, release_beat_lock, cancel_staging_report,
    _save_installments, _installments_path,
    _append_installments_csv, _update_vouchers_balance, _archive_completed,
    archive_files,
    write_finalize_checkpoint, clear_finalize_checkpoint, read_finalize_checkpoint,
)


def prepare_submit_review(report_path, report_data):
    """Sort vouchers by bill_no and best-effort regenerate the submit/submitted
    TXT sidecar ahead of a supervisor reviewing a submit-stage report.

    TXT regeneration failure is swallowed (matches CLI's existing
    try/except pass) — the review can proceed from report_data alone.
    """
    report_data["vouchers"] = sorted(report_data["vouchers"], key=lambda v: bill_no_sort_key(v["bill_no"]))
    sel = report_data.get("selection", [])
    beat = sel[0] if len(sel) > 0 else ""
    salesman = sel[1] if len(sel) > 1 else ""
    try:
        write_collection_text(report_path.with_suffix(".txt"), [beat], [salesman],
                              report_data["vouchers"], stage="submit", status="submitted")
    except Exception:
        pass
    return report_data


def apply_submit_approval(report_path, report_data, action: Literal["approve", "return"]):
    """Approve or return a submit-confirmed-pending report.

    'approve' -> stages.submit = 'confirmed' (ready to post).
    'return'  -> stages.submit = 'returned' (salesman must revise).
    Persists report_data via save_report_json; raises on I/O failure —
    caller decides how to surface it (print vs error.html).
    """
    report_data.setdefault("stages", {})["submit"] = "confirmed" if action == "approve" else "returned"
    save_report_json(report_path, report_data)
    return report_data


# ---------------------------------------------------------------------------
# coll-start / coll-approve-start
# ---------------------------------------------------------------------------

class ActiveReportState(str, Enum):
    NONE = "none"                          # no active report for this beat/salesman
    PENDING_START = "pending_start"         # stages.start == 'new' — offer approve/cancel inline
    START_CONFIRMED = "start_confirmed"     # approved, not yet submitted — read-only block
    IN_SUBMIT_PIPELINE = "in_submit_pipeline"  # submit in (submitted, inprogress, confirmed) — read-only block


def check_active_beat_report(selection_type, selection_values):
    """Classify any existing active staging report for this beat/salesman selection.

    Returns (ActiveReportState, report_path_or_None, report_data_or_None).
    """
    existing = _find_any_active_beat_report(selection_type, selection_values)
    if not existing:
        return ActiveReportState.NONE, None, None
    path, data = existing
    stages = data.get("stages", {})
    start_confirmed = stages.get("start") == "confirmed"
    in_submit_pipeline = stages.get("submit") in ("submitted", "inprogress", "confirmed")
    if in_submit_pipeline:
        return ActiveReportState.IN_SUBMIT_PIPELINE, path, data
    if start_confirmed:
        return ActiveReportState.START_CONFIRMED, path, data
    return ActiveReportState.PENDING_START, path, data


class GenerateOutcome(NamedTuple):
    ok: bool
    reason: Optional[Literal["lock_conflict", "write_error"]]
    error: Optional[str]
    json_path: Optional[object]
    txt_path: Optional[object]
    vouchers: list


def generate_collection_list(beat, salesman, vouchers):
    """Acquire the beat lock and write a new start-stage staging report.

    Precondition (caller's responsibility): `vouchers` is non-empty and
    check_active_beat_report() returned NONE for this beat/salesman.
    """
    if not acquire_beat_lock(beat):
        return GenerateOutcome(False, "lock_conflict", None, None, None, vouchers)

    ensure_staging_dir()
    timestamp = datetime.now().strftime("%Y%m%d")
    safe_selection = "_".join(sanitize_filename_component(v) for v in (beat, salesman))
    base_name = f"coll{timestamp}-beat_salesman-{safe_selection}"
    json_path = STAGING_DIR / f"{base_name}.json"
    txt_path = STAGING_DIR / f"{base_name}.txt"

    report_data = {
        "stages": {"start": "new", "submit": "", "post": ""},
        "selection_type": "beat_salesman",
        "selection": [beat, salesman],
        "date": datetime.now().strftime("%Y-%m-%d"),
        "vouchers": vouchers,
    }
    try:
        save_report_json(json_path, report_data)
        write_collection_text(txt_path, [beat], [salesman], vouchers, stage="start", status="new")
    except Exception as error:
        release_beat_lock(beat)
        return GenerateOutcome(False, "write_error", str(error), None, None, vouchers)

    return GenerateOutcome(True, None, None, json_path, txt_path, vouchers)


def approve_start_stage(report_path, report_data):
    """Mark stages.start='confirmed', regenerate the confirmed-status TXT, persist JSON.

    TXT regeneration is skipped for an empty voucher list (write_collection_text
    raises ValueError on empty input) — matches the more defensive of the two
    pre-unification implementations rather than letting that edge case surface
    as an unhandled error on the path that didn't previously guard against it.
    """
    report_data.setdefault("stages", {})["start"] = "confirmed"
    sel = report_data.get("selection", [])
    beat = sel[0] if len(sel) > 0 else ""
    salesman = sel[1] if len(sel) > 1 else ""
    save_report_json(report_path, report_data)
    vouchers = report_data.get("vouchers", [])
    if vouchers:
        write_collection_text(report_path.with_suffix(".txt"),
                              [beat] if beat else [], [salesman] if salesman else [],
                              vouchers, stage="start", status="confirmed")
    return report_data


def apply_start_approval(report_path, report_data, action: Literal["approve", "return", "cancel"]):
    """Approve, return, or cancel a start-stage (pending-approval) report.

    'return' and 'cancel' are the same store operation today (delete staging
    + release the beat lock) — kept as two literal values only because the
    two call sites (coll-approve-start vs. coll-start's inline handling of an
    already-pending report) use different wording for the same action, not
    because the state machine actually diverges.
    """
    if action == "approve":
        return approve_start_stage(report_path, report_data)
    beat_name = report_data.get("selection", [None])[0]
    cancel_staging_report(report_path, beat_name)
    return report_data


# ---------------------------------------------------------------------------
# coll-submit
# ---------------------------------------------------------------------------

def compute_payment_dates(vouchers, prior_installments, today=None):
    """Set payment_date on each voucher based on its (possibly edited) payment.

    Empty payment -> payment_date cleared. Non-empty payment identical to what
    was recorded before -> keep the original date (an unchanged re-edit).
    Otherwise -> stamp `today`. Mutates and returns `vouchers`.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    for v in vouchers:
        payment = (v.get("payment") or "").strip()
        if not payment:
            v["payment_date"] = ""
            continue
        prior = prior_installments.get(v["bill_no"])
        if prior and (prior.get("payment") or "").strip() == payment:
            v["payment_date"] = prior.get("date") or today
        else:
            v["payment_date"] = today
    return vouchers


def record_submit_payments(report_path, report_data, vouchers, submit_for_review,
                            beats=None, salesmen=None, bookmark_bill_no=None):
    """Persist the installments sidecar + report_data['vouchers'], and advance
    stages.submit.

    submit_for_review=False -> stages.submit = 'inprogress' (mid-session save/quit).
    submit_for_review=True  -> stages.submit = 'submitted' (ready for supervisor
                                review); regenerates the submitted-status TXT
                                sidecar (skipped if `vouchers` is empty, matching
                                the more defensive of the two pre-unification
                                implementations — see approve_start_stage).
    Raises on I/O failure — caller decides how to surface it.
    """
    _save_installments(report_path, vouchers, bookmark_bill_no=bookmark_bill_no)
    report_data.setdefault("stages", {})["submit"] = "submitted" if submit_for_review else "inprogress"
    report_data["vouchers"] = vouchers
    save_report_json(report_path, report_data)
    if submit_for_review and vouchers:
        write_collection_text(report_path.with_suffix(".txt"), beats or [], salesmen or [],
                              vouchers, stage="submit", status="submitted")
    return report_data


# ---------------------------------------------------------------------------
# coll-post
# ---------------------------------------------------------------------------

class PostOutcome(NamedTuple):
    ok: bool
    step_failed: Optional[int]
    error: Optional[str]
    archive_warning: Optional[str]
    completed_bill_nos: list
    total_collected: Decimal
    paid_count: int


def post_confirmed_report(report_path, report_data):
    """Checkpointed write-to-DB sequence for a submit-confirmed report.

    Steps (checkpointed via write_finalize_checkpoint so a crash mid-sequence
    can be diagnosed): append installments -> update voucher balances ->
    archive fully-settled vouchers -> mark stages.post='confirmed' + save ->
    archive staging files -> release the beat lock.

    On failure, the checkpoint file is left in place (no rollback — matches
    the existing at-least-once semantics both UIs already relied on) and the
    returned PostOutcome has ok=False with step_failed/error set. Does not
    raise — both UIs need to render a message either way, so the outcome is
    always a normal return value rather than an exception.
    """
    vouchers = sorted(report_data["vouchers"], key=lambda v: bill_no_sort_key(v["bill_no"]))
    report_data["vouchers"] = vouchers
    beat = report_data.get("selection", [None])[0]

    write_finalize_checkpoint(report_path, 1)
    try:
        _append_installments_csv(vouchers)
        write_finalize_checkpoint(report_path, 2)
        completed_bill_nos = _update_vouchers_balance(vouchers)
        write_finalize_checkpoint(report_path, 3)
        if completed_bill_nos:
            _archive_completed(completed_bill_nos)
        write_finalize_checkpoint(report_path, 4)
    except Exception as error:
        checkpoint = read_finalize_checkpoint()
        step_failed = checkpoint.get("step") if checkpoint else None
        return PostOutcome(False, step_failed, str(error), None, [], Decimal("0"), 0)

    report_data.setdefault("stages", {})["post"] = "confirmed"
    try:
        save_report_json(report_path, report_data)
        write_finalize_checkpoint(report_path, 5)
    except Exception as error:
        return PostOutcome(False, 5, str(error), None, completed_bill_nos, Decimal("0"), 0)

    archive_warning = None
    try:
        archive_files([report_path, _installments_path(report_path), report_path.with_suffix(".txt")])
    except Exception as error:
        archive_warning = str(error)

    clear_finalize_checkpoint()
    if beat:
        release_beat_lock(beat)

    total_collected = sum(Decimal(v.get("payment", "0") or "0") for v in vouchers)
    paid_count = sum(1 for v in vouchers if Decimal(v.get("payment", "0") or "0") > 0)

    return PostOutcome(True, None, None, archive_warning, completed_bill_nos, total_collected, paid_count)


def return_post_stage(report_path, report_data):
    """Send a submit-confirmed report back to the supervisor for re-approval."""
    report_data.setdefault("stages", {})["submit"] = "submitted"
    save_report_json(report_path, report_data)
    return report_data
