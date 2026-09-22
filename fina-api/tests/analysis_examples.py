"""Synthetic adapter output for testing FinaAPI contracts, independent of any SDK."""

from uuid import UUID

TASK_ID = UUID("9b6cb9ea-2973-42a4-8524-8b47b7281a11")


def identity_data(**changes: object) -> dict[str, object]:
    return {
        "source_call_id": "source-123",
        "started_at": "2026-09-01T00:00:00Z",
        "advisor_phone": "79990000001",
        "counterparty_phone": "79990000002",
        "call_direction": "INBOUND",
    } | changes


def analysis_data(task_id: UUID = TASK_ID) -> dict:
    return {
        "kind": "success",
        "task_id": str(task_id),
        "artifacts": {
            "transcript": {
                "text": "Обсуждаем облигации и акции.",
                "segments": [
                    {
                        "start_seconds": 0.0,
                        "end_seconds": 2.5,
                        "speaker": "advisor",
                        "text": "Обсуждаем облигации и акции.",
                    }
                ],
            },
            "summary": "Купить акции Сбербанка.",
            "client_profile": {"data": {"call_answer": "Персональный ответ из этого звонка"}},
            "customer_profile": {
                "data": {
                    "experience": "Опытный инвестор",
                    "preferences": {"instruments": ["Облигации", "Акции"]},
                }
            },
            "pre_order": {
                "order_type": "BUY",
                "instrument_name": "Сбербанк",
                "volume": "100 лотов",
                "execution_date": "2026-09-30",
                "price": "Рыночная",
                "currency": "RUB",
                "additional_details": "Дополнительные реквизиты",
            },
        },
        "processed_at": "2026-09-01T00:02:00Z",
        "provider_result": {
            "task_id": str(task_id),
            "processed_at": "2026-09-01T00:02:00Z",
            "artifacts": {
                "transcript": {"text": "Обсуждаем облигации и акции."},
                "future_stage": {"confidence": 0.75},
            },
            "future_metadata": {"values": ["новое поле", 42, True, None]},
        },
    }
