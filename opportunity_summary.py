"""User-triggered, localized job explanations with cache reuse."""
import ai_requests
import ai_service


VERSION = "listing-summary-v1"


def _extra(language: str) -> dict:
    return {"language": language, "version": VERSION}


def cached(opportunity: dict, language: str):
    return ai_service.cached_task(
        "listing_summary", opportunity, profile={}, extra=_extra(language)
    )


@ai_requests.serialized("listing-summary")
def generate(opportunity: dict, language: str) -> dict:
    outcome = ai_service.run_task(
        "listing_summary", opportunity, profile={}, extra=_extra(language)
    )
    return {
        "ok": outcome.ok,
        "cached": outcome.cached,
        "data": outcome.data,
        "error": outcome.error,
    }
