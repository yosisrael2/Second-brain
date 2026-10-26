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
  GETs only); ``--apply`` is explicit and capped (``max_apply``, valid range
  1..MAX_APPLY_CEILING (100), default 20 per run — argparse refuses anything
  outside the range; the service raises ValueError outside it too; there is
  NO unlimited).
- own credentials: the labeler's refresh token lives in its OWN keychain item
  (``LABELER_KEYCHAIN_SERVICE``, distinct from the sync token's item) and is
  stored by ``--auth`` ONLY after the consented profile's email address
  matches the configured account (``settings.gmail_account``). A missing
  item is a refusal, never a fallback to the sync token.
- label-only: the labeler adds SB/ labels and never removes INBOX, never
  archives/trashes/deletes. v1 creates NO Gmail filters (gmail.settings.basic
  never enters the scope set).
- consent gate: label writes need gmail.modify. The FIRST write-capable call
  (label creation) happens before any message is modified; a 403 there aborts
  the run with a re-consent message — zero message writes.
- write-ahead audit + undo: each label's ops_event (component 'gmail-label',
  kind 'apply') is committed with detail state 'intent' BEFORE its Gmail
  modify call, then updated to 'applied' or 'failed'. An intent row that
  cannot be committed means the label is NOT applied (no unaudited writes).
  ``undo_batch`` treats EVERY 'apply' row of the batch as its undo
  candidate — 'failed' included, because a GmailError never proves the
  label is absent (a 5xx/timeout can fire after Google committed the
  write), and removing a label that was never applied is harmless — and
  removes exactly the label ids its batch added — user labels are
  untouched. The prune EXEMPTS these rows from the 7-day retention (they
  are the undo ledger).
- idempotent: a message already carrying ANY SB/ label is skipped.

Message selection: the same scope the email connector syncs (the configured
gmail label + query), so the labeler can never reach outside the pipeline's
scope. The scan is bounded (``max_scan``, default 100) — each metadata GET
costs ~5 quota units, paced by the client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from brain.ai.gmail import GMAIL_LABEL_SCOPES, GmailAuthError, GmailError
from brain.ops import record_event
from brain.services.triage import is_name_token

MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# The labeler's OWN keychain item — deliberately NOT the sync token's item
# (brain.services.keychain.SERVICE / GMAIL_ACCOUNT). ``--auth`` stores the
# labeler's modify-capable token HERE and never reads, writes or overwrites
# the sync token's item; a missing item is a refusal (exit 78), never a
# fallback to the sync token.
LABELER_KEYCHAIN_SERVICE = "second-brain-gmail-labeler"
LABELER_KEYCHAIN_ACCOUNT = "default"


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
# The per-run apply cap ceiling (M1/R2): ONE constant shared by the argparse
# type (cli._max_apply_value) and the service's own check, so the CLI range
# and the defense in depth can never drift apart.
MAX_APPLY_CEILING = 100
# Precedence (high → low). Kids is memory-driven; the rest come from the
# rules file. A message gets exactly ONE label — the first rule that wins.
LABEL_ORDER = ("Kids", "School", "Logistics", "Bills", "Noise")

COMPONENT = "gmail-label"


@dataclass(frozen=True)
class RuleSet:
    """Parsed rules file: label -> sender domains / List-Id substrings."""

    domains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    list_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load_rules(path: str | None) -> RuleSet:
    """Load the rules JSON ({"School": {"domains": [...], "list_ids": [...]}}).

    An empty/missing path means NO domain/list rules — everything that is not
    a kid subject lands SB/Unsorted. Unknown labels in the file are ignored
    (the taxonomy is the code's, not the file's).
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
        domains[str(label)] = tuple(str(d).lower() for d in (spec.get("domains") or []))
        list_ids[str(label)] = tuple(str(d).lower() for d in (spec.get("list_ids") or []))
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


def build_labeler_client(
    refresh_token: str,
    *,
    settings: Any,
    service_factory: Any = None,
) -> Any:
    """RestGmailClient over GMAIL_LABEL_SCOPES (readonly + gmail.modify),
    paced by the connector's quota-hygiene interval. The SYNC path never uses
    this (its credentials stay readonly-only)."""
    from brain.ai.gmail import RestGmailClient
    from brain.connectors.google.base import default_service_factory

    factory = service_factory or default_service_factory
    service = factory(refresh_token, list(GMAIL_LABEL_SCOPES), "gmail", "v1")
    return RestGmailClient(
        service=service,
        min_interval_s=getattr(settings, "gmail_min_call_interval_s", None),
    )


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
    consented) aborts before ANY message is modified.
    """
    try:
        by_name = {label.name: label for label in client.list_labels()}
        out: dict[str, str] = {}
        for path in paths:
            label = by_name.get(path)
            if label is None:
                label = client.create_label(path)
            out[path] = label.id
    except GmailAuthError as exc:
        raise _consent_error(exc) from exc
    except GmailError as exc:
        if getattr(exc, "status", None) == 403:
            raise _consent_error(exc) from exc
        raise
    return out


