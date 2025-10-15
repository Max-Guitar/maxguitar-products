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


def _options_from_meta(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    options: Sequence[Any] = meta.get("options") or []
    normalized: List[Dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("value")
        if option_id in (None, ""):
            continue
        id_str = str(option_id).strip()
        if not id_str:
            continue
        label = option.get("label") or option.get("label_default") or option.get("labelDefault")
        label_str = str(label) if label is not None else id_str
        candidates: List[str] = []
        for candidate in (
            id_str,
            option.get("raw_value"),
            option.get("value"),
            option.get("label"),
            option.get("label_default"),
            option.get("labelDefault"),
        ):
            if candidate in (None, ""):
                continue
            candidate_str = str(candidate)
            if candidate_str not in candidates:
                candidates.append(candidate_str)
        normalized.append(
            {
                "id": id_str,
                "label": label_str,
                "candidates": candidates,
            }
        )
    return normalized


def _normalise_option_token(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = html.unescape(str(value)).strip()
    if not text:
        return None
    return text.lower()


def resolve_option_ids(code: str, raw_value: Any, meta: Dict[str, Any]) -> List[int]:
    options = _options_from_meta(meta)
    id_map: Dict[str, int] = {}
    norm_map: Dict[str, int] = {}

    for option in options:
        raw_id = option.get("id")
        if raw_id in (None, ""):
            continue
        id_str = str(raw_id).strip()
        if not id_str:
            continue
        try:
            option_id = int(id_str)
        except ValueError as exc:
            raise ValueError(
                f"Option id '{raw_id}' for attribute '{code}' is not numeric"
            ) from exc
        id_map.setdefault(id_str, option_id)
        norm_key = _normalise_option_token(id_str)
        if norm_key:
            norm_map.setdefault(norm_key, option_id)
        for candidate in option.get("candidates", []):
            norm_candidate = _normalise_option_token(candidate)
            if norm_candidate:
                norm_map.setdefault(norm_candidate, option_id)

    def _match_token(token: Any, *, allow_fallback: bool) -> Optional[int]:
        if token in (None, ""):
            return None
        if isinstance(token, dict):
            for key in ("option_id", "optionId", "value", "label"):
                if key in token:
                    resolved = _match_token(
                        token.get(key), allow_fallback=allow_fallback
                    )
                    if resolved is not None:
                        return resolved
            return None
        if isinstance(token, (list, tuple, set)):
            for item in token:
                resolved = _match_token(item, allow_fallback=allow_fallback)
                if resolved is not None:
                    return resolved
            return None
        token_str = str(token).strip()
        if not token_str:
            return None
        if options:
            if token_str in id_map:
                return id_map[token_str]
            norm_token = _normalise_option_token(token_str)
            if norm_token and norm_token in norm_map:
                return norm_map[norm_token]
            return None
        if not allow_fallback:
            return None
        try:
            return int(token_str)
        except ValueError as exc:
            raise ValueError(f"Value '{token}' is not a valid option for '{code}'") from exc

    resolved: List[int] = []
    if raw_value is None:
        return resolved

    candidates = _ensure_sequence(raw_value)
    for candidate in candidates:
        option_id = _match_token(candidate, allow_fallback=not bool(options))
        if option_id is None:
            raise ValueError(f"Value '{candidate}' is not a valid option for '{code}'")
        if option_id not in resolved:
            resolved.append(option_id)
    return resolved


def _normalise_multiselect(code: str, raw: Any, meta: Dict[str, Any]) -> Optional[str]:
    option_ids = resolve_option_ids(code, raw, meta)
    if not option_ids:
        return None
    return ",".join(str(option_id) for option_id in option_ids)


def _normalise_select(code: str, raw: Any, meta: Dict[str, Any]) -> Optional[int]:
    option_ids = resolve_option_ids(code, raw, meta)
    if not option_ids:
        return None
    if len(option_ids) > 1:
        raise ValueError(
            f"Multiple option ids resolved for single-select attribute '{code}'"
        )
    return option_ids[0]


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
        return _normalise_multiselect(attr_code, raw_value, meta)

    if input_type == "select":
        return _normalise_select(attr_code, raw_value, meta)

    if input_type in {"boolean", "bool"}:
        return _normalise_bool(raw_value)

    if backend_type == "decimal" or input_type in {"price"}:
        return _normalise_decimal(raw_value)

    # For all other textual fields attempt to canonicalise with the YAML map.
    return _canonicalise_free_text(attr_code, raw_value)

