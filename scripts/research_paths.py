"""Resolve research artifacts after moving their roots into research_results/."""

from pathlib import Path

LEGACY_ROOTS = {"runs", "ablation_results_large", "ablation_results_small", "ablation_results"}


def resolve_artifact_path(root, value):
    root = Path(root).resolve()
    value = Path(value)
    absolute = value if value.is_absolute() else root / value
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        return absolute
    if relative.parts and relative.parts[0] in LEGACY_ROOTS:
        return root / "research_results" / relative
    return absolute


def configurations_match(root, first, second):
    """Location changes do not change experiment identity; hashes still must match."""
    def normalize(value):
        if isinstance(value, dict):
            return {key: str(resolve_artifact_path(root, item)) if key == "source_run" and isinstance(item, str)
                    else normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return normalize(first) == normalize(second)