def apply_labels(
    client: Any,
    plans: list[LabelPlan],
    *,
    max_apply: int,
    batch_id: str,
    session: Any = None,
) -> int:
    """Apply at most ``max_apply`` planned labelings; ops-audit each one
    WRITE-AHEAD.

    Returns the applied count. The label ids are ensured FIRST (a 403 aborts
    before any message write); the cap is hard — nothing past it is sent, and
    ``max_apply`` outside 1..MAX_APPLY_CEILING raises ValueError (there is no
    unlimited; argparse already pins the CLI to the same range).

    Per label: the audit row (state 'intent') is COMMITTED before the Gmail
    modify call; after the call it is updated to 'applied' or 'failed'. An
    intent row that cannot be committed means the label is NOT applied —
    there are no unaudited writes. ``session`` (optional) hosts the audit
    writes; when None a short-lived session is opened per write.
    """
    if not 1 <= max_apply <= MAX_APPLY_CEILING:
        raise ValueError(
            f"max_apply must be in 1..{MAX_APPLY_CEILING} (got {max_apply!r}) "
            "— the per-run cap has no unlimited mode"
        )
    batch = plans[:max_apply]
    if not batch:
        return 0
    label_ids = ensure_labels(client, sorted({p.label_path for p in batch}))
    applied = 0
    for plan in batch:
        detail = {
            "batch": batch_id,
            "gmail_id": plan.gmail_id,
            "label_path": plan.label_path,
            "label_id": label_ids[plan.label_path],
            "source": plan.source,
        }
        if not _record_audit(
            session, "apply", ok=True, detail={**detail, "state": "intent"}
        ):
            logger.warning(
                "gmail-label: the intent audit row for %s (batch %s) did not "
                "commit — the label is NOT applied (no unaudited writes)",
                plan.gmail_id,
                batch_id,
            )
            continue
        try:
            client.modify_message_labels(plan.gmail_id, add=[label_ids[plan.label_path]])
        except GmailAuthError as exc:
            _finish_audit(session, batch_id, plan.gmail_id, state="failed", ok=False)
            raise _consent_error(exc) from exc
        except GmailError as exc:
            _finish_audit(session, batch_id, plan.gmail_id, state="failed", ok=False)
            if getattr(exc, "status", None) == 403:
                raise _consent_error(exc) from exc
            raise
        _finish_audit(session, batch_id, plan.gmail_id, state="applied", ok=True)
        applied += 1
    record_event(COMPONENT, "batch", ok=True, detail={"batch": batch_id, "applied": applied})
    return applied


def _record_audit(session: Any, kind: str, *, ok: bool, detail: dict[str, Any]) -> bool:
    """INSERT one labeler audit row into ops_events and COMMIT.

    True only when the row COMMITTED. Unlike :func:`brain.ops.record_event`
    (log-and-swallow — instrumentation must never break the pipeline), the
    labeler's write-ahead audit MUST know whether its row landed: a failed
    write means the label is not applied. Same table, same component; just
    an honest return.
    """
    own = session is None
    audit_session = None
    try:
        from sqlalchemy import text  # noqa: PLC0415

        audit_session = _audit_session(session)
        audit_session.execute(
            text(
                "INSERT INTO ops_events (component, kind, ok, detail) "
                "VALUES (:component, :kind, :ok, CAST(:detail AS jsonb))"
            ),
            {
                "component": COMPONENT,
                "kind": kind[:64],
                "ok": ok,
                "detail": json.dumps(_audit_detail(detail)),
            },
        )
        audit_session.commit()
        return True
    except Exception:
        logger.warning("gmail-label: the audit INSERT failed", exc_info=True)
        if audit_session is not None:
            _rollback_quietly(audit_session)
        return False
    finally:
        if own and audit_session is not None:
            audit_session.close()


