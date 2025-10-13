"""Helpers for mapping fuzzy LLM output to Magento option values."""

from pathlib import Path

import ruamel.yaml as yaml
from rapidfuzz import fuzz


def load_map():
    """Load the attribute normalization map from disk if it exists."""
    path = Path("data/attributes_map.yaml")
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_value(attr_code, raw_value):
    """Return the canonical value for a Magento attribute if a close match is found."""
    data = load_map()
    if attr_code not in data:
        return raw_value
    options = data[attr_code]
    for canonical, variants in options.items():
        if any(fuzz.ratio(raw_value.lower(), v.lower()) > 85 for v in variants):
            return canonical
    return raw_value
