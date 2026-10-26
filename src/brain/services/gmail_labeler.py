"""The Gmail SB/ labeler (v1, plan approved 25 Oct): RULES ONLY.

Privacy posture (the approved decision): Jev and every external LLM are OUT —
classification is deterministic over headers only:
- Kids: the message SUBJECT must BE a confirmed kid name/alias, full token
  (``brain.services.triage.is_name_token`` — the same zero-wrong-child matcher
  audience resolution uses; a classmate's full name or a substring never
  matches). Kid names come from confirmed memory at runtime, never hardcoded.
- School/Logistics/Bills/Noise: sender domain or List-Id patterns from a small
  rules file (Settings.gmail_labeler_rules_path; empty default — no personal
  domains are committed).
- Anything else: SB/Unsorted. Precedence: Kids > School > Logistics > Bills >
  Noise > Unsorted; a message gets ONE SB label.

Safety rails:
- dry-run is the DEFAULT: planning performs ZERO write calls (ids + metadata
  GETs only); ``--apply`` is explicit and capped (``max_apply``, hard default
  20 per run).
- label-only: the labeler adds SB/ labels and never removes INBOX, never
  archives/trashes/deletes. v1 creates NO Gmail filters (gmail.settings.basic
  never enters the scope set).
- consent gate: label writes need gmail.modify. The FIRST write-capable call
  (label creation) happens before any message is modified; a 403 there aborts
  the run with a re-consent message — zero message writes.
- audit + undo: every applied label is a row in a DURABLE append-only JSONL
  ledger (0600, under the app data dir — NOT ops_events, which is pruned
  after 7 days and would silently kill undo). ``undo_batch`` removes exactly
  the label ids its batch added — user labels are untouched.
- idempotent: a message already carrying ANY SB/ label is skipped.

Message selection: the same scope the email connector syncs (the configured
gmail label + query), so the labeler can never reach outside the pipeline's
scope. The scan is bounded (``max_scan``, default 100) — each metadata GET
costs ~5 quota units, paced by the client.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brain.ai.gmail import GmailAuthError, GmailError
from brain.services.triage import is_name_token

MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def token_grants_modify(
    refresh_token: str,
    *,
    client_config: dict[str, str],
    poster: Any = None,
) -> bool:
    """True when the token's GRANTED scopes include gmail.modify.

    Refreshes the token ONCE against the OAuth token endpoint (stdlib POST —
    no google libs, no Gmail API call, no quota) and inspects the granted
    scope list. The check runs before ANY Gmail call, so a readonly-only
    token (the live state until the USER runs `brain gmail-label --auth`)
    fails with zero writes. ``poster(form) -> payload`` is the test seam.
    """
    import urllib.parse
    import urllib.request

    form = urllib.parse.urlencode(
        {
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()

    def _default_post(body: bytes) -> dict[str, Any]:
        req = urllib.request.Request(
            client_config["token_uri"],
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed URL)
            return json.loads(resp.read())

    try:
        payload = (poster or _default_post)(form)
    except Exception as exc:  # HTTPError 400 invalid_grant, URLError, OSError…
        raise GmailAuthError(
            "could not verify the token's granted scopes (the token may be "
            "revoked or expired) — run `brain gmail-label --auth` to re-consent",
        ) from exc
    granted = str(payload.get("scope") or "").split(" ")
    return MODIFY_SCOPE in granted

logger = logging.getLogger(__name__)

SB_PREFIX = "SB/"
UNSORTED = "SB/Unsorted"
# Precedence (high → low). Kids is memory-driven; the rest come from the
# rules file. A message gets exactly ONE label — the first rule that wins.
LABEL_ORDER = ("Kids", "School", "Logistics", "Bills", "Noise")

COMPONENT = "gmail-label"

# Hard per-run ceiling on applied labels (review MAJOR 1): --max-apply used
# to accept -1 as 'unlimited'. The CLI rejects < 1; even a huge value can
# never write more than this many labels in one run.
MAX_APPLY_CEILING = 50

# The durable undo ledger (review MAJOR 3): a 0600 append-only JSONL file
# under the app data dir. ops_events is pruned after RETENTION_DAYS=7, which
# turned 'undo' into a silent 'undone 0' — the ledger must outlive retention.
AUDIT_FILENAME = "gmail-label-audit.jsonl"


class AuditLedgerError(RuntimeError):
    """A write to the durable audit ledger failed. The ledger is the undo
    source of truth, so the run fails BEFORE the label is applied — an
    applied label is never left un-undoable (review MAJOR 4)."""


@dataclass(frozen=True)
class UndoResult:
    """What one undo pass did (the CLI reports all of it)."""

    total: int  # apply events believed applied (outcome ok) for the batch
    undone: int
    already_undone: int
    failed: int


@dataclass(frozen=True)
class RuleSet:
    """Parsed rules file: label -> sender domains / List-Id substrings."""

    domains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    list_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load_rules(path: str | None) -> RuleSet:
    """Load the rules JSON ({"School": {"domains": [...], "list_ids": [...]}}).

    An empty/missing path means NO domain/list rules — everything that is not
    a kid subject lands SB/Unsorted. Unknown labels in the file are ignored
    (the taxonomy is the code's, not the file's). EMPTY patterns are dropped:
    an empty domain or List-Id substring would otherwise match EVERY message
    ( ''.in(x) is always True; ''.endswith('') is True ) — review follow-up.
    """
    if not path:
        return RuleSet()
    with open(path) as fh:
        raw = json.load(fh)
    domains: dict[str, tuple[str, ...]] = {}
    list_ids: dict[str, tuple[str, ...]] = {}
    for label, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            continue
        domains[str(label)] = tuple(
            str(d).lower().strip() for d in (spec.get("domains") or []) if str(d).strip()
        )
        list_ids[str(label)] = tuple(
            str(d).lower().strip() for d in (spec.get("list_ids") or []) if str(d).strip()
        )
    return RuleSet(domains=domains, list_ids=list_ids)


def _sender_domain(sender: str | None) -> str:
    """The domain of 'Rinat <office@school.example>' — lowercase, no brackets."""
    if not sender or "@" not in sender:
        return ""
    tail = sender.rsplit("@", 1)[1].strip().strip(">").lower()
    return tail.split(">", 1)[0].strip()


def classify(
    *,
    sender: str | None,
    subject: str | None,
    list_header: str | None,
    kid_names: list[str],
    rules: RuleSet | None,
) -> str:
    """One SB/ label path for one message's headers (pure; no I/O)."""
    rules = rules or RuleSet()
    # Kids first (precedence), subject-only, FULL token (zero wrong-child).
    for name in kid_names:
        if is_name_token(name, (subject or "").strip()):
            return f"{SB_PREFIX}Kids/{name}"
    lowered_list = (list_header or "").lower()
    domain = _sender_domain(sender)
    for label in LABEL_ORDER:
        if label == "Kids":
            continue
        domain_hit = any(
            domain == d or domain.endswith("." + d) for d in rules.domains.get(label, ())
        ) if domain else False
        list_hit = any(p in lowered_list for p in rules.list_ids.get(label, ()))
        if domain_hit or list_hit:
            return f"{SB_PREFIX}{label}"
    return UNSORTED