def _finish_audit(
    session: Any, batch_id: str, gmail_id: str, *, state: str, ok: bool
) -> None:
    """Advance ONE apply row's detail state: 'intent' -> 'applied'/'failed'.

    Best-effort but loud: a failed state update leaves the row at 'intent',
    which stays an undo candidate (removing a label that was maybe never
    applied is harmless) — it must never strand a half-audited write.
    """
    own = session is None
    audit_session = None
    try:
        from sqlalchemy import text  # noqa: PLC0415

        audit_session = _audit_session(session)
        result = audit_session.execute(
            text(
                "UPDATE ops_events SET ok = :ok, "
                "detail = jsonb_set(detail, '{state}', to_jsonb(CAST(:state AS text))) "
                "WHERE component = :component AND kind = 'apply' "
                "AND detail->>'batch' = :batch AND detail->>'gmail_id' = :gmail_id "
                "AND detail->>'state' = 'intent'"
            ),
            {
                "ok": ok,
                "state": state,
                "component": COMPONENT,
                "batch": batch_id,
                "gmail_id": gmail_id,
            },
        )
        audit_session.commit()
        if not (result.rowcount or 0):
            logger.warning(
                "gmail-label: no open intent row for %s/%s to mark %s",
                batch_id,
                gmail_id,
                state,
            )
    except Exception:
        logger.warning(
            "gmail-label: the audit state update to %s failed", state, exc_info=True
        )
        if audit_session is not None:
            _rollback_quietly(audit_session)
    finally:
        if own and audit_session is not None:
            audit_session.close()


def _audit_session(session: Any) -> Any:
    """The given audit session, or a fresh one from the app's factory."""
    from brain.db.session import SessionLocal  # noqa: PLC0415

    return session or SessionLocal()


def _rollback_quietly(session: Any) -> None:
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 (the failure is already logged)
        pass


def _audit_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Bounded, JSON-safe scalars only (the labeler's own detail is short
    strings; the defense mirrors brain.ops._safe_detail for the row shape
    this module writes directly)."""
    safe: dict[str, Any] = {}
    for key, value in list(detail.items())[:16]:
        safe[str(key)[:64]] = value if isinstance(value, (str, int, float, bool)) \
            or value is None else str(value)[:120]
    return safe


def undo_batch(client: Any, batch_id: str, session: Any = None) -> int:
    """Remove exactly the label ids ONE batch added (its own ops events).

    The undo candidates are EVERY 'apply' row of the batch, whatever its
    state (intent, applied, failed): a GmailError from the modify does NOT
    prove the label never landed — a 5xx, a timeout or a connection reset
    can fire AFTER Google committed the write, and then the row says
    'failed' while the label is on the message. The state is audit truth
    only; undo does not trust it. Removing a label that was never applied is
    a harmless no-op that must not error. A pre-existing user label on the
    same message is untouched: only the batch's own label ids are removed,
    and only from the messages the batch touched.
    """
    events = _batch_events(batch_id, session)
    undone = 0
    for detail in events:
        gmail_id = detail.get("gmail_id")
        label_id = detail.get("label_id")
        if not gmail_id or not label_id:
            continue
        client.modify_message_labels(gmail_id, remove=[label_id])
        record_event(
            COMPONENT,
            "undo",
            ok=True,
            detail={"batch": batch_id, "gmail_id": gmail_id, "label_id": label_id},
        )
        undone += 1
    return undone


def _batch_events(batch_id: str, session: Any) -> list[dict[str, Any]]:
    """The undo ledger of ONE batch: EVERY of its 'apply' rows, oldest
    first — 'intent', 'applied' and 'failed' alike. The state is audit
    truth only, never an undo filter: a 'failed' row's label MAY be on the
    message (a 5xx/timeout can fire after Google committed the write), and
    removing a label that was never applied is a harmless no-op."""
    from sqlalchemy import text  # noqa: PLC0415

    from brain.db.session import SessionLocal  # noqa: PLC0415

    own = session or SessionLocal()
    try:
        rows = own.execute(
            text(
                "SELECT detail FROM ops_events WHERE component = :component "
                "AND kind = 'apply' AND detail->>'batch' = :batch ORDER BY ts"
            ),
            {"component": COMPONENT, "batch": batch_id},
        ).all()
        return [dict(row.detail) for row in rows]
    finally:
        if session is None:
            own.close()
