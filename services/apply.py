"""Utilities for building and applying Magento product updates."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Dict, Optional, Tuple

import httpx

from connectors.magento import client
from services.normalize import normalize_value

try:  # pragma: no cover - Streamlit is optional in test environments.
    import streamlit as st
except Exception:  # pragma: no cover
    st = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

NON_EDITABLE_CODES = {
    "has_options",
    "required_options",
    "quantity_and_stock_status",
    "image",
    "small_image",
    "thumbnail",
    "media_gallery",
    "media_gallery_entries",
    "stock_item",
}

_ATTRIBUTE_CACHE: Dict[str, Dict[str, Any]] = {}


def _normalise_options(options: Any) -> list[dict[str, str]]:
    normalised: list[dict[str, str]] = []
    if not isinstance(options, list):
        return normalised
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("option_id")
        raw_value = option.get("value")
        if option_id in (None, ""):
            option_id = raw_value
        if option_id in (None, ""):
            continue
        label = (
            option.get("label")
            or option.get("label_default")
            or option.get("labelDefault")
            or option.get("default_label")
        )
        value_str = str(option_id)
        entry: dict[str, str] = {
            "value": value_str,
            "label": str(label) if label is not None else value_str,
        }
        if raw_value not in (None, ""):
            raw_str = str(raw_value)
            if raw_str != value_str:
                entry["raw_value"] = raw_str
        normalised.append(entry)
    return normalised


def resolve_attribute_metadata(code: str, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return merged metadata for ``code`` combining API data with ``fallback``."""

    if code not in _ATTRIBUTE_CACHE:
        try:
            remote = client.get_attribute(code) or {}
        except Exception as exc:  # pragma: no cover - network failure fallback
            logger.warning("Failed to load attribute metadata", extra={"code": code, "error": str(exc)})
            remote = {}

        options = remote.get("options")
        if not options:
            try:
                options = client.get_attribute_options(code)
            except Exception:  # pragma: no cover - optional endpoint
                options = []

        merged = {
            "attribute_code": remote.get("attribute_code") or code,
            "frontend_input": remote.get("frontend_input") or remote.get("input"),
            "backend_type": remote.get("backend_type"),
            "frontend_class": remote.get("frontend_class"),
            "options": _normalise_options(options),
        }
        merged["input"] = merged.get("frontend_input")
        _ATTRIBUTE_CACHE[code] = merged

    resolved = deepcopy(_ATTRIBUTE_CACHE[code])
    if fallback:
        for key, value in fallback.items():
            if key == "options" and value:
                resolved.setdefault("options", [])
                resolved["options"] = _normalise_options(list(value)) or resolved["options"]
                continue
            if value not in (None, ""):
                resolved[key] = value
        if "input" not in resolved and fallback.get("frontend_input"):
            resolved["input"] = fallback["frontend_input"]
    return resolved


