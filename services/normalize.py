from __future__ import annotations

import re
from pathlib import Path
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional

from ruamel.yaml import YAML
from rapidfuzz import fuzz

yaml = YAML(typ="safe")  # safe loader вместо yaml.safe_load()


ATTR_TYPE_OVERRIDES: Dict[str, str] = {
    "options_container": "str",
    "url_key": "str",
}

ATTRIBUTE_INPUT_TYPE_HINTS: Dict[str, str] = {
    "options_container": "select",
    "category_ids": "multiselect",
    "quantity_and_stock_status": "boolean",
}

_BOOLEAN_TRUE = {"1", "true", "yes", "y", "on"}
_BOOLEAN_FALSE = {"0", "false", "no", "n", "off"}

@lru_cache(maxsize=1)
def load_map() -> Dict[str, Any]:
    path = Path("data/attributes_map.yaml")
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f) or {}
        return data if isinstance(data, dict) else {}

def _iter_variants(x: Any) -> Iterable[str]:
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        return [str(v) for v in x if v is not None]
    return [str(x)]

def _extract_options(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        for key in ("options", "values", "map"):
            maybe = config.get(key)
            if isinstance(maybe, dict):
                return maybe
        return {
            k: v
            for k, v in config.items()
            if not (isinstance(k, str) and k.startswith("__"))
        }
    return {}


def _extract_input_type(attr_code: str, config: Any) -> Optional[str]:
    if isinstance(config, dict):
        for key in ("input_type", "input", "type", "frontend_input"):
            value = config.get(key)
            if isinstance(value, str):
                return value.lower()
        meta = config.get("__meta__") if isinstance(config.get("__meta__"), dict) else None
        if isinstance(meta, dict):
            for key in ("input_type", "input", "type"):
                value = meta.get(key)
                if isinstance(value, str):
                    return value.lower()
    return ATTRIBUTE_INPUT_TYPE_HINTS.get(attr_code)


def _coerce_select_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            converted = _coerce_select_value(item)
            if converted is not None:
                return converted
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _coerce_select_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        for item in value:
            converted = _coerce_select_string(item)
            if converted:
                return converted
        return None
    text = str(value).strip()
    return text or None


def _coerce_multiselect(value: Any) -> List[int]:
    if value is None:
        return []

    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return []
        parts: List[Any] = re.split(r"[,;\s]+", cleaned)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    ints: List[int] = []
    for part in parts:
        if isinstance(part, (list, tuple, set)):
            ints.extend(_coerce_multiselect(part))
            continue
        if isinstance(part, bool):
            ints.append(1 if part else 0)
            continue
        if isinstance(part, (int, float)):
            ints.append(int(part))
            continue
        text = str(part).strip()
        if not text:
            continue
        digits = re.findall(r"-?\d+", text)
        if not digits:
            continue
        ints.extend(int(d) for d in digits)

    deduped: List[int] = []
    for item in ints:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _coerce_boolean(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _BOOLEAN_TRUE:
        return 1
    if text in _BOOLEAN_FALSE:
        return 0
    return None


def normalize_value(attr_code: str, raw_value: Any) -> Any:
    if raw_value is None:
        return None

    data = load_map()
    attr_config = data.get(attr_code)
    options = _extract_options(attr_config)
    input_type = _extract_input_type(attr_code, attr_config)

    text = "" if raw_value is None else str(raw_value)
    normalised_value: Any = raw_value

    for canonical, variants in options.items():
        for v in _iter_variants(variants):
            try:
                if fuzz.ratio(text.lower(), v.lower()) > 85:
                    normalised_value = canonical
                    break
            except Exception:
                continue
        else:
            continue
        break

    if attr_code == "custom_layout_update_file":
        trimmed = str(normalised_value).strip()
        if trimmed in {"", "__no_update__", "no_update"}:
            return None
        return trimmed

    if input_type == "multiselect":
        return _coerce_multiselect(normalised_value)

    if input_type in {"boolean", "bool"}:
        boolean_value = _coerce_boolean(normalised_value)
        return boolean_value

    if input_type == "select":
        if ATTR_TYPE_OVERRIDES.get(attr_code) == "str":
            return _coerce_select_string(normalised_value)
        return _coerce_select_value(normalised_value)

    return normalised_value
