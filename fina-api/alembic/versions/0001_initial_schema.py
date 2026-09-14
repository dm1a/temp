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

# Each constant holds exactly one SQL statement. The async engine executes
# migrations over asyncpg, whose prepared-statement protocol rejects a
# string containing more than one command ("cannot insert multiple commands
# into a prepared statement") -- unlike psycopg2's simple query protocol,
# semicolon-separated batches are not an option here.

ADVISORS_CREATE_TABLE = """
    CREATE TABLE advisors (
        phone_normalized TEXT PRIMARY KEY,
        is_active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

CLIENTS_CREATE_TABLE = """
    CREATE TABLE clients (
        id UUID PRIMARY KEY,
        phone_normalized TEXT NOT NULL UNIQUE,
        discovered_from_call_id UUID NULL,
        customer_profile JSONB NULL,
        customer_profile_updated_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_clients_customer_profile_object CHECK (
            customer_profile IS NULL OR jsonb_typeof(customer_profile) = 'object'
        )
    )
"""

DISCOVERY_RUNS_CREATE_TABLE = """
    CREATE TABLE discovery_runs (
        id UUID PRIMARY KEY,
        window_from TIMESTAMPTZ NOT NULL,
        window_to TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        claim_token UUID NULL,
        error_code TEXT NULL,
        error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_discovery_runs_window UNIQUE (window_from, window_to),
        CONSTRAINT ck_discovery_runs_window CHECK (window_from < window_to),
        CONSTRAINT ck_discovery_runs_claim CHECK (
            (
                status = 'RUNNING'
                AND claim_token IS NOT NULL
                AND locked_by IS NOT NULL
                AND locked_at IS NOT NULL
            ) OR (
                status <> 'RUNNING'
                AND claim_token IS NULL
                AND locked_by IS NULL
                AND locked_at IS NULL
            )
        )
    )
"""

DISCOVERY_RUNS_CREATE_INDEX_PENDING = """
    CREATE INDEX ix_discovery_runs_pending
        ON discovery_runs (created_at)
        WHERE status = 'PENDING'
"""

DISCOVERY_RUNS_CREATE_INDEX_RUNNING = """
    CREATE INDEX ix_discovery_runs_running
        ON discovery_runs (locked_at)
        WHERE status = 'RUNNING'
"""

DISCOVERY_RUNS_CREATE_UNIQUE_INDEX_ACTIVE = """
    CREATE UNIQUE INDEX uq_discovery_runs_active
        ON discovery_runs ((1))
        WHERE status IN ('PENDING', 'RUNNING')