def _extract_custom_attributes(product: Dict[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {}
    for attr in product.get("custom_attributes", []) or []:
        if not isinstance(attr, dict):
            continue
        code = attr.get("attribute_code")
        if not code:
            continue
        attributes[str(code)] = attr.get("value")
    return attributes


def _extract_category_ids(source: Optional[Dict[str, Any]]) -> list[int]:
    if not source:
        return []
    links = source.get("category_links") or []
    if not isinstance(links, list):
        return []
    result: list[int] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        cid = link.get("category_id")
        if cid in (None, ""):
            continue
        try:
            result.append(int(cid))
        except (TypeError, ValueError):
            continue
    return result


def _payload_from_diff(sku: Any, diff: Dict[str, Any]) -> Dict[str, Any]:
    if not diff:
        return {}

    product: Dict[str, Any] = {"sku": sku, "custom_attributes": []}
    extension_attributes: Dict[str, Any] = {}

    category_ids = diff.get("category_ids")
    if category_ids is not None:
        extension_attributes["category_links"] = [
            {"category_id": str(cid)} for cid in category_ids
        ]

    for code, value in diff.items():
        if code in NON_EDITABLE_CODES or code == "category_ids":
            continue
        if value is None:
            continue
        product["custom_attributes"].append(
            {"attribute_code": code, "value": value}
        )

    if extension_attributes:
        product["extension_attributes"] = extension_attributes

    if not product["custom_attributes"] and not extension_attributes:
        return {}

    return {"product": product}


def build_product_payload(
    sku: Any,
    form_data: Dict[str, Any],
    metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    original_attributes: Optional[Dict[str, Any]] = None,
    original_extension_attributes: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Return ``(diff, resolved_metadata, payload)`` for the provided form input."""

    metadata = metadata or {}
    original_attributes = original_attributes or {}
    original_extension_attributes = original_extension_attributes or {}

    diff: Dict[str, Any] = {}
    resolved_meta: Dict[str, Dict[str, Any]] = {}

    for code, raw_value in form_data.items():
        if code in NON_EDITABLE_CODES:
            continue

        attr_meta = resolve_attribute_metadata(code, metadata.get(code, {}))
        resolved_meta[code] = attr_meta

        fetcher = (
            lambda requested_code, meta=attr_meta: meta if requested_code == code else {}
        )

        try:
            normalised_new = normalize_value(
                code, raw_value, attr_meta, fetch_meta_func=fetcher
            )
        except ValueError as exc:
            raise ValueError(f"{code}: {exc}") from exc

        if code == "category_ids" and not original_attributes.get(code):
            current_categories = _extract_category_ids(original_extension_attributes)
            baseline = normalize_value(
                code,
                current_categories,
                attr_meta,
                fetch_meta_func=fetcher,
            )
        else:
            baseline = normalize_value(
                code,
                original_attributes.get(code),
                attr_meta,
                fetch_meta_func=fetcher,
            )

        if normalised_new is None or normalised_new == baseline:
            continue

        if code == "custom_layout_update_file" and normalised_new is None:
            # Skip the sentinel "no update" values entirely.
            continue

        diff[code] = normalised_new

    if not diff:
        return diff, resolved_meta, {}

    payload = _payload_from_diff(sku, diff)
    return diff, resolved_meta, payload


def apply_product_update(
    sku: Any,
    diff: Dict[str, Any],
    resolved_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Any, Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Send the Magento update request and verify the resulting state."""

    if not diff:
        raise ValueError("No changes to apply")

    payload = _payload_from_diff(sku, diff)
    if not payload:
        raise ValueError("No payload to send for the provided diff")

    if st is not None:
        try:  # pragma: no cover - Streamlit optional during tests
            st.code(json.dumps(payload, indent=2))
        except Exception:
            pass

    logger.info("Sending product update", extra={"sku": sku, "payload": payload})

    try:
        response = client.patch(f"products/{sku}", payload)
    except httpx.HTTPStatusError as exc:
        status_code = getattr(exc.response, "status_code", None)
        request_url = getattr(exc.request, "url", None)
        request_body = getattr(exc.request, "content", b"")
        if isinstance(request_body, bytes):
            try:
                request_body = request_body.decode("utf-8")
            except UnicodeDecodeError:
                request_body = request_body.decode("utf-8", errors="replace")
        response_text = getattr(exc.response, "text", None)

        logger.error(
            "Magento update failed",
            extra={
                "sku": sku,
                "status_code": status_code,
                "request_url": str(request_url) if request_url is not None else None,
                "request_body": request_body,
                "response_text": response_text,
            },
        )
        if st is not None:
            try:  # pragma: no cover - Streamlit optional during tests
                message_lines = [
                    "❌ Failed to save attributes:",
                    f"Status: {status_code or '?'}",
                    f"URL: {request_url}",
                    "Request body:",
                    request_body or "<empty request body>",
                    "Response text:",
                    response_text or "<empty response>",
                ]
                st.error("\n".join(message_lines))
            except Exception:
                pass
        raise

    logger.info("Magento update succeeded", extra={"sku": sku, "response": response})

    product_state = client.get_product(sku)
    resolved_metadata = resolved_metadata or {}
    mismatches = _verify_diff(diff, product_state, resolved_metadata)
    return response, mismatches, product_state


def _verify_diff(
    diff: Dict[str, Any],
    product_state: Dict[str, Any],
    metadata: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Compare ``diff`` against ``product_state`` and return mismatched fields."""

    current_attrs = _extract_custom_attributes(product_state)
    extension_attrs = product_state.get("extension_attributes") or {}
    mismatches: Dict[str, Dict[str, Any]] = {}

    for code, expected in diff.items():
        attr_meta = metadata.get(code, {})
        if code == "category_ids":
            raw_current = _extract_category_ids(extension_attrs)
        else:
            raw_current = current_attrs.get(code)
        fetcher = (
            lambda requested_code, meta=attr_meta: meta if requested_code == code else {}
        )
        current_normalised = normalize_value(
            code, raw_current, attr_meta, fetch_meta_func=fetcher
        )
        if current_normalised != expected:
            mismatches[code] = {
                "expected": expected,
                "actual": current_normalised,
                "raw": raw_current,
            }

    return mismatches

