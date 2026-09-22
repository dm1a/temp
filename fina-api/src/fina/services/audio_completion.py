"""Successful provider results bound to the jobs they belong to.

Construction checks response identity and retains the matching claim and result.
This does not prove that the claim is still owned: repositories must check the
claim token atomically when committing a completion.
"""

from dataclasses import dataclass

from fina.domain.audio_analysis import AnalyzeResult, TaskIdMismatchError
from fina.domain.audio_contracts import CallIdentityMismatchError
from fina.domain.audio_fetch import FetchCallResult
from fina.repositories.types import AnalysisJobClaim, FetchJobClaim


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchCompletion:
    claim: FetchJobClaim
    result: FetchCallResult

    def __post_init__(self) -> None:
        if self.result.identity != self.claim.identity:
            # Keep phone numbers and other call data out of the exception message.
            raise CallIdentityMismatchError("Fetched call identity does not match the queued call")


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisCompletion:
    claim: AnalysisJobClaim
    result: AnalyzeResult

    def __post_init__(self) -> None:
        if self.result.task_id != self.claim.call_id:
            raise TaskIdMismatchError("Analysis task ID does not match the queued task")
