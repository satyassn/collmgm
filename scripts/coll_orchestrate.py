"""
Shared orchestration layer for the 5-stage collection workflow.

Pure state-transition logic used by BOTH the CLI (coll_workflow.py) and the
web app (coll_api.py), so the two UIs cannot drift on stage-transition rules.

No print()/input() calls. No import of coll_cli (the CLI stays the only
caller of that module). Imports only from coll_store/coll_data, same
constraint already enforced on coll_data.
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Literal, NamedTuple, Optional

from coll_data import _find_any_active_beat_report
from coll_store import (
    STAGING_DIR,
    parse_decimal, load_vouchers_by_bill_nos,
    save_report_json, load_report_json, write_collection_text, bill_no_sort_key,
    ensure_staging_dir, sanitize_filename_component,
    acquire_beat_lock, release_beat_lock, cancel_staging_report,
    acquire_post_claim, release_post_claim,
    _save_installments, _installments_path,
    apply_post_to_db,
    archive_files,
    write_finalize_checkpoint, clear_finalize_checkpoint, read_finalize_checkpoint,
)


class StageError(ValueError):
    """A transition was applied to a report that is not in the required stage.

    Raised by the transition functions below so both UIs reject requests that
    arrive after the report has already moved on — a stale page, a double
    click, or a hand-crafted request that skipped the stage's listing screen.
    """


class ValidationError(ValueError):
    """A report fails validation against current master data: malformed
    voucher entries, payments that are non-numeric/negative/exceeding the
    voucher's current master balance, vouchers missing from master, or
    beat/salesman mismatches.

    Raised at approval so bad data staged by an older/unvalidated client is
    stopped at the supervisor gate instead of flowing on toward the master
    tables. Posting re-checks the same rules as a final backstop.
    """


def _require_stage(report_data, ok, action_desc):
    if not ok:
        s = report_data.get("stages", {})
        raise StageError(
            f"Report cannot be {action_desc} in its current stage "
            f"(start={s.get('start') or '-'}, submit={s.get('submit') or '-'}, "
            f"post={s.get('post') or '-'})."
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
    Requires stages.submit == 'submitted' (StageError otherwise). Approving
    also re-validates the whole report against current master data
    (ValidationError otherwise) so bad or stale staged data stops at the
    supervisor gate — 'return' is deliberately exempt, since returning is
    the remedy for bad data.
    Persists report_data via save_report_json; raises on I/O failure —
    caller decides how to surface it (print vs error.html).
    """
    stages = report_data.get("stages", {})
    _require_stage(report_data, stages.get("submit") == "submitted",
                   "approved" if action == "approve" else "returned")
    if action == "approve":
        errors = validate_staged_report(report_data)
        if errors:
            raise ValidationError(
                "Cannot approve — report failed validation: " + "; ".join(errors)
                + ". Return the report to the salesman for correction.")
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

    Requires stages.start == 'new' (StageError otherwise) — once a list is
    approved or in the submit pipeline it can no longer be approved again,
    returned, or cancelled here.

    'return' and 'cancel' are the same store operation today (delete staging
    + release the beat lock) — kept as two literal values only because the
    two call sites (coll-approve-start vs. coll-start's inline handling of an
    already-pending report) use different wording for the same action, not
    because the state machine actually diverges.
    """
    _require_stage(report_data, report_data.get("stages", {}).get("start") == "new",
                   {"approve": "approved", "return": "returned", "cancel": "cancelled"}[action])
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
    Requires an approved report not already submitted/approved: stages.start ==
    'confirmed' and stages.submit not in ('submitted', 'confirmed') — the same
    filter _load_confirmed_start_reports applies to build the submit listing.
    Raises StageError otherwise, on I/O failure the underlying OSError —
    caller decides how to surface it.
    """
    stages = report_data.get("stages", {})
    _require_stage(report_data,
                   stages.get("start") == "confirmed"
                   and stages.get("submit") not in ("submitted", "confirmed"),
                   "edited")
    _save_installments(report_path, vouchers, bookmark_bill_no=bookmark_bill_no)
    report_data.setdefault("stages", {})["submit"] = "submitted" if submit_for_review else "inprogress"
    report_data["vouchers"] = vouchers
    save_report_json(report_path, report_data)
    if submit_for_review and vouchers:
        write_collection_text(report_path.with_suffix(".txt"), beats or [], salesmen or [],
                              vouchers, stage="submit", status="submitted")
    return report_data


