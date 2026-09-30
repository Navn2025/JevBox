import os


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    return float(value)


JEV_BATCH_SIZE = min(100, max(1, int(os.getenv("JEV_BATCH_SIZE", "100"))))
JEV_BATCH_DEBOUNCE_MS = max(100, int(os.getenv("JEV_BATCH_DEBOUNCE_MS", "450")))

# Pricing is deliberately optional. Costs remain unavailable until the owner
# supplies prices for the configured provider/model.
JEV_INPUT_COST_PER_1M_TOKENS = _optional_float("JEV_INPUT_COST_PER_1M_TOKENS")
JEV_OUTPUT_COST_PER_1M_TOKENS = _optional_float("JEV_OUTPUT_COST_PER_1M_TOKENS")

GMAIL_PAGE_SIZE = min(50, max(1, int(os.getenv("GMAIL_PAGE_SIZE", "20"))))
SESSION_MAX_AGE_DAYS = max(1, int(os.getenv("SESSION_MAX_AGE_DAYS", "30")))
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
