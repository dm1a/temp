"""Create the agreed FinaAPI foundation schema with raw PostgreSQL SQL.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS = (
    """
    CREATE TABLE advisors (
        phone_normalized TEXT PRIMARY KEY,
        is_active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE clients (
        id UUID PRIMARY KEY,
        phone_normalized TEXT NOT NULL UNIQUE,
        discovered_from_call_id UUID NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE discovery_runs (
        id UUID PRIMARY KEY,
        window_from TIMESTAMPTZ NOT NULL,
        window_to TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        error_code TEXT NULL,
        error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_discovery_runs_window UNIQUE (window_from, window_to),
        CONSTRAINT ck_discovery_runs_window CHECK (window_from < window_to)
    )
    """,
    """
    CREATE INDEX ix_discovery_runs_pending
        ON discovery_runs (created_at)
        WHERE status = 'PENDING'
    """,
    """
    CREATE INDEX ix_discovery_runs_running
        ON discovery_runs (locked_at)
        WHERE status = 'RUNNING'
    """,
    """
    CREATE TABLE calls (
        id UUID PRIMARY KEY,
        source_call_id TEXT NOT NULL UNIQUE,
        discovered_in_run_id UUID NOT NULL REFERENCES discovery_runs(id),
        started_at TIMESTAMPTZ NOT NULL,
        advisor_phone TEXT NOT NULL,
        counterparty_phone TEXT NOT NULL,
        call_direction TEXT NOT NULL
            CHECK (call_direction IN ('INBOUND', 'OUTBOUND')),
        client_id UUID NULL REFERENCES clients(id),
        processing_decision TEXT NOT NULL
            CHECK (processing_decision IN ('PROCESS', 'SKIP')),
        skip_reason TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_calls_processing_data CHECK (
            (
                processing_decision = 'PROCESS'
                AND client_id IS NOT NULL
                AND skip_reason IS NULL
            )
            OR
            (
                processing_decision = 'SKIP'
                AND client_id IS NULL
                AND skip_reason IS NOT NULL
            )
        )
    )
    """,
    """
    CREATE INDEX ix_calls_client_started
        ON calls (client_id, started_at DESC)
    """,
    """
    CREATE INDEX ix_calls_discovery_run
        ON calls (discovered_in_run_id)
    """,
    """
    ALTER TABLE clients
        ADD CONSTRAINT fk_clients_discovered_from_call
        FOREIGN KEY (discovered_from_call_id)
        REFERENCES calls(id)
        ON DELETE SET NULL
    """,
    """
    CREATE TABLE audio_fetch_jobs (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        object_key TEXT NULL UNIQUE,
        last_error_code TEXT NULL,
        last_error_http_status SMALLINT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_audio_fetch_jobs_result CHECK (
            (status = 'COMPLETED' AND object_key IS NOT NULL)
            OR
            (status <> 'COMPLETED' AND object_key IS NULL)
        )
    )
    """,
    """
    CREATE INDEX ix_audio_fetch_jobs_pending
        ON audio_fetch_jobs (available_at)
        WHERE status = 'PENDING'
    """,
    """
    CREATE INDEX ix_audio_fetch_jobs_in_progress
        ON audio_fetch_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
    """,
    """
    CREATE TABLE audio_analysis_jobs (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        object_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        analysis_result JSONB NULL,
        analysis_schema_version TEXT NULL,
        last_error_code TEXT NULL,
        last_error_http_status SMALLINT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_audio_analysis_jobs_result CHECK (
            (
                status = 'COMPLETED'
                AND analysis_result IS NOT NULL
                AND analysis_schema_version IS NOT NULL
            )
            OR
            (
                status <> 'COMPLETED'
                AND analysis_result IS NULL
                AND analysis_schema_version IS NULL
            )
        ),
        CONSTRAINT ck_audio_analysis_jobs_result_object CHECK (
            analysis_result IS NULL
            OR jsonb_typeof(analysis_result) = 'object'
        )
    )
    """,
    """
    CREATE INDEX ix_audio_analysis_jobs_pending
        ON audio_analysis_jobs (available_at)
        WHERE status = 'PENDING'
    """,
    """
    CREATE INDEX ix_audio_analysis_jobs_in_progress
        ON audio_analysis_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
    """,
    """
    CREATE TABLE call_search (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        transcript_text TEXT NOT NULL DEFAULT '',
        summary_text TEXT NOT NULL DEFAULT '',
        client_profile_text TEXT NOT NULL DEFAULT '',
        deals_text TEXT NOT NULL DEFAULT '',
        transcript_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, transcript_text)
        ) STORED,
        summary_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, summary_text)
        ) STORED,
        client_profile_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, client_profile_text)
        ) STORED,
        deals_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, deals_text)
        ) STORED,
        search_schema_version TEXT NOT NULL,
        indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX ix_call_search_transcript
        ON call_search USING GIN (transcript_search)
    """,
    """
    CREATE INDEX ix_call_search_summary
        ON call_search USING GIN (summary_search)
    """,
    """
    CREATE INDEX ix_call_search_client_profile
        ON call_search USING GIN (client_profile_search)
    """,
    """
    CREATE INDEX ix_call_search_deals
        ON call_search USING GIN (deals_search)
    """,
)


DOWNGRADE_STATEMENTS = (
    "DROP TABLE call_search",
    "DROP TABLE audio_analysis_jobs",
    "DROP TABLE audio_fetch_jobs",
    "ALTER TABLE clients DROP CONSTRAINT fk_clients_discovered_from_call",
    "DROP TABLE calls",
    "DROP TABLE discovery_runs",
    "DROP TABLE clients",
    "DROP TABLE advisors",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