@dataclass(frozen=True)
class LabelPlan:
    """One planned labeling (dry-run row or apply unit)."""

    gmail_id: str
    label_path: str
    label_id: str | None  # the existing Gmail label id, when already created
    source: str  # "kid:<name>" | "domain" | "list-id" | "unsorted"


def plan_hash(plans: list[LabelPlan]) -> str:
    """A stable content hash of ONE plan (review follow-up): the dry-run
    prints it; ``--apply --confirm-plan <hash>`` writes exactly that preview
    or refuses with an explanation when the plan has since changed."""
    payload = "\n".join(f"{p.gmail_id}\t{p.label_path}\t{p.source}" for p in plans)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def plan_labels(
    client: Any,
    *,
    rules: RuleSet,
    kid_names: list[str],
    label_id: str,
    query: str,
    max_scan: int = 100,
) -> list[LabelPlan]:
    """Walk the sync scope (ids + metadata GETs ONLY — zero writes) and plan
    one label per message that carries none yet. Bounded by ``max_scan``
    metadata GETs; messages already carrying ANY SB/ label are skipped
    (idempotence — no re-label churn)."""
    plans: list[LabelPlan] = []
    existing_names = {label.name: label for label in client.list_labels()}
    page_token: str | None = None
    scanned = 0
    while scanned < max_scan:
        stubs, page_token = client.list_messages_page(
            query=query, label_ids=[label_id], page_token=page_token, max_results=100
        )
        for stub in stubs:
            if scanned >= max_scan:
                break
            scanned += 1
            msg = client.get_message_metadata(stub.id)
            if any(lid.startswith("SB/") or lname.startswith(SB_PREFIX)
                   for lid, lname in _label_names(msg, existing_names)):
                continue  # already SB-labeled: idempotent skip
            subject = msg.subject or ""
            list_header = (msg.extra_headers or {}).get("List-Id")
            path = classify(
                sender=msg.sender,
                subject=subject,
                list_header=list_header,
                kid_names=kid_names,
                rules=rules,
            )
            label = existing_names.get(path)
            source = "kid" if path.startswith(f"{SB_PREFIX}Kids") else (
                "unsorted" if path == UNSORTED else "rules"
            )
            plans.append(
                LabelPlan(
                    gmail_id=stub.id,
                    label_path=path,
                    label_id=label.id if label else None,
                    source=source,
                )
            )
        if page_token is None:
            break
    return plans


