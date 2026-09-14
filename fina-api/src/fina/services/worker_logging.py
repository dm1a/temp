import logging

from fina.repositories.types import (
    AnalysisJobClaim,
    ClientProfileJobClaim,
    DiscoveryRunClaim,
    FetchJobClaim,
    SendOrderJobClaim,
)

JobClaim = (
    FetchJobClaim | AnalysisJobClaim | ClientProfileJobClaim | SendOrderJobClaim | DiscoveryRunClaim
)


def job_context(worker_id: str, claim: JobClaim) -> dict[str, str | int]:
    context: dict[str, str | int] = {
        "worker_id": worker_id,
        "claim_token": str(claim.claim_token),
    }
    if isinstance(claim, DiscoveryRunClaim):
        context["run_id"] = str(claim.id)
    else:
        context["attempt"] = claim.attempts
        if isinstance(claim, ClientProfileJobClaim):
            context["client_id"] = str(claim.client_id)
            context["request_version"] = claim.request_version
        else:
            context["call_id"] = str(claim.call_id)
    return context


def log_job_update(
    logger: logging.Logger,
    message: str,
    *,
    applied: bool,
    worker_id: str,
    claim: JobClaim,
    level: int = logging.INFO,
    **details: str | int | bool | None,
) -> None:
    """Call after the transaction commits; a fenced update is never a success."""
    context = {**job_context(worker_id, claim), **details}
    if applied:
        logger.log(level, message, extra=context)
    else:
        logger.warning(
            "job update skipped for a lost or superseded claim",
            extra={**context, "operation": message},
        )
