from fina_api.repositories.analysis_jobs import AnalysisJobRepository
from fina_api.repositories.discovery_runs import (
    ENQUEUE_NEXT_DISCOVERY_SQL,
    DiscoveryRunRepository,
)
from fina_api.repositories.fetch_jobs import FetchJobRepository
from fina_api.repositories.transcripts import (
    RAW_TRANSCRIPTS_SQL,
    SUMMARIZED_TRANSCRIPTS_SQL,
)


def test_transcript_queries_are_fixed_and_parameterized() -> None:
    raw = str(RAW_TRANSCRIPTS_SQL)
    summarized = str(SUMMARIZED_TRANSCRIPTS_SQL)

    assert "s.transcript_search @@ websearch_to_tsquery" in raw
    assert "s.summary_search @@ websearch_to_tsquery" in summarized

    for statement in (RAW_TRANSCRIPTS_SQL, SUMMARIZED_TRANSCRIPTS_SQL):
        assert set(statement._bindparams) == {
            "advisor_phone",
            "client_phone",
            "date_from",
            "date_to",
            "search",
            "limit",
            "offset",
        }


def test_queue_repositories_expose_claim_and_terminal_operations() -> None:
    assert hasattr(DiscoveryRunRepository, "enqueue_next")
    assert hasattr(FetchJobRepository, "claim_next")
    assert hasattr(FetchJobRepository, "complete_and_enqueue_analysis")
    assert hasattr(FetchJobRepository, "schedule_retry")
    assert hasattr(FetchJobRepository, "mark_failed")

    assert hasattr(AnalysisJobRepository, "claim_next")
    assert hasattr(AnalysisJobRepository, "complete_and_index")
    assert hasattr(AnalysisJobRepository, "schedule_retry")
    assert hasattr(AnalysisJobRepository, "mark_failed")


def test_discovery_scheduling_uses_completed_boundary_and_blocks_overlap() -> None:
    statement = str(ENQUEUE_NEXT_DISCOVERY_SQL)

    assert "status = 'COMPLETED'" in statement
    assert "status IN ('PENDING', 'RUNNING')" in statement
    assert "COALESCE(last_completed.window_from, :initial_window_from)" in statement