def _label_names(msg: Any, existing: dict[str, Any]) -> list[tuple[str, str]]:
    by_id = {label.id: label.name for label in existing.values()}
    return [(lid, by_id.get(lid, "")) for lid in (msg.label_ids or [])]


def _consent_error(exc: Exception) -> GmailAuthError:
    """A 403 from a write call = gmail.modify not consented (rate-limit 403s
    are retried inside the client and never surface here)."""
    raise GmailAuthError(
        "gmail.modify is not consented — run `brain gmail-label --auth` to "
        "re-consent (the labeler writes labels only)",
    ) from exc


def ensure_labels(client: Any, paths: list[str]) -> dict[str, str]:
    """Map each SB/ path to a label id, CREATING the missing ones.

    This is the run's FIRST write-capable call: a 403 here (gmail.modify not
    consented) aborts before ANY message is modified. A 409 on create means a
    CONCURRENT creator won the race — re-list and use the existing label.
    """
    try:
        by_name = {label.name: label for label in client.list_labels()}
        out: dict[str, str] = {}
        for path in paths:
            label = by_name.get(path)
            if label is None:
                try:
                    label = client.create_label(path)
                except GmailError as exc:
                    if getattr(exc, "status", None) != 409:
                        raise
                    by_name = {label.name: label for label in client.list_labels()}
                    label = by_name.get(path)
                    if label is None:  # not a create race after all
                        raise
            out[path] = label.id
    except GmailAuthError as exc:
        raise _consent_error(exc) from exc
    except GmailError as exc:
        if getattr(exc, "status", None) == 403:
            raise _consent_error(exc) from exc
        raise
    return out


# --- the durable audit ledger (review MAJORs 3+4) -----------------------------


def audit_path_for(settings: Any) -> Path:
    """Where the JSONL audit ledger lives: GMAIL_LABELER_AUDIT_PATH when set,
    else ``<files_dir>/gmail-label-audit.jsonl`` (the app data dir, OUTSIDE
    the repo and the DB)."""
    configured = str(getattr(settings, "gmail_labeler_audit_path", "") or "")
    if configured:
        return Path(configured).expanduser()
    files_dir = getattr(settings, "files_dir", None) or (Path.home() / ".second-brain-files")
    return Path(files_dir) / AUDIT_FILENAME


def append_audit_row(path: Path, row: dict[str, Any]) -> None:
    """Append ONE row to the JSONL ledger, flushed + fsync'd, file 0600.

    Raises (AuditLedgerError wrapping the OSError) on ANY failure — the
    caller fails the run BEFORE touching Gmail (MAJOR 4: no un-undoable
    labels)."""
    row = {"ts": datetime.now(UTC).isoformat(), **row}
    line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, 0o600)
    except OSError as exc:
        raise AuditLedgerError(
            f"the audit ledger is unwritable at {path}: {exc} — refusing to "
            "apply labels that could not be recorded (undo depends on it)"
        ) from exc


