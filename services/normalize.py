"""Helpers for normalising attribute values before Magento updates."""

from __future__ import annotations

from copy import deepcopy
import logging
import html
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

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


logger = logging.getLogger(__name__)


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
    normalised: List[Dict[str, Any]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        raw_value = option.get("value")
        if raw_value in (None, ""):
            continue
        value_str = str(raw_value).strip()
        if not value_str:
            continue
        try:
            option_id = int(value_str)
        except ValueError:
            logger.debug(
                "Skipping non-numeric option id", extra={"value": raw_value}
            )
            continue

        tokens: set[str] = set()
        for candidate in (
            value_str,
            option.get("raw_value"),
            option.get("value"),
            option.get("label"),
            option.get("label_default"),
            option.get("labelDefault"),
        ):
            if candidate in (None, ""):
                continue
            if isinstance(candidate, (list, tuple, set)):
                iter_values: Iterable[Any] = candidate
            else:
                iter_values = [candidate]
            for item in iter_values:
                token = _normalise_option_token(item)
                if token:
                    tokens.add(token)

        normalised.append({"id": option_id, "value": value_str, "tokens": tokens})
    return normalised


def _normalise_option_token(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = html.unescape(str(value)).strip()
    if not text:
        return None
    return text.lower()


_ATTRIBUTE_META_CACHE: Dict[str, Dict[str, Any]] = {}


def resolve_option_ids(
    code: str,
    raw_value: Any,
    fetch_meta_func: Callable[[str], Dict[str, Any]],
) -> Any:
    """Resolve option identifiers for ``code`` based on ``raw_value``.

    Returns ``int`` for select attributes and comma-separated ``str`` for
    multiselect attributes. When no option can be determined, ``None`` is
    returned and a warning is logged.
    """

    if raw_value in (None, ""):
        return None

    if code not in _ATTRIBUTE_META_CACHE:
        try:
            meta = fetch_meta_func(code) or {}
        except Exception as exc:  # pragma: no cover - network failure fallback
            logger.warning(
                "Failed to fetch attribute metadata", extra={"code": code, "error": str(exc)}
            )
            meta = {}
        _ATTRIBUTE_META_CACHE[code] = deepcopy(meta)

    meta = deepcopy(_ATTRIBUTE_META_CACHE.get(code, {}))
    options = _options_from_meta(meta)
    input_type = _determine_input_type(code, meta)
    is_multiselect = input_type == "multiselect"

    id_map: Dict[str, int] = {option["value"]: option["id"] for option in options}
    norm_map: Dict[str, int] = {}
    for option in options:
        for token in option.get("tokens", set()):
            norm_map.setdefault(token, option["id"])

    allow_numeric_fallback = not bool(options)

    def _match_token(token: Any) -> Optional[int]:
        if token in (None, ""):
            return None
        if isinstance(token, dict):
            for key in ("option_id", "optionId", "value", "label"):
                if key in token:
                    resolved = _match_token(token.get(key))
                    if resolved is not None:
                        return resolved
            return None
        if isinstance(token, (list, tuple, set)):
            for item in token:
                resolved = _match_token(item)
                if resolved is not None:
                    return resolved
            return None
        token_str = str(token).strip()
        if not token_str:
            return None
        if token_str in id_map:
            return id_map[token_str]
        norm_token = _normalise_option_token(token_str)
        if norm_token and norm_token in norm_map:
            return norm_map[norm_token]
        if allow_numeric_fallback:
            try:
                return int(token_str)
            except ValueError:
                return None
        return None

    if is_multiselect:
        candidates = _ensure_sequence(raw_value)
    else:
        candidates = [raw_value]

    resolved_ids: List[int] = []
    missing_tokens: List[str] = []

    for candidate in candidates:
        option_id = _match_token(candidate)
        if option_id is None:
            missing_tokens.append(str(candidate))
            continue
        if option_id not in resolved_ids:
            resolved_ids.append(option_id)

    if not resolved_ids:
        if missing_tokens:
            logger.warning(
                "Failed to resolve options", extra={"code": code, "values": missing_tokens}
            )
        return None

    if missing_tokens:
        logger.warning(
            "Partially resolved options", extra={"code": code, "values": missing_tokens}
        )

    if is_multiselect:
        return ",".join(str(option_id) for option_id in resolved_ids)
    return resolved_ids[0]


def _determine_input_type(code: str, meta: Dict[str, Any]) -> str:
    override = ATTR_TYPE_OVERRIDES.get(code)
    if override:
        return override
    for key in ("input", "frontend_input", "frontendInput"):
        if key in meta and meta[key]:
            return str(meta[key]).lower()
    return "text"


def normalize_value(
    attr_code: str,
    raw_value: Any,
    meta: Optional[Dict[str, Any]] = None,
    fetch_meta_func: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Any:
    """Normalise ``raw_value`` according to Magento attribute metadata.

    The function is pure: it returns a converted value without mutating the
    provided metadata.
    """

    source_meta = meta or {}
    meta_copy = deepcopy(source_meta)
    input_type = _determine_input_type(attr_code, meta_copy)
    backend_type = str(meta_copy.get("backend_type") or "").lower()

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

    if input_type in {"multiselect", "select"}:
        if fetch_meta_func is None:
            def _fallback_fetch(code: str) -> Dict[str, Any]:
                if code == attr_code:
                    return deepcopy(source_meta)
                return {}

            fetcher = _fallback_fetch
        else:
            fetcher = fetch_meta_func
        return resolve_option_ids(attr_code, raw_value, fetcher)

    if input_type in {"boolean", "bool"}:
        return _normalise_bool(raw_value)

    if backend_type == "decimal" or input_type in {"price"}:
        return _normalise_decimal(raw_value)

    # For all other textual fields attempt to canonicalise with the YAML map.
    return _canonicalise_free_text(attr_code, raw_value)

