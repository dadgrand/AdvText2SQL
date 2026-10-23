"""Fixed feature configurations used in reproducible comparisons."""

HYBRID_FEATURES = (2, 3, 4, 8, 12, 17, 18, 19, 27, 29, 30, 32, 33, 35)


def feature_flags(configuration):
    if configuration not in ("baseline", "hybrid", "hybrid_no32_33"):
        raise ValueError(f"Unknown feature configuration: {configuration}")
    enabled = set(HYBRID_FEATURES) if configuration != "baseline" else set()
    if configuration == "hybrid_no32_33":
        enabled -= {32, 33}
    return {f"FEAT_{number}": number in enabled for number in range(1, 37)}