"""

CALLS_CREATE_TABLE = """
    CREATE TABLE calls (
        id UUID PRIMARY KEY,
        source_call_id TEXT NOT NULL UNIQUE,
        discovered_in_run_id UUID NOT NULL REFERENCES discovery_runs(id),
        started_at TIMESTAMPTZ NOT NULL,
        advisor_phone TEXT NOT NULL,
        counterparty_phone TEXT NOT NULL,
        call_direction TEXT NOT NULL
            CHECK (call_direction IN ('INBOUND', 'OUTBOUND')),
        mts_filename TEXT NOT NULL,
        call_duration_sec INTEGER NOT NULL CHECK (call_duration_sec > 0),
        rec_duration_sec INTEGER NOT NULL CHECK (rec_duration_sec > 0),
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
"""

CALLS_CREATE_INDEX_CLIENT_STARTED = """
    CREATE INDEX ix_calls_client_started
        ON calls (client_id, started_at DESC)
"""

CALLS_CREATE_INDEX_DISCOVERY_RUN = """
    CREATE INDEX ix_calls_discovery_run
        ON calls (discovered_in_run_id)
"""

CALLS_CREATE_INDEX_CALLS_PHONES_STARTED = """
    CREATE INDEX ix_calls_phones_started
        ON calls (advisor_phone, counterparty_phone, started_at DESC, id DESC)
"""

ADD_CONSTRAINT_BETWEEN_CLIENTS_AND_CALLS = """
    ALTER TABLE clients
        ADD CONSTRAINT fk_clients_discovered_from_call
        FOREIGN KEY (discovered_from_call_id)
        REFERENCES calls(id)
        ON DELETE SET NULL
"""

AUDIO_FETCH_JOBS_CREATE_TABLE = """
    CREATE TABLE audio_fetch_jobs (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        claim_token UUID NULL,
        object_key TEXT NULL UNIQUE,
        last_error_code TEXT NULL,
        last_error_http_status SMALLINT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_audio_fetch_jobs_claim CHECK (
            (
                status = 'IN_PROGRESS'
                AND claim_token IS NOT NULL
                AND locked_by IS NOT NULL
                AND locked_at IS NOT NULL
            ) OR (
                status <> 'IN_PROGRESS'
                AND claim_token IS NULL
                AND locked_by IS NULL
                AND locked_at IS NULL
            )
        ),
        CONSTRAINT ck_audio_fetch_jobs_result CHECK (
            (status = 'COMPLETED' AND object_key IS NOT NULL)
            OR
            (status <> 'COMPLETED' AND object_key IS NULL)
        )
    )
"""

AUDIO_FETCH_JOBS_CREATE_INDEX_PENDING = """
    CREATE INDEX ix_audio_fetch_jobs_pending
        ON audio_fetch_jobs (available_at)
        WHERE status = 'PENDING'
"""

AUDIO_FETCH_JOBS_CREATE_INDEX_IN_PROGRESS = """
    CREATE INDEX ix_audio_fetch_jobs_in_progress
        ON audio_fetch_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
"""

AUDIO_ANALYSIS_JOBS_CREATE_TABLE = """
    CREATE TABLE audio_analysis_jobs (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        object_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        claim_token UUID NULL,
        analysis_result JSONB NULL,
        analysis_schema_version TEXT NULL,
        last_error_code TEXT NULL,
        last_error_http_status SMALLINT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_audio_analysis_jobs_claim CHECK (
            (
                status = 'IN_PROGRESS'
                AND claim_token IS NOT NULL
                AND locked_by IS NOT NULL
                AND locked_at IS NOT NULL
            ) OR (
                status <> 'IN_PROGRESS'
                AND claim_token IS NULL
                AND locked_by IS NULL
                AND locked_at IS NULL
            )
        ),
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
"""

AUDIO_ANALYSIS_JOBS_CREATE_INDEX_PENDING = """
    CREATE INDEX ix_audio_analysis_jobs_pending
        ON audio_analysis_jobs (available_at)
        WHERE status = 'PENDING'
"""

AUDIO_ANALYSIS_JOBS_CREATE_INDEX_IN_PROGRESS = """
    CREATE INDEX ix_audio_analysis_jobs_in_progress
        ON audio_analysis_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
"""

CLIENT_PROFILE_JOBS_CREATE_TABLE = """
    CREATE TABLE client_profile_jobs (
        client_id UUID PRIMARY KEY REFERENCES clients(id),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        -- The request_version that `attempts` was last counted against.
        -- claim_next() compares this to the row's current request_version:
        -- on a match it's a genuine retry of the same generation and
        -- attempts increments; on a mismatch (a newer enqueue() arrived,
        -- whether that was noticed by a normal retry or only discovered
        -- via recover_stale() after a crash) it's the new generation's
        -- first attempt, so attempts restarts at 1 instead of inheriting an
        -- unrelated, already-superseded generation's count.
        attempts_request_version BIGINT NOT NULL DEFAULT 0,
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- Incremented by every enqueue(), even while IN_PROGRESS (an integer
        -- counter rather than now(), since now() returns transaction start
        -- time in Postgres -- an earlier-starting, later-committing
        -- transaction could otherwise move a timestamp backwards).
        -- mark_completed()/mark_failed() compare this against the value
        -- captured at claim time, so a call that finished during processing
        -- isn't lost: it re-queues instead of completing/failing on stale data.
        request_version BIGINT NOT NULL DEFAULT 1,
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        claim_token UUID NULL,
        last_error_code TEXT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_client_profile_jobs_claim CHECK (
            (
                status = 'IN_PROGRESS'
                AND claim_token IS NOT NULL
                AND locked_by IS NOT NULL
                AND locked_at IS NOT NULL
            ) OR (
                status <> 'IN_PROGRESS'
                AND claim_token IS NULL
                AND locked_by IS NULL
                AND locked_at IS NULL
            )
        )
    )
"""

CLIENT_PROFILE_JOBS_CREATE_INDEX_PENDING = """
    CREATE INDEX ix_client_profile_jobs_pending
        ON client_profile_jobs (available_at)
        WHERE status = 'PENDING'
"""

CLIENT_PROFILE_JOBS_CREATE_INDEX_IN_PROGRESS = """
    CREATE INDEX ix_client_profile_jobs_in_progress
        ON client_profile_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
"""

SEND_ORDER_JOBS_CREATE_TABLE = """
    CREATE TABLE send_order_jobs (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        pre_order JSONB NOT NULL,
        client_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        locked_by TEXT NULL,
        locked_at TIMESTAMPTZ NULL,
        claim_token UUID NULL,
        last_error_code TEXT NULL,
        last_error_message TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_send_order_jobs_claim CHECK (
            (
                status = 'IN_PROGRESS'
                AND claim_token IS NOT NULL
                AND locked_by IS NOT NULL
                AND locked_at IS NOT NULL
            ) OR (
                status <> 'IN_PROGRESS'
                AND claim_token IS NULL
                AND locked_by IS NULL
                AND locked_at IS NULL
            )
        ),
        CONSTRAINT ck_send_order_jobs_pre_order_object CHECK (
            jsonb_typeof(pre_order) = 'object'
        )
    )
"""

SEND_ORDER_JOBS_CREATE_INDEX_PENDING = """
    CREATE INDEX ix_send_order_jobs_pending
        ON send_order_jobs (available_at)
        WHERE status = 'PENDING'
"""

SEND_ORDER_JOBS_CREATE_INDEX_IN_PROGRESS = """
    CREATE INDEX ix_send_order_jobs_in_progress
        ON send_order_jobs (locked_at)
        WHERE status = 'IN_PROGRESS'
"""

CALL_SEARCH_CREATE_TABLE = """
    CREATE TABLE call_search (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        transcript_text TEXT NOT NULL DEFAULT '',
        summary_text TEXT NOT NULL DEFAULT '',
        client_profile_text TEXT NOT NULL DEFAULT '',
        orders_text TEXT NOT NULL DEFAULT '',
        transcript_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, transcript_text)
        ) STORED,
        summary_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, summary_text)
        ) STORED,
        client_profile_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, client_profile_text)
        ) STORED,
        orders_search TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('pg_catalog.russian'::regconfig, orders_text)
        ) STORED,
        search_schema_version TEXT NOT NULL,
        indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

