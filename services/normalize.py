"""Helpers for normalising attribute values before Magento updates."""

from __future__ import annotations

from copy import deepcopy
import html
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from rapidfuzz import fuzz
from ruamel.yaml import YAML


yaml = YAML(typ="safe")  # safe loader вместо yaml.safe_load()

ATTR_TYPE_OVERRIDES: Dict[str, str] = {
    # URL key behaves like a plain text field even if the backend type differs.
    "url_key": "text",
    "options_container": "text",
}

BOOL_TRUE = {"1", "true", "yes", "y", "on"}
BOOL_FALSE = {"0", "false", "no", "n", "off"}


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


def _canonicalise_free_text(attr_code: str, raw_value: Any) -> Any:
    data = load_map()
    options = data.get(attr_code)
    if not options:
        return raw_value

    text = "" if raw_value is None else str(raw_value)
    for canonical, variants in options.items():
        for candidate in _iter_variants(variants):
            try:
                if fuzz.ratio(text.lower(), candidate.lower()) > 85:
                    return canonical
            except Exception:
                continue
    return raw_value


def _normalise_bool(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1 if raw else 0
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return 1 if int(raw) else 0
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        lowered = stripped.lower()
        if lowered in BOOL_TRUE:
            return 1
        if lowered in BOOL_FALSE:
            return 0
    raise ValueError("Expected a boolean-compatible value")


def _normalise_decimal(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        dec = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Cannot convert '{raw}' to decimal") from None
    return format(dec.normalize(), 'f')


def _ensure_sequence(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        return parts
    if isinstance(raw, (list, tuple, set)):
        return [item for item in raw if item not in (None, "")]
    return [raw]


def _options_from_meta(meta: Dict[str, Any]) -> List[Dict[str, str]]:
    options: Sequence[Any] = meta.get("options") or []
    normalized: List[Dict[str, str]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        raw_value = option.get("value")
        if raw_value in (None, ""):
            continue
        label = option.get("label") or option.get("label_default") or option.get("labelDefault")
        normalized.append(
            {
                "value": str(raw_value),
                "label": str(label) if label is not None else str(raw_value),
            }
        )
    return normalized


def _map_to_option_id(raw: Any, meta: Dict[str, Any]) -> Optional[str]:
    options = _options_from_meta(meta)
    if not options:
        if raw is None:
            return None
        return str(raw)

    by_value = {opt["value"]: opt["value"] for opt in options}
    by_label = {opt["label"].lower(): opt["value"] for opt in options}

    if raw is None:
        return None
    candidate = raw
    if isinstance(candidate, dict):
        candidate = candidate.get("value") or candidate.get("option_id") or candidate.get(
            "optionId"
        )

    if candidate is None:
        return None

    candidate_str = str(candidate).strip()
    if not candidate_str:
        return None

    if candidate_str in by_value:
        return by_value[candidate_str]

    lowered = candidate_str.lower()
    if lowered in by_label:
        return by_label[lowered]

    raise ValueError(f"Value '{candidate}' is not a valid option")


def _normalise_multiselect(raw: Any, meta: Dict[str, Any]) -> Optional[str]:
    values = []
    for part in _ensure_sequence(raw):
        option_id = _map_to_option_id(part, meta)
        if option_id is None:
            continue
        if option_id not in values:
            values.append(option_id)
    if not values:
        return None
    return ",".join(values)


def _normalise_select(raw: Any, meta: Dict[str, Any]) -> Optional[int]:
    option_id = _map_to_option_id(raw, meta)
    if option_id is None:
        return None
    try:
        return int(option_id)
    except ValueError as exc:  # pragma: no cover - defensive for unexpected data
        raise ValueError(f"Option id '{option_id}' for select '{meta.get('attribute_code')}' is not an int") from exc


def _determine_input_type(code: str, meta: Dict[str, Any]) -> str:
    override = ATTR_TYPE_OVERRIDES.get(code)
    if override:
        return override
    for key in ("input", "frontend_input", "frontendInput"):
        if key in meta and meta[key]:
            return str(meta[key]).lower()
    return "text"


def normalize_value(attr_code: str, raw_value: Any, meta: Optional[Dict[str, Any]] = None) -> Any:
    """Normalise ``raw_value`` according to Magento attribute metadata.

    The function is pure: it returns a converted value without mutating the
    provided metadata.
    """

    meta = deepcopy(meta or {})
    input_type = _determine_input_type(attr_code, meta)
    backend_type = str(meta.get("backend_type") or "").lower()

    if raw_value is None:
        return None
    if isinstance(raw_value, str) and not raw_value.strip():
        return None

    if attr_code == "custom_layout_update_file":
        lowered = str(raw_value).strip().lower()
        if lowered in {"__no_update__", "no_update", ""}:
            return None

    if attr_code == "description" and isinstance(raw_value, str):
        cleaned = html.unescape(raw_value)
        cleaned = cleaned.replace("\\\\", "\\").replace("\\", "")
        cleaned = cleaned.replace("\n", "").replace("\r", "")
        return cleaned.strip() or None

    if attr_code == "category_ids":
        sequence = _ensure_sequence(raw_value)
        if not sequence:
            return None
        try:
            return [int(str(v)) for v in sequence]
        except ValueError as exc:
            raise ValueError("Category ids must be integers") from exc

    if input_type == "multiselect":
        return _normalise_multiselect(raw_value, meta)

    if input_type == "select":
        return _normalise_select(raw_value, meta)

    if input_type in {"boolean", "bool"}:
        return _normalise_bool(raw_value)

    if backend_type == "decimal" or input_type in {"price"}:
        return _normalise_decimal(raw_value)

    # For all other textual fields attempt to canonicalise with the YAML map.
    return _canonicalise_free_text(attr_code, raw_value)

