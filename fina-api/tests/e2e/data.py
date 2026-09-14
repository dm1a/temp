import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from tests.e2e.stack import ComposeStack

START = datetime(2026, 1, 1, 9, tzinfo=UTC)
ADVISOR = "79990000001"
CLIENT = "79990000002"
OTHER_ADVISOR = "79990000003"
OTHER_CLIENT = "79990000004"


async def seed_transcripts(stack: ComposeStack) -> None:
    """Seed completed worker output; worker loops are not implemented yet."""
    connection = await asyncpg.connect(stack.database_url, timeout=5, command_timeout=10)
    try:
        async with connection.transaction():
            run_id = UUID(int=100)
            await connection.execute(
                """
                INSERT INTO discovery_runs (id, window_from, window_to, status)
                VALUES ($1, $2, $3, 'COMPLETED')
                """,
                run_id,
                START,
                START + timedelta(days=1),
            )
            await connection.executemany(
                "INSERT INTO advisors (phone_normalized) VALUES ($1)",
                [(ADVISOR,), (OTHER_ADVISOR,)],
            )
            await connection.executemany(
                "INSERT INTO clients (id, phone_normalized) VALUES ($1, $2)",
                [(UUID(int=101), CLIENT), (UUID(int=102), OTHER_CLIENT)],
            )
            for number, hour, advisor, client_id, client in [
                (1, 0, ADVISOR, UUID(int=101), CLIENT),
                (2, 1, ADVISOR, UUID(int=101), CLIENT),
                (3, 2, ADVISOR, UUID(int=101), CLIENT),
                (4, 1, OTHER_ADVISOR, UUID(int=101), CLIENT),
                (5, 1, ADVISOR, UUID(int=102), OTHER_CLIENT),
            ]:
                call_id = UUID(int=number)
                object_key = f"e2e/audio/{number}"
                transcript = f"Обсуждаем облигации, запись {number}"
                summary = f"Покупка акций, итог {number}"
                await connection.execute(
                    """
                    INSERT INTO calls (
                        id, source_call_id, discovered_in_run_id, started_at,
                        advisor_phone, counterparty_phone, call_direction,
                        mts_filename, call_duration_sec, rec_duration_sec,
                        client_id, processing_decision
                    ) VALUES ($1, $2, $3, $4, $5, $6, 'INBOUND', $7, 120, 118, $8, 'PROCESS')
                    """,
                    call_id,
                    f"e2e-call-{number}",
                    run_id,
                    START + timedelta(hours=hour),
                    advisor,
                    client,
                    f"e2e-call-{number}.mp3",
                    client_id,
                )
                await connection.execute(
                    """
                    UPDATE clients SET discovered_from_call_id = $1
                    WHERE id = $2 AND discovered_from_call_id IS NULL
                    """,
                    call_id,
                    client_id,
                )
                await connection.execute(
                    """
                    INSERT INTO audio_fetch_jobs (call_id, status, attempts, object_key)
                    VALUES ($1, 'COMPLETED', 1, $2)
                    """,
                    call_id,
                    object_key,
                )
                await connection.execute(
                    """
                    INSERT INTO audio_analysis_jobs (
                        call_id, object_key, status, attempts,
                        analysis_result, analysis_schema_version
                    ) VALUES ($1, $2, 'COMPLETED', 1, $3::jsonb, '1')
                    """,
                    call_id,
                    object_key,
                    json.dumps({"transcript": transcript, "summary": summary}),
                )
                await connection.execute(
                    """
                    INSERT INTO call_search (
                        call_id, transcript_text, summary_text, search_schema_version
                    ) VALUES ($1, $2, $3, '1')
                    """,
                    call_id,
                    transcript,
                    summary,
                )
    finally:
        await connection.close()