CALL_SEARCH_CREATE_INDEX_TRANSCRIPT = """
    CREATE INDEX ix_call_search_transcript
        ON call_search USING GIN (transcript_search)
"""

CALL_SEARCH_CREATE_INDEX_SUMMARY = """
    CREATE INDEX ix_call_search_summary
        ON call_search USING GIN (summary_search)
"""

CALL_SEARCH_CREATE_INDEX_CLIENT_PROFILE = """
    CREATE INDEX ix_call_search_client_profile
        ON call_search USING GIN (client_profile_search)
"""

CALL_SEARCH_CREATE_INDEX_ORDERS = """
    CREATE INDEX ix_call_search_orders
        ON call_search USING GIN (orders_search)
"""

ORDERS_CREATE_TABLE = """
    CREATE TABLE orders (
        call_id UUID PRIMARY KEY REFERENCES calls(id),
        order_type TEXT NULL
            CHECK (order_type IN ('BUY', 'SELL')),
        instrument_name TEXT NULL,
        volume TEXT NULL,
        execution_date DATE NULL,
        price TEXT NULL,
        currency TEXT NULL,
        additional_details TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""

ORDERS_CREATE_INDEX_TYPE_CREATED = """
    CREATE INDEX ix_orders_type_created
        ON orders (order_type, created_at DESC)
"""

UPGRADE_STATEMENTS = (
    ADVISORS_CREATE_TABLE,
    CLIENTS_CREATE_TABLE,
    DISCOVERY_RUNS_CREATE_TABLE,
    DISCOVERY_RUNS_CREATE_INDEX_PENDING,
    DISCOVERY_RUNS_CREATE_INDEX_RUNNING,
    DISCOVERY_RUNS_CREATE_UNIQUE_INDEX_ACTIVE,
    CALLS_CREATE_TABLE,
    CALLS_CREATE_INDEX_CLIENT_STARTED,
    CALLS_CREATE_INDEX_DISCOVERY_RUN,
    CALLS_CREATE_INDEX_CALLS_PHONES_STARTED,
    ADD_CONSTRAINT_BETWEEN_CLIENTS_AND_CALLS,
    AUDIO_FETCH_JOBS_CREATE_TABLE,
    AUDIO_FETCH_JOBS_CREATE_INDEX_PENDING,
    AUDIO_FETCH_JOBS_CREATE_INDEX_IN_PROGRESS,
    AUDIO_ANALYSIS_JOBS_CREATE_TABLE,
    AUDIO_ANALYSIS_JOBS_CREATE_INDEX_PENDING,
    AUDIO_ANALYSIS_JOBS_CREATE_INDEX_IN_PROGRESS,
    CLIENT_PROFILE_JOBS_CREATE_TABLE,
    CLIENT_PROFILE_JOBS_CREATE_INDEX_PENDING,
    CLIENT_PROFILE_JOBS_CREATE_INDEX_IN_PROGRESS,
    SEND_ORDER_JOBS_CREATE_TABLE,
    SEND_ORDER_JOBS_CREATE_INDEX_PENDING,
    SEND_ORDER_JOBS_CREATE_INDEX_IN_PROGRESS,
    CALL_SEARCH_CREATE_TABLE,
    CALL_SEARCH_CREATE_INDEX_TRANSCRIPT,
    CALL_SEARCH_CREATE_INDEX_SUMMARY,
    CALL_SEARCH_CREATE_INDEX_CLIENT_PROFILE,
    CALL_SEARCH_CREATE_INDEX_ORDERS,
    ORDERS_CREATE_TABLE,
    ORDERS_CREATE_INDEX_TYPE_CREATED,
)


DOWNGRADE_STATEMENTS = (
    "DROP TABLE orders",
    "DROP TABLE call_search",
    "DROP TABLE send_order_jobs",
    "DROP TABLE client_profile_jobs",
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
