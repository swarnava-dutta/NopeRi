"""Strict interpretation of Naukri apply-workflow responses.

The apply endpoint commonly returns HTTP 200 for both useful workflow states
and application-level failures.  Callers therefore must not equate a parsed
JSON body (or the absence of a questionnaire) with a successful application.

``parse_apply_response`` is deliberately conservative: only a response tied
to the requested job can become ``APPLIED`` or ``QUESTIONNAIRE``.  Ambiguous
shapes become ``UNKNOWN`` and explicit rejection signals become ``FAILED``.
Neither outcome should be persisted as an applied job by the caller.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ApplyStatus(str, Enum):
    """Normalized state of one apply-workflow response."""

    APPLIED = "applied"
    QUESTIONNAIRE = "questionnaire"
    ALREADY_APPLIED = "already_applied"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ApplyOutcome:
    """Safe result returned to the apply agent.

    ``questionnaire`` is populated only for ``QUESTIONNAIRE``. ``reason`` is
    diagnostic text produced by this parser; it never contains the full raw
    response, which may hold screening questions or personal information.
    """

    status: ApplyStatus
    job_id: str
    questionnaire: Any | None = None
    reason: str = ""

    @property
    def is_applied(self) -> bool:
        """True only for a newly confirmed application."""
        return self.status is ApplyStatus.APPLIED

    @property
    def is_server_applied(self) -> bool:
        """True when the server confirms either a new or prior application."""
        return self.status in (ApplyStatus.APPLIED, ApplyStatus.ALREADY_APPLIED)


_JOB_ID_FIELDS = ("jobId", "jobID", "job_id", "id")
_FALSE_FIELDS = ("success", "isSuccess", "isSuccessful", "applied")
_ERROR_FIELDS = (
    "error",
    "errors",
    "errorMessage",
    "validationErrors",
    "failureReason",
    "exception",
)
_STATUS_FIELDS = (
    "status",
    "applyStatus",
    "statusText",
    "statusValue",
    "message",
    "statusMessage",
)
_STATUS_CODE_FIELDS = ("statusCode", "status_code", "responseCode")
_QUESTIONNAIRE_FIELDS = ("questionnaire", "questionnaireQuestions")
_DIAGNOSTIC_FIELDS = (
    "message",
    "statusMessage",
    "error",
    "errorMessage",
    "failureReason",
    "status",
    "applyStatus",
    "statusText",
    "statusValue",
    "errors",
    "validationErrors",
    "exception",
)

_ALREADY_APPLIED_RE = re.compile(
    r"\b(?:already\s+(?:been\s+)?applied|already\s+submitted|"
    r"application\s+(?:has\s+)?already\s+(?:been\s+)?(?:submitted|exists))\b",
    re.IGNORECASE,
)
_AMBIGUOUS_ALREADY_RE = re.compile(
    r"\balready\s+(?:been\s+)?applied\s+or\s+(?:failed|failure)\b",
    re.IGNORECASE,
)
_FAILURE_TEXT_RE = re.compile(
    r"\b(?:failed|failure|unsuccessful|error|rejected|invalid|expired|"
    r"closed|cancelled|canceled|ineligible|not\s+eligible|not\s+accepting|"
    r"not\s+(?:(?:yet|successfully|been)\s+){0,2}"
    r"(?:applied|submitted|sent|successful|completed|succeeded)|"
    r"did\s+not\s+(?:apply|submit|send|succeed|complete)|no\s+success|"
    r"application\s+not\s+sent)\b",
    re.IGNORECASE,
)
_POSITIVE_TEXT_RE = re.compile(
    r"\b(?:success|successful|succeeded|applied|application\s+sent|"
    r"applied\s+successfully|successfully\s+applied|completed)\b",
    re.IGNORECASE,
)
_NEGATED_POSITIVE_RE = re.compile(
    r"(?:\b(?:application|submission|request)\s+"
    r"(?:(?:was|is|has|have|had)\s+)?(?:"
    r"not\s+(?:(?:yet|been|successfully|a)\s+){0,3}"
    r"(?:success(?:ful|fully)?|succeed(?:ed)?|appl(?:y|ied)|"
    r"complet(?:e|ed)|submit(?:ted)?|sent)|"
    r"(?:cannot|can't|could\s+not)\s+(?:be\s+)?"
    r"(?:successful|completed|submitted|sent|applied))\b|"
    r"^(?:cannot|can't|could\s+not)\s+(?:be\s+)?"
    r"(?:successful|completed|submitted|sent|applied)\b|"
    r"\bnot\s+a\s+success\b)",
    re.IGNORECASE,
)


def normalize_job_id(value: Any) -> str:
    """Return a stable textual job ID without destroying leading zeroes."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _outcome(
    status: ApplyStatus,
    job_id: str,
    reason: str,
    questionnaire: Any | None = None,
) -> ApplyOutcome:
    return ApplyOutcome(
        status=status,
        job_id=job_id,
        questionnaire=questionnaire,
        reason=reason,
    )