def read_audit_rows(path: Path) -> list[dict[str, Any]]:
    """Every ledger row, oldest first (file order = append order). A corrupt
    trailing line (a crash mid-write) is skipped with a warning, never fatal
    for reads — undo still works off the intact prefix."""
    if not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                logger.warning("audit ledger %s: skipping corrupt line %d", path, lineno)
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def apply_labels(
    client: Any,
    plans: list[LabelPlan],
    *,
    max_apply: int,
    batch_id: str,
    audit_path: Path,
) -> int:
    """Apply at most ``max_apply`` planned labelings; audit each one.

    Per message (review MAJOR 4): the 'apply' audit row is written FIRST —
    a failed ledger write fails the run with NOTHING modified (no label can
    end up un-undoable) — then the modify, then the 'outcome' row.
    Returns the applied count. The cap is hard: max_apply < 1 is a
    programming error (ValueError), and everything past MIN(max_apply,
    MAX_APPLY_CEILING) is never sent (MAJOR 1).
    """
    if max_apply < 1:
        raise ValueError(f"max_apply must be >= 1 (got {max_apply}) — the cap cannot be lifted")
    batch = plans[: min(max_apply, MAX_APPLY_CEILING)]
    if not batch:
        return 0
    label_ids = ensure_labels(client, sorted({p.label_path for p in batch}))
    applied = 0
    for plan in batch:
        event = uuid.uuid4().hex[:16]
        # MAJOR 4: the ledger row FIRST (fsync'd), BEFORE the label write.
        append_audit_row(
            audit_path,
            {
                "kind": "apply",
                "event": event,
                "batch": batch_id,
                "gmail_id": plan.gmail_id,
                "label_path": plan.label_path,
                "label_id": label_ids[plan.label_path],
                "source": plan.source,
            },
        )
        try:
            client.modify_message_labels(plan.gmail_id, add=[label_ids[plan.label_path]])
        except GmailAuthError as exc:
            raise _consent_error(exc) from exc
        except GmailError as exc:
            if getattr(exc, "status", None) == 403:
                raise _consent_error(exc) from exc
            append_audit_row(
                audit_path,
                {"kind": "outcome", "event": event, "batch": batch_id,
                 "gmail_id": plan.gmail_id, "ok": False,
                 "error": f"{type(exc).__name__}: {exc}"[:200]},
            )
            raise
        append_audit_row(
            audit_path,
            {"kind": "outcome", "event": event, "batch": batch_id,
             "gmail_id": plan.gmail_id, "ok": True},
        )
        applied += 1
    append_audit_row(audit_path, {"kind": "batch", "batch": batch_id, "ok": True,
                                  "applied": applied})
    return applied


def undo_batch(client: Any, batch_id: str, *, audit_path: Path) -> UndoResult:
    """Remove exactly the label ids ONE batch added, from the durable JSONL
    ledger (survives ops_events' 7-day prune — review MAJOR 3).

    Continues past per-item errors (a deleted message, a missing label):
    each failure is recorded in the ledger and counted, never fatal for the
    rest of the batch. A batch (or item) already undone is skipped —
    re-running an undo never churns (review follow-up). A pre-existing user
    label on the same message is untouched: only the batch's own label ids
    are removed, and only from the messages the batch touched.
    """
    rows = read_audit_rows(audit_path)
    applied: dict[str, dict[str, Any]] = {}  # event -> apply row, file order
    for row in rows:
        if row.get("batch") != batch_id:
            continue
        event = str(row.get("event") or "")
        if row.get("kind") == "apply":
            applied[event] = {**row, "event": event}
        elif row.get("kind") == "outcome" and event in applied:
            applied[event]["_outcome_ok"] = row.get("ok") is True
    undone_before = {
        row.get("event")
        for row in rows
        if row.get("kind") == "undo" and row.get("batch") == batch_id and row.get("ok") is True
    }
    targets = [row for row in applied.values() if row.get("_outcome_ok") is not False]
    undone = already = failed = 0
    for row in targets:
        event = row.get("event") or ""
        if event in undone_before:
            already += 1
            continue
        gmail_id, label_id = row.get("gmail_id"), row.get("label_id")
        if not gmail_id or not label_id:
            failed += 1
            append_audit_row(audit_path, {"kind": "undo", "event": event,
                                          "batch": batch_id, "ok": False,
                                          "error": "ledger row missing gmail_id/label_id"})
            continue
        try:
            client.modify_message_labels(gmail_id, remove=[label_id])
        except GmailAuthError as exc:
            raise _consent_error(exc) from exc
        except GmailError as exc:
            if getattr(exc, "status", None) == 403:
                raise _consent_error(exc) from exc
            append_audit_row(audit_path, {"kind": "undo", "event": event,
                                          "batch": batch_id, "gmail_id": gmail_id,
                                          "label_id": label_id, "ok": False,
                                          "error": f"{type(exc).__name__}: {exc}"[:200]})
            failed += 1
            continue
        append_audit_row(audit_path, {"kind": "undo", "event": event, "batch": batch_id,
                                      "gmail_id": gmail_id, "label_id": label_id, "ok": True})
        undone += 1
    return UndoResult(total=len(targets), undone=undone, already_undone=already, failed=failed)