def validate_staged_report(report_data):
    """Cross-check a staged coll report against CURRENT master data.

    Returns a list of error strings (empty = valid). Defense against a
    hand-edited or stale staging JSON: the staged voucher list must be a
    list of dicts each carrying a non-empty bill_no with balance/payment
    keys; every voucher must still exist in the master vouchers table;
    staged beat/salesman must match both the master row and the report
    selection; each payment must validate against the master row's
    CURRENT balance — not the staged copy of the balance; payment_date,
    when set, must be ISO YYYY-MM-DD and not in the future. A master
    balance that fails to parse is itself an error (never skipped).
    Used at approve and post time — entry-time validation can be bypassed
    by an older client or by data staged before validation existed.
    """
    vouchers = report_data.get("vouchers")
    if not isinstance(vouchers, list):
        return ["report: 'vouchers' is not a list"]

    errors = []
    well_formed = []
    for idx, v in enumerate(vouchers, start=1):
        if (not isinstance(v, dict) or not isinstance(v.get("bill_no"), str)
                or not v["bill_no"].strip() or "balance" not in v or "payment" not in v):
            errors.append(f"entry {idx}: malformed voucher")
        else:
            well_formed.append(v)

    seen = set()
    for v in well_formed:
        if v["bill_no"] in seen:
            errors.append(f"{v['bill_no']}: appears more than once in the report")
        seen.add(v["bill_no"])

    sel = report_data.get("selection", [])
    check_selection = report_data.get("selection_type") == "beat_salesman" and len(sel) >= 2
    master = load_vouchers_by_bill_nos([v["bill_no"] for v in well_formed])
    today = datetime.now().date()

    for v in well_formed:
        bill_no = v["bill_no"]
        row = master.get(bill_no)
        if row is None:
            errors.append(f"{bill_no}: not found in master vouchers (deleted or already settled)")
            continue
        if check_selection:
            expected = (sel[0], sel[1])
            if ((v.get("beat"), v.get("salesman")) != expected
                    or (row["beat"], row["salesman"]) != expected):
                errors.append(f"{bill_no}: beat/salesman mismatch with master")
        _, reason = validate_payment(v.get("payment"), row["balance"])
        if reason:
            errors.append(f"{bill_no}: {reason}")
        payment_date = (v.get("payment_date") or "").strip()
        if payment_date:
            try:
                if datetime.strptime(payment_date, "%Y-%m-%d").date() > today:
                    errors.append(f"{bill_no}: payment date '{payment_date}' is in the future")
            except ValueError:
                errors.append(f"{bill_no}: invalid payment date '{payment_date}'")
    return errors