def _flatten_text(value: Any, depth: int = 0):
    """Yield short textual leaves from known response fields only."""
    if depth > 3 or value is None:
        return
    if isinstance(value, str):
        text = " ".join(value.split())
        if text:
            yield text
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _flatten_text(nested, depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _flatten_text(nested, depth + 1)


def _known_texts(data: Mapping[str, Any]):
    for field in _DIAGNOSTIC_FIELDS:
        if field in data:
            yield from _flatten_text(data.get(field))


def _is_nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, Mapping)):
        return bool(value)
    return True


def _false_like(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, str):
        return value.strip().casefold() in {
            "",
            "0",
            "false",
            "failed",
            "failure",
            "error",
            "rejected",
            "no",
        }
    return False


def _true_like(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {
            "1",
            "true",
            "yes",
            "success",
            "successful",
            "applied",
            "completed",
        }
    return False


# Naukri's cloudgateway apply-workflow reports its own result code in
# ``statusCode``; 0 means "no error" and is NOT an HTTP status. Successful
# applies carry ``statusCode: 0`` with no message, error, or status text at
# all, so grading it like an HTTP code marked every single response FAILED
# ("top-level failure signal: statusCode=0") and stopped the whole run from
# recording a single application. Treat it exactly like a 2xx.
_NO_ERROR_STATUS_CODE = 0


def _numeric_status_code(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_failure_status_code(code: int | None) -> bool:
    """True only when a code positively reports an error."""
    if code is None or code == _NO_ERROR_STATUS_CODE:
        return False
    return not 200 <= code < 300


def _is_success_status_code(code: int | None) -> bool:
    """True for the no-error sentinel and for any 2xx code."""
    if code is None:
        return False
    return code == _NO_ERROR_STATUS_CODE or 200 <= code < 300


def _has_already_applied(data: Mapping[str, Any]) -> bool:
    for text in _known_texts(data):
        if _AMBIGUOUS_ALREADY_RE.search(text):
            continue
        if _ALREADY_APPLIED_RE.search(text):
            return True
    return False


def _has_explicit_failure(data: Mapping[str, Any]) -> bool:
    for field in _FALSE_FIELDS:
        if field in data and _false_like(data.get(field)):
            return True

    for field in _ERROR_FIELDS:
        if field in data and _is_nonempty(data.get(field)):
            return True

    for field in _STATUS_CODE_FIELDS:
        if _is_failure_status_code(_numeric_status_code(data.get(field))):
            return True

    status_value = data.get("status")
    if status_value is False or (
        isinstance(status_value, (int, float))
        and not isinstance(status_value, bool)
        and status_value == 0
    ):
        return True

    return any(
        _FAILURE_TEXT_RE.search(text) or _NEGATED_POSITIVE_RE.search(text)
        for text in _known_texts(data)
    )


def _diagnostic_value(value: Any) -> str:
    """Render only a short status/error value, never the full response body."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = " ".join(value.split())
        return text[:160] + ("…" if len(text) > 160 else "")
    if _is_nonempty(value):
        return "present"
    return ""


def _failure_reason(data: Mapping[str, Any], scope: str) -> str:
    """Explain an explicit failure using sanitized, known response fields."""
    details: list[str] = []

    for field in _STATUS_CODE_FIELDS:
        code = _numeric_status_code(data.get(field))
        if _is_failure_status_code(code):
            details.append(f"{field}={code}")

    for field in _FALSE_FIELDS:
        if field in data and _false_like(data.get(field)):
            details.append(f"{field}={_diagnostic_value(data.get(field))}")

    for field in _DIAGNOSTIC_FIELDS:
        if field not in data:
            continue
        value = data.get(field)
        diagnostic = _diagnostic_value(value)
        if not diagnostic:
            continue
        details.append(f"{field}={diagnostic}")

    # Keep logs useful but bounded, unique, and free of questionnaire content.
    unique = list(dict.fromkeys(details))[:4]
    suffix = f": {'; '.join(unique)}" if unique else ""
    return f"{scope} failure signal{suffix}"


def _has_positive_signal(data: Mapping[str, Any]) -> bool:
    for field in _FALSE_FIELDS:
        if field in data and _true_like(data.get(field)):
            return True

    for field in _STATUS_CODE_FIELDS:
        if _is_success_status_code(_numeric_status_code(data.get(field))):
            return True

    return any(
        _POSITIVE_TEXT_RE.search(text)
        and not _FAILURE_TEXT_RE.search(text)
        and not _NEGATED_POSITIVE_RE.search(text)
        for text in _known_texts(data)
    )


def _has_workflow_marker(data: Mapping[str, Any]) -> bool:
    fields = (
        *_FALSE_FIELDS,
        *_ERROR_FIELDS,
        *_STATUS_FIELDS,
        *_STATUS_CODE_FIELDS,
        *_QUESTIONNAIRE_FIELDS,
    )
    return any(field in data for field in fields)


def _item_job_id(item: Mapping[str, Any]) -> str:
    for field in _JOB_ID_FIELDS:
        job_id = normalize_job_id(item.get(field))
        if job_id:
            return job_id
    return ""


def _questionnaire(data: Mapping[str, Any]) -> Any | None:
    for field in _QUESTIONNAIRE_FIELDS:
        value = data.get(field)
        if _is_nonempty(value):
            return value
    return None


def parse_apply_response(
    data: Any,
    requested_job_id: Any,
    *,
    final_submission: bool = False,
) -> ApplyOutcome:
    """Parse one Naukri apply response without assuming HTTP 200 means success.

    Args:
        data: Parsed JSON response from the apply-workflow endpoint.
        requested_job_id: ID sent in ``strJobsarr``.
        final_submission: True for the second request that submits screening
            questionnaire answers. A repeated questionnaire at that stage is
            a failure rather than another intermediate workflow state.
    """
    requested_id = normalize_job_id(requested_job_id)
    if not requested_id:
        return _outcome(ApplyStatus.FAILED, "", "requested job ID is empty")

    if not isinstance(data, Mapping):
        return _outcome(ApplyStatus.UNKNOWN, requested_id, "response is not an object")

    # An explicit prior-application message is terminal even when this endpoint
    # omits its normal jobs array for duplicate submissions.
    if _has_already_applied(data):
        return _outcome(
            ApplyStatus.ALREADY_APPLIED,
            requested_id,
            "server reports that this job was already applied",
        )

    if _has_explicit_failure(data):
        return _outcome(
            ApplyStatus.FAILED,
            requested_id,
            _failure_reason(data, "top-level"),
        )

    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return _outcome(ApplyStatus.UNKNOWN, requested_id, "missing or empty jobs list")

    entries = [item for item in jobs if isinstance(item, Mapping)]
    if not entries:
        return _outcome(ApplyStatus.UNKNOWN, requested_id, "jobs list has no objects")

    matching = [item for item in entries if _item_job_id(item) == requested_id]
    if len(matching) > 1:
        return _outcome(ApplyStatus.UNKNOWN, requested_id, "duplicate matching job entries")

    if matching:
        item = matching[0]
        explicitly_matched = True
    else:
        identified_ids = [_item_job_id(item) for item in entries if _item_job_id(item)]
        if identified_ids or len(entries) != 1:
            return _outcome(ApplyStatus.UNKNOWN, requested_id, "requested job is absent")
        # The endpoint handles one requested ID per call. An ID-less single
        # entry can still carry an explicit success or questionnaire state, but
        # it is never accepted through the compatibility fallback below.
        item = entries[0]
        explicitly_matched = False

    if _has_already_applied(item):
        return _outcome(
            ApplyStatus.ALREADY_APPLIED,
            requested_id,
            "matching job reports that it was already applied",
        )

    if _has_explicit_failure(item):
        return _outcome(
            ApplyStatus.FAILED,
            requested_id,
            _failure_reason(item, "matching job"),
        )

    questions = _questionnaire(item)
    if questions is None:
        questions = _questionnaire(data)
    if questions is not None:
        if final_submission:
            return _outcome(
                ApplyStatus.FAILED,
                requested_id,
                "questionnaire still required after final submission",
            )
        return _outcome(
            ApplyStatus.QUESTIONNAIRE,
            requested_id,
            "questionnaire required",
            questionnaire=questions,
        )

    item_positive = _has_positive_signal(item)
    top_positive = _has_positive_signal(data)
    if item_positive or (top_positive and not _has_workflow_marker(item)):
        return _outcome(ApplyStatus.APPLIED, requested_id, "explicit success signal")

    # Compatibility with the established endpoint contract: a non-empty jobs
    # entry for the exact requested ID, without any error/questionnaire marker,
    # represents a completed direct apply. Never use this fallback for an
    # anonymous entry, an empty object, or an entry belonging to another ID.
    if (
        explicitly_matched
        and item
        and not _has_workflow_marker(item)
        and not _has_workflow_marker(data)
    ):
        return _outcome(
            ApplyStatus.APPLIED,
            requested_id,
            "matching job entry without failure markers",
        )

    if _has_workflow_marker(item) or _has_workflow_marker(data):
        return _outcome(
            ApplyStatus.UNKNOWN,
            requested_id,
            "unrecognized workflow signal",
        )

    return _outcome(ApplyStatus.UNKNOWN, requested_id, "no conclusive workflow state")
