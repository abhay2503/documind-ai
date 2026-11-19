"""
Decide what to do when processing a job fails.

    classify(error) -> Failure   the error can never succeed on retry: mark the job 'failed' now
    classify(error) -> None      the error may be temporary: keep the normal retry behaviour

Only errors we are SURE are permanent are listed here. Everything else (timeouts, 5xx, 429 rate
limits that clear after a minute, a database that is down...) stays retryable.

The message is shown to the user, so it must be friendly and must not contain provider details
(organisation ids, token counts, billing links...). The full error is still printed to CloudWatch.
"""

from typing import NamedTuple, Optional


class Failure(NamedTuple):
    code: str       # stored in summary_jobs.error_code
    message: str    # stored in summary_jobs.error_message (shown in the UI)


TOO_LARGE = Failure(
    "DOCUMENT_TOO_LARGE",
    "This document is too large for the AI model to summarize in one go. "
    "Please try a shorter document or split it into smaller parts.",
)

REQUEST_REJECTED = Failure(
    "AI_REQUEST_REJECTED",
    "The AI service could not process this document. Please try a different document.",
)

SERVICE_UNAVAILABLE = Failure(
    "AI_SERVICE_ERROR",
    "The summarization service is not available right now. Please try again later.",
)

# HTTP status codes from the LLM provider that retrying will never fix
_NON_RETRYABLE = {
    413: TOO_LARGE,             # request bigger than the model / token limit
    400: REQUEST_REJECTED,      # bad request
    422: REQUEST_REJECTED,      # request could not be processed
    401: SERVICE_UNAVAILABLE,   # bad or missing API key (our configuration, not the user's)
    403: SERVICE_UNAVAILABLE,
    404: SERVICE_UNAVAILABLE,   # model not found
}
# Deliberately NOT listed: 408, 409, 429 (rate limit that clears), 5xx -> these are retried.


class DocumentTooLargeError(Exception):
    """Raised by our own code when a document is bigger than we can summarize within the limits."""


def classify(error: BaseException) -> Optional[Failure]:
    """Look at the error (and whatever it wraps) for an HTTP status from the LLM provider."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, DocumentTooLargeError):
            return TOO_LARGE
        status = getattr(error, "status_code", None)
        if status in _NON_RETRYABLE:
            return _NON_RETRYABLE[status]
        error = error.__cause__ or error.__context__
    return None
