import json
from collections.abc import Iterator

from pydantic import JsonValue

from fina.domain.audio_analysis import AnalysisArtifacts, AnalysisSearchText, PreOrder


def _profile_values(value: JsonValue) -> Iterator[str]:
    """Yield scalar values in deterministic order, excluding object keys and nulls."""
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _profile_values(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _profile_values(item)
    elif isinstance(value, str):
        if value:
            yield value
    elif value is not None:
        yield json.dumps(value, allow_nan=False)


def build_search_text(artifacts: AnalysisArtifacts) -> AnalysisSearchText:
    return AnalysisSearchText(
        transcript_text=artifacts.transcript.text,
        summary_text=artifacts.summary,
        # customer_profile is never set at this point in the flow -- it's
        # computed later, asynchronously, by ClientProfileWorker. client_profile
        # (the per-call data) is always present here, so index that instead.
        client_profile_text="\n".join(_profile_values(artifacts.client_profile.data)),
        orders_text=("\n".join(_order_values(artifacts.pre_order)) if artifacts.pre_order else ""),
    )


def _order_values(pre_order: PreOrder) -> Iterator[str]:
    for field in (
        pre_order.order_type.value if pre_order.order_type else None,
        pre_order.instrument_name,
        pre_order.volume,
        pre_order.price,
        pre_order.currency,
        pre_order.additional_details,
    ):
        if field:
            yield field