def validate_payment(raw, balance):
    """Validate one payment entry against a voucher balance.

    Returns (normalized, None) on success — empty stays empty, amounts come
    back quantized to 2dp — or (None, reason) on rejection. Mirrors the CLI's
    interactive payment-editor rules in coll_cli: a finite number, not
    negative, at most the outstanding balance.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", None
    # Format gate ahead of the Decimal parse: plain decimal notation only.
    # Rejects letters (incl. scientific-notation e/E), '+', multiple dots,
    # >2 decimal places (never silently rounded), and leading zeros.
    m = re.fullmatch(r"(-?)(\d*)(?:\.(\d*))?", raw)
    if not m or (m.group(2) == "" and not m.group(3)):
        return None, "not a number"
    if m.group(3) and len(m.group(3)) > 2:
        return None, "max 2 decimal places"
    if len(m.group(2)) > 1 and m.group(2).startswith("0"):
        return None, "no leading zeros"
    try:
        amount = Decimal(raw)
        if not amount.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        return None, "not a number"
    if amount < 0:
        return None, "cannot be negative"
    try:
        balance_dec = Decimal(balance or "0")
        if not balance_dec.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return None, "stored balance is invalid — return the report for correction"
    if amount > balance_dec:
        return None, f"exceeds balance ({balance})"
    try:
        return str(amount.quantize(Decimal("0.01"))), None
    except InvalidOperation:
        return None, "not a number"


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


def post_confirmed_report(report_path, posted_by="app"):
    """Checkpointed write-to-DB sequence for a submit-confirmed report.

    posted_by is the audit identity written to installments.created_by —
    pass the logged-in user's name.

    Single-flight: an atomic claim file makes a concurrent post of the same
    report fail fast instead of deducting balances twice, and report_data is
    re-read from disk inside the claim so the stage check (stages.submit ==
    'confirmed', not yet posted) cannot race a concurrent approve/return.
    Payments are re-validated inside the claim as a final backstop before
    anything touches the master tables.

    Steps (checkpointed via write_finalize_checkpoint so a crash mid-sequence
    can be diagnosed): all DB writes (installments + balances + archival of
    fully-settled vouchers) in one transaction via apply_post_to_db -> mark
    stages.post='confirmed' + save -> archive staging files -> release the
    beat lock.

    A DB failure rolls the transaction back completely (checkpoint cleared,
    nothing written). The only remaining at-least-once window is a failure
    AFTER the DB commit — saving the staging JSON — where the checkpoint is
    left at step 4+ so a retry is refused until the operator verifies the
    data and removes it. The returned PostOutcome has ok=False with
    step_failed/error set on any failure. Does not raise — both UIs need to
    render a message either way, so the outcome is always a normal return
    value rather than an exception. Claim and stage failures come back the
    same way, with step_failed=None.
    """
    if not acquire_post_claim(report_path):
        return PostOutcome(False, None,
                           "This report is already being posted by another session.",
                           None, [], Decimal("0"), 0)
    try:
        return _post_claimed_report(report_path, posted_by)
    finally:
        release_post_claim(report_path)


def _post_claimed_report(report_path, posted_by):
    stale = read_finalize_checkpoint()
    if stale and stale.get("report") == str(report_path) and (stale.get("step") or 0) >= 4:
        return PostOutcome(False, None,
                           "A previous post of this report reached the database but did not "
                           "finish. Verify installments/balances manually, then delete "
                           "staging/.finalize_checkpoint.json to proceed.",
                           None, [], Decimal("0"), 0)
    try:
        report_data = load_report_json(report_path)
    except Exception as error:
        return PostOutcome(False, None, f"Could not read report: {error}",
                           None, [], Decimal("0"), 0)
    stages = report_data.get("stages", {}) if isinstance(report_data, dict) else {}
    if stages.get("submit") != "confirmed" or stages.get("post") == "confirmed":
        return PostOutcome(False, None,
                           "Report is not approved for posting — supervisor approval is required first.",
                           None, [], Decimal("0"), 0)

    validation_errors = validate_staged_report(report_data)
    if validation_errors:
        return PostOutcome(False, None,
                           "Cannot post — report failed validation: " + "; ".join(validation_errors)
                           + ". Return the report for correction.",
                           None, [], Decimal("0"), 0)

    vouchers = sorted(report_data["vouchers"], key=lambda v: bill_no_sort_key(v["bill_no"]))
    report_data["vouchers"] = vouchers
    beat = report_data.get("selection", [None])[0]

    write_finalize_checkpoint(report_path, 1)
    try:
        completed_bill_nos = apply_post_to_db(vouchers, created_by=posted_by)
        write_finalize_checkpoint(report_path, 4)
    except Exception as error:
        # The transaction rolled back — nothing was written, so the
        # checkpoint has nothing left to diagnose.
        clear_finalize_checkpoint()
        return PostOutcome(False, 1, f"{error} (no changes were written)",
                           None, [], Decimal("0"), 0)

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

    total_collected = sum(parse_decimal(v.get("payment")) for v in vouchers)
    paid_count = sum(1 for v in vouchers if parse_decimal(v.get("payment")) > 0)

    return PostOutcome(True, None, None, archive_warning, completed_bill_nos, total_collected, paid_count)


def return_post_stage(report_path, report_data):
    """Send a submit-confirmed report back to the supervisor for re-approval.

    Requires stages.submit == 'confirmed' and not yet posted (StageError otherwise).
    """
    stages = report_data.get("stages", {})
    _require_stage(report_data,
                   stages.get("submit") == "confirmed" and stages.get("post") != "confirmed",
                   "returned")
    report_data.setdefault("stages", {})["submit"] = "submitted"
    save_report_json(report_path, report_data)
    return report_data
