"""Utilities for persisting enriched product attributes back to Magento."""

from __future__ import annotations

import ast
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

import httpx

from connectors.magento import client


logger = logging.getLogger(__name__)


def _looks_like_float(candidate: str) -> bool:
    try:
        float(candidate)
    except ValueError:
        return False
    return True


def _normalise_int_list(value: Any) -> List[int]:
    """Return a sorted, de-duplicated list of ints parsed from ``value``."""

    if value is None:
        return []

    items: Iterable[Any]
    original = value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return []
        parsed = None
        try:
            parsed = ast.literal_eval(trimmed)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            items = parsed
        else:
            found = re.findall(r"\d+", trimmed)
            if found:
                items = found
            else:
                parts = [p.strip() for p in trimmed.split(",") if p.strip()]
                items = parts or []
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]

    ints: List[int] = []
    for item in items:
        if isinstance(item, (list, tuple, set)):
            ints.extend(_normalise_int_list(item))
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if not stripped:
                continue
            if re.fullmatch(r"-?\d+", stripped):
                ints.append(int(stripped))
                continue
            digits = re.findall(r"-?\d+", stripped)
            if digits:
                ints.extend(int(d) for d in digits)
                continue
            raise ValueError(f"Cannot parse integer from '{item}'")
        try:
            ints.append(int(item))
        except (TypeError, ValueError):
            raise ValueError(f"Cannot parse integer from '{item}'") from None

    if not ints and original not in (None, "", [], (), set()):
        raise ValueError("No numeric values found")

    # De-duplicate while preserving order, then sort for deterministic output.
    seen = []
    for item in ints:
        if item not in seen:
            seen.append(item)
    return sorted(seen)


def _normalise_single_int(value: Any) -> Optional[int]:
    numbers = _normalise_int_list(value)
    if not numbers:
        return None
    if len(numbers) > 1:
        raise ValueError("Expected a single numeric value")
    return numbers[0]


def _normalise_bool_flag(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 1 if int(value) else 0
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        lowered = trimmed.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return 1
        if lowered in {"0", "false", "no", "off"}:
            return 0
    raise ValueError("Expected a boolean-like value")


def _generic_normalise(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        if "," in trimmed:
            parts = [p.strip() for p in trimmed.split(",") if p.strip()]
            converted_parts: List[str] = []
            for part in parts:
                lowered = part.lower()
                if re.fullmatch(r"-?\d+", part):
                    converted = int(part)
                elif lowered in {"true", "false"}:
                    converted = lowered == "true"
                elif _looks_like_float(part):
                    converted = float(part)
                else:
                    converted = part
                converted_parts.append(str(converted))
            return ",".join(converted_parts)
        if re.fullmatch(r"-?\d+", trimmed):
            return int(trimmed)
        lowered = trimmed.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if _looks_like_float(trimmed):
            return float(trimmed)
        return trimmed
    if isinstance(value, (int, float, bool)):
        return value
    return value


def normalise_attribute_value(
    code: str,
    value: Any,
    attr_meta: Optional[Dict[str, Any]] = None,
) -> Any:
    """Return a normalised value for the provided attribute metadata."""

    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None

    attr_meta = attr_meta or {}
    input_type = str(attr_meta.get("input") or "").lower()

    if code == "custom_layout_update_file":
        if value is None:
            return None
        if isinstance(value, str):
            trimmed = value.strip()
        else:
            trimmed = str(value).strip()
        if not trimmed:
            return None
        if trimmed in {"__no_update__", "no_update"}:
            return None
        return trimmed
    if code == "quantity_and_stock_status":
        return None
    if code == "options_container":
        if value is None:
            return None
        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return None
            lowered = trimmed.lower()
            if lowered.startswith("container"):
                return lowered
            if re.fullmatch(r"\d+", trimmed):
                return f"container{trimmed}"
            return trimmed
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float):
                if value.is_integer():
                    number_str = format(value, ".0f")
                else:
                    number_str = format(value, "g")
            else:
                number_str = str(value)
            return f"container{number_str}"
        return str(value)
    if code == "category_ids":
        return _normalise_int_list(value)
    if input_type == "multiselect":
        return _normalise_int_list(value)
    if input_type == "select":
        return _normalise_single_int(value)
    if input_type in {"boolean", "bool"}:
        return _normalise_bool_flag(value)

    return _generic_normalise(value)


def build_product_payload(
    sku: Any,
    attributes: Dict[str, Any],
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a Magento product payload respecting attribute types."""

    metadata = metadata or {}
    custom_attributes: List[Dict[str, Any]] = []
    category_links: List[Dict[str, str]] = []

    for code, raw_value in attributes.items():
        attr_meta = metadata.get(code, {})
        normalised = normalise_attribute_value(code, raw_value, attr_meta)
        if normalised is None:
            continue
        if code == "category_ids":
            if not isinstance(normalised, list):
                raise ValueError("Attribute 'category_ids' must be a list of integers")
            category_links = [
                {"category_id": str(category_id)} for category_id in normalised
            ]
            continue
        if code == "quantity_and_stock_status":
            continue
        if attr_meta.get("input", "").lower() == "multiselect":
            if not isinstance(normalised, list) or any(
                not isinstance(item, int) for item in normalised
            ):
                raise ValueError(
                    f"Attribute '{code}' must be a list of integers for multiselect"
                )
        elif attr_meta.get("input", "").lower() == "select":
            if code == "options_container":
                if not isinstance(normalised, str):
                    raise ValueError(
                        "Attribute 'options_container' must be a string for select"
                    )
            elif not isinstance(normalised, int):
                raise ValueError(f"Attribute '{code}' must be an integer for select")
        elif attr_meta.get("input", "").lower() in {"boolean", "bool"}:
            if normalised not in {0, 1}:
                raise ValueError(
                    f"Attribute '{code}' must be normalised to 0 or 1 for boolean fields"
                )
        custom_attributes.append({"attribute_code": code, "value": normalised})

    product: Dict[str, Any] = {
        "sku": sku,
        "custom_attributes": custom_attributes,
        "extension_attributes": {"category_links": category_links},
    }

    return {"product": product}


def apply_product_update(
    sku: Any,
    attributes: Dict[str, Any],
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    payload: Optional[Dict[str, Any]] = None,
):
    """Send a Magento product update request for the provided SKU without retries."""

    # Always regenerate the payload to ensure fresh normalisation, regardless of
    # whether a pre-built payload has been supplied.
    final_payload = build_product_payload(sku, attributes, metadata)

    logger.info("Sending product update", extra={"sku": sku, "payload": final_payload})

    patch_func = getattr(client.patch, "__wrapped__", None)
    try:
        if callable(patch_func):
            response = patch_func(client, f"products/{sku}", final_payload)
        else:
            response = client.patch(f"products/{sku}", final_payload)
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Magento update failed",
            extra={
                "sku": sku,
                "payload": final_payload,
                "status_code": getattr(exc.response, "status_code", None),
                "response_text": getattr(exc.response, "text", None),
            },
        )
        raise

    logger.info("Magento update succeeded", extra={"sku": sku, "response": response})
    return response
