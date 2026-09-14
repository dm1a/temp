from fina.domain.audio_analysis import AnalyzeResult
from fina.services.analysis_indexing import build_search_text
from tests.analysis_examples import analysis_data


def test_search_uses_complete_transcript_and_order_fields() -> None:
    result = AnalyzeResult.model_validate(analysis_data())
    search = build_search_text(result.artifacts)

    assert search.transcript_text == "Обсуждаем облигации и акции."
    assert search.summary_text == "Купить акции Сбербанка."
    assert search.orders_text == (
        "BUY\nСбербанк\n100 лотов\nРыночная\nRUB\nДополнительные реквизиты"
    )
    assert search.search_schema_version == "1"


def test_profile_search_uses_per_call_client_profile_not_customer_profile() -> None:
    """client_profile (per-call) is always present when analysis completes;
    customer_profile is only computed later, asynchronously. The search text
    must come from the former, or it would always be empty."""
    data = analysis_data()
    data["artifacts"]["client_profile"]["data"] = {
        "z_key_not_indexed": {"risk_key_not_indexed": "Умеренный риск"},
        "a_key_not_indexed": ["Инвестор", 42, 3.5, True, False, None, ""],
    }
    search = build_search_text(AnalyzeResult.model_validate(data).artifacts)

    assert search.client_profile_text == "Инвестор\n42\n3.5\ntrue\nfalse\nУмеренный риск"


def test_missing_pre_order_produces_empty_orders_text() -> None:
    data = analysis_data()
    del data["artifacts"]["pre_order"]
    result = AnalyzeResult.model_validate(data)

    search = build_search_text(result.artifacts)

    assert search.orders_text == ""


def test_profile_search_is_deterministic_across_object_key_order() -> None:
    first, second = analysis_data(), analysis_data()
    first["artifacts"]["client_profile"]["data"] = {
        "b": {"y": "Акции", "x": "Облигации"},
        "a": "Инвестор",
    }
    second["artifacts"]["client_profile"]["data"] = {
        "a": "Инвестор",
        "b": {"x": "Облигации", "y": "Акции"},
    }

    assert build_search_text(AnalyzeResult.model_validate(first).artifacts) == build_search_text(
        AnalyzeResult.model_validate(second).artifacts
    )
