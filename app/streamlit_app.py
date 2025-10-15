"""Streamlit front end for reviewing and enriching Magento catalog data."""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.magento import client
from services.apply import (
    apply_product_update,
    build_product_payload,
    resolve_attribute_metadata,
)
from services.llm_extract import extract_attributes
from services.normalize import normalize_value, resolve_option_ids

st.set_page_config(page_title="Magento Product Enricher", layout="wide")

st.title("🎸 Magento Product Enricher")

st.session_state.setdefault("products", [])
st.session_state.setdefault("product_details", {})
st.session_state.setdefault("selected_product_id", None)
st.session_state.setdefault("selected_skus", [])
st.session_state.setdefault("editor_open", False)
st.session_state.setdefault("last_update", None)
st.session_state.setdefault("generated", {})
st.session_state.setdefault("original_attrs", {})
st.session_state.setdefault("active_form_sku", None)


def to_simple(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        return value.item()
    return int(value)

if st.button("🔄 Load Default Products"):
    data = client.get_default_products()
    st.session_state.products = data.get("items", [])
    st.success(f"Loaded {len(st.session_state.products)} products")

if st.session_state.products:
    set_name_by_id = {}
    try:
        set_name_by_id = client.get_attribute_sets()
    except Exception as e:
        st.warning(f"Couldn't load Attribute Sets ({e}); editing by set name will be disabled.")

    name_to_id = {name: set_id for set_id, name in set_name_by_id.items()}

    selected_lookup = set(st.session_state.get("selected_skus", []))
    table_rows = []

    def _extract_qty(prod: Dict[str, Any]) -> Optional[float]:
        ext = prod.get("extension_attributes") or {}
        stock_item = None
        if isinstance(ext, dict):
            stock_item = ext.get("stock_item")
        if isinstance(stock_item, dict):
            qty_value = stock_item.get("qty")
        else:
            qty_value = None
        if qty_value in (None, ""):
            return None
        try:
            return float(qty_value)
        except (TypeError, ValueError):
            return None

    for product in st.session_state.products:
        raw_set_id = product.get("attribute_set_id")
        try:
            set_id = int(raw_set_id)
        except (TypeError, ValueError):
            set_id = None
        product["attribute_set_id"] = set_id
        if set_id is None:
            set_name = "Unknown"
        elif set_name_by_id:
            set_name = set_name_by_id.get(set_id, str(set_id))
        else:
            set_name = str(set_id)
        product["attribute_set_name"] = set_name

        table_rows.append(
            {
                "select": product.get("sku") in selected_lookup,
                "sku": product.get("sku"),
                "name": product.get("name"),
                "attribute_set": set_name,
                "qty": _extract_qty(product),
                "created_at": product.get("created_at"),
            }
        )

    df = pd.DataFrame(table_rows)

    column_config: Dict[str, Any] = {
        "select": st.column_config.CheckboxColumn(
            "Select", help="Mark products to work with in the attribute editor."
        )
    }
    disabled_columns = ["sku", "name", "qty", "created_at"]

    if set_name_by_id:
        column_config["attribute_set"] = st.column_config.SelectboxColumn(
            "Attribute Set", options=sorted(set_name_by_id.values())
        )
    else:
        disabled_columns.append("attribute_set")

    editable_df = st.data_editor(
        df,
        column_config=column_config,
        disabled=disabled_columns,
        hide_index=True,
        key="default_products_editor",
    )

    if isinstance(editable_df, pd.DataFrame):
        current_df = editable_df
    elif editable_df is not None:
        current_df = pd.DataFrame(editable_df)
    else:
        current_df = df

    st.session_state["selected_skus"] = [
        to_simple(sku)
        for sku in current_df.loc[current_df["select"], "sku"].dropna().tolist()
    ]

    edited_records = current_df.to_dict("records")

    name_by_sku = {
        row.get("sku"): row.get("attribute_set", "Unknown")
        for row in edited_records
        if row.get("sku")
    }

    for product in st.session_state.products:
        selected_name = name_by_sku.get(
            product["sku"], product.get("attribute_set_name", "Unknown")
        )
        product["attribute_set_name"] = selected_name
        if set_name_by_id:
            new_set_id = name_to_id.get(selected_name)
            if new_set_id is not None:
                product["attribute_set_id"] = new_set_id
        else:
            try:
                product["attribute_set_id"] = int(selected_name)
            except (TypeError, ValueError):
                pass

    if (
        st.session_state.get("selected_product_id")
        and st.session_state["selected_product_id"]
        not in st.session_state["selected_skus"]
    ):
        st.session_state["selected_product_id"] = None

    selected_skus = st.session_state["selected_skus"]
    hint = st.text_input("Optional hint (e.g. 'telecaster electric guitar')")  # пока не используем

    all_attr_codes_cache: Set[str] = set()
    MAX_FIELDS_TO_SHOW = 60

    def fetch_all_attr_codes() -> Set[str]:
        if all_attr_codes_cache:
            return all_attr_codes_cache
        try:
            data = client.get_all_attributes() or []
        except Exception as exc:
            st.warning(f"Failed to load global attributes: {exc}")
            return set()
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("attributes") or []
        else:
            items = []
        for attr in items:
            code = (
                attr.get("attribute_code")
                or attr.get("code")
                or attr.get("attributeCode")
                or ""
            ).strip()
            if code:
                all_attr_codes_cache.add(code)
        return set(all_attr_codes_cache)

    def all_attribute_codes() -> Set[str]:
        return fetch_all_attr_codes()

    if st.button("✨ Get Specs"):
        if not selected_skus:
            st.info("Select at least one SKU.")
        else:
            results = []
            global_codes = all_attribute_codes()
            for sku in selected_skus:
                product = next(
                    (p for p in st.session_state.products if p.get("sku") == sku),
                    None,
                )
                if not product:
                    st.warning(f"SKU {sku} is not loaded anymore; skipping.")
                    continue
                try:
                    specs = extract_attributes(product.get("name", ""), hint)
                except Exception as exc:
                    st.warning(f"Failed to extract specs for {sku}: {exc}")
                    continue
                normalized = {
                    code: normalize_value(code, str(value), {"input": "text"})
                    for code, value in (specs or {}).items()
                }
                base_filter = global_codes or set()
                shown = {
                    code: val
                    for code, val in normalized.items()
                    if not base_filter or code in base_filter
                }
                if len(shown) > MAX_FIELDS_TO_SHOW:
                    sorted_items = sorted(shown.items())
                    trimmed_items = sorted_items[:MAX_FIELDS_TO_SHOW]
                    hidden_count = len(shown) - MAX_FIELDS_TO_SHOW
                    shown = dict(trimmed_items)
                    shown["_note"] = f"… {hidden_count} more attributes hidden"
                results.append({"sku": sku, **shown})
            if results:
                st.dataframe(pd.DataFrame(results))
            else:
                st.info("No specs generated yet. Try adjusting the hint or selection.")

    # --- Unified Attribute Editor ---
    st.subheader("🛠️ Edit Attributes")

    editable_skus = selected_skus

    if not editable_skus:
        st.info("Select at least one product to enable the attribute editor.")
        st.session_state["editor_open"] = False
        st.session_state["selected_product_id"] = None
    else:
        if st.session_state.selected_product_id not in editable_skus:
            st.session_state["selected_product_id"] = to_simple(editable_skus[0])

        try:
            current_index = editable_skus.index(st.session_state.selected_product_id)
        except ValueError:
            current_index = 0

        selected_product_id = st.selectbox(
            "Product to edit",
            options=editable_skus,
            index=current_index,
            key="selected_product_selector",
            help="Choose which SKU to edit attributes for.",
        )

        selected_product_id = to_simple(selected_product_id)
        if selected_product_id != st.session_state.selected_product_id:
            st.session_state["selected_product_id"] = selected_product_id

        open_editor = st.button(
            "Open attribute editor", disabled=not bool(st.session_state["selected_skus"])
        )
        if open_editor and selected_product_id is not None:
            st.session_state["editor_open"] = True
            st.session_state["selected_product_id"] = selected_product_id

        if st.session_state.editor_open and st.session_state.selected_product_id:
            sku = st.session_state.selected_product_id
            product = next(
                (p for p in st.session_state.products if p.get("sku") == sku),
                None,
            )
            if not product:
                st.warning("Product not found in session. Reload products to continue.")
            else:
                close_editor = st.button("Close editor")
                if close_editor:
                    st.session_state.editor_open = False

                prod_full = st.session_state.product_details.get(sku)
                if not prod_full:
                    try:
                        prod_full = client.get_product(sku)
                        st.session_state.product_details[sku] = prod_full
                    except Exception as exc:
                        st.error(f"Failed to load product {sku}: {exc}")
                        prod_full = None

                if prod_full:
                    raw_set_id = prod_full.get("attribute_set_id") or product.get(
                        "attribute_set_id"
                    )
                    try:
                        set_id = int(raw_set_id)
                    except (TypeError, ValueError):
                        set_id = None

                    # Fallback order: attribute-set scoped attrs → global attrs → current custom values
                    set_attrs = []
                    try:
                        if set_id:
                            set_attrs = client.get_attributes_for_set(set_id) or []
                    except Exception:
                        set_attrs = []
                    if not set_attrs:
                        all_attrs = []
                        try:
                            all_attrs = client.get_all_attributes() or []
                        except Exception:
                            pass
                        set_attrs = all_attrs

                    # Normalize attribute metadata
                    meta: Dict[str, Dict[str, Any]] = {}
                    for a in (set_attrs if isinstance(set_attrs, list) else []):
                        code = (
                            a.get("attribute_code")
                            or a.get("code")
                            or a.get("attributeCode")
                            or (a.get("attribute") or {}).get("attribute_code")
                            or ""
                        ).strip()
                        if not code:
                            continue
                        meta[code] = {
                            "label": (a.get("frontend_label") or code),
                            "input": a.get("frontend_input")
                            or a.get("frontendInput")
                            or "text",
                            "options": a.get("options") or [],
                            "group": "",
                        }

                    custom_attrs = {
                        attr.get("attribute_code"): attr.get("value")
                        for attr in (prod_full.get("custom_attributes") or [])
                        if attr.get("attribute_code")
                    }
                    ext_attrs = prod_full.get("extension_attributes") or {}
                    category_links = ext_attrs.get("category_links") or []
                    current_categories = []
                    for link in category_links if isinstance(category_links, list) else []:
                        if not isinstance(link, dict):
                            continue
                        cid = link.get("category_id")
                        if cid in (None, ""):
                            continue
                        try:
                            current_categories.append(int(cid))
                        except (TypeError, ValueError):
                            continue

                    display_values = dict(custom_attrs)
                    if current_categories:
                        display_values["category_ids"] = ",".join(
                            str(cid) for cid in current_categories
                        )

                    st.session_state["original_attrs"][sku] = {
                        **custom_attrs,
                        "category_ids": list(current_categories),
                    }

                    resolved_meta = {
                        code: resolve_attribute_metadata(code, meta.get(code, {}))
                        for code in meta
                    }

                    view_placeholder = st.empty()

                    def render_view_table(current_values: Dict[str, Any]):
                        view_rows = []
                        for code, m in sorted(resolved_meta.items()):
                            opts = m.get("options") or []
                            input_type = str(m.get("input") or "").lower()
                            raw_value = current_values.get(code, "")
                            if code == "category_ids":
                                raw_value = display_values.get("category_ids", "")
                            if opts and isinstance(opts, list):
                                value_map = {
                                    str(opt.get("value")): str(opt.get("label"))
                                    for opt in opts
                                    if isinstance(opt, dict)
                                }
                                if input_type == "multiselect" and isinstance(raw_value, str):
                                    labels = [
                                        value_map.get(part.strip(), part.strip())
                                        for part in raw_value.split(",")
                                        if part.strip()
                                    ]
                                    display_val = ", ".join(labels)
                                else:
                                    display_val = value_map.get(str(raw_value), str(raw_value))
                            else:
                                display_val = str(raw_value)
                            view_rows.append(
                                {
                                    "attribute_code": code,
                                    "label": meta.get(code, {}).get("label", code),
                                    "value": display_val,
                                }
                            )

                        if view_rows:
                            view_placeholder.dataframe(
                                pd.DataFrame(view_rows),
                                hide_index=True,
                                use_container_width=True,
                            )

                    render_view_table(display_values)

                    st.markdown("#### Edit values")

                    def _widget_default(code: str, attr_meta: Dict[str, Any]) -> Any:
                        raw = display_values.get(code, "")
                        input_type = str(attr_meta.get("input") or "").lower()
                        if code == "category_ids":
                            return ",".join(str(cid) for cid in current_categories)
                        if input_type == "multiselect":
                            try:
                                resolved = resolve_option_ids(
                                    code,
                                    raw,
                                    lambda requested_code, meta=attr_meta: meta
                                    if requested_code == code
                                    else {},
                                )
                            except ValueError:
                                resolved = None
                            if isinstance(resolved, str):
                                return [
                                    part
                                    for part in (v.strip() for v in resolved.split(","))
                                    if part
                                ]
                            if isinstance(resolved, int):
                                return [str(resolved)]
                            return []
                        if input_type == "select":
                            try:
                                resolved = resolve_option_ids(
                                    code,
                                    raw,
                                    lambda requested_code, meta=attr_meta: meta
                                    if requested_code == code
                                    else {},
                                )
                            except ValueError:
                                resolved = None
                            if isinstance(resolved, int):
                                return str(resolved)
                            return None
                        if input_type in {"boolean", "bool"}:
                            lowered = str(raw).strip().lower()
                            if lowered in {"1", "true", "yes", "on"}:
                                return 1
                            if lowered in {"0", "false", "no", "off"}:
                                return 0
                            return None
                        if raw is None:
                            return None
                        if isinstance(raw, (str, int, float, bool)):
                            return raw
                        if isinstance(raw, (list, tuple, set)):
                            joined_parts: List[str] = []
                            for item in raw:
                                if item in (None, ""):
                                    continue
                                try:
                                    joined_parts.append(str(to_simple(item)))
                                except Exception:
                                    joined_parts.append(str(item))
                            return ",".join(joined_parts)
                        try:
                            return to_simple(raw)
                        except Exception:
                            return str(raw)

                    edit_codes = sorted(resolved_meta.keys())

                    def _initialise_form_state(*, force_reset: bool = False) -> None:
                        if force_reset:
                            keys_to_clear = [
                                key
                                for key in list(st.session_state.keys())
                                if key.startswith("attr_")
                            ]
                            for key in keys_to_clear:
                                st.session_state.pop(key, None)
                        st.session_state["active_form_sku"] = sku
                        for code in edit_codes:
                            state_key = f"attr_{code}"
                            default_value = _widget_default(
                                code, resolved_meta.get(code, {})
                            )
                            st.session_state.setdefault(state_key, default_value)

                    _initialise_form_state(
                        force_reset=st.session_state.get("active_form_sku") != sku
                    )

                    with st.form(key="attr_editor", clear_on_submit=False):
                        for code in edit_codes:
                            attr_meta = resolved_meta.get(
                                code, {"label": code, "input": "text", "options": []}
                            )
                            label = meta.get(code, {}).get("label", code)
                            input_type = str(attr_meta.get("input") or "").lower()
                            options = attr_meta.get("options") or []
                            state_key = f"attr_{code}"

                            if code == "category_ids":
                                st.text_input(
                                    label,
                                    value=st.session_state.get(state_key, ""),
                                    key=state_key,
                                )
                                continue

                            if input_type == "multiselect" and options:
                                option_values = [
                                    opt.get("value")
                                    for opt in options
                                    if isinstance(opt, dict) and opt.get("value") not in (None, "")
                                ]
                                label_map = {
                                    str(opt.get("value")): str(opt.get("label"))
                                    for opt in options
                                    if isinstance(opt, dict)
                                }
                                selected_values = st.session_state.get(state_key)
                                if isinstance(selected_values, str):
                                    selected_default = [
                                        part
                                        for part in (v.strip() for v in selected_values.split(","))
                                        if part
                                    ]
                                elif isinstance(selected_values, int):
                                    selected_default = [str(selected_values)]
                                else:
                                    selected_default = selected_values or []
                                st.multiselect(
                                    label,
                                    option_values,
                                    default=selected_default,
                                    key=state_key,
                                    format_func=lambda v, lm=label_map: lm.get(str(v), str(v)),
                                )
                                continue

                            if input_type == "select" and options:
                                option_values = [
                                    opt.get("value")
                                    for opt in options
                                    if isinstance(opt, dict) and opt.get("value") not in (None, "")
                                ]
                                label_map = {
                                    str(opt.get("value")): str(opt.get("label"))
                                    for opt in options
                                    if isinstance(opt, dict)
                                }
                                default_value = st.session_state.get(state_key)
                                if isinstance(default_value, int):
                                    default_value = str(default_value)
                                if default_value not in option_values and option_values:
                                    default_value = option_values[0]
                                st.selectbox(
                                    label,
                                    option_values,
                                    index=option_values.index(default_value)
                                    if default_value in option_values
                                    else 0,
                                    key=state_key,
                                    format_func=lambda v, lm=label_map: lm.get(str(v), str(v)),
                                )
                                continue

                            if input_type in {"boolean", "bool"}:
                                bool_options = [1, 0]
                                labels = {1: "Yes", 0: "No"}
                                default_val = st.session_state.get(state_key, 0)
                                if default_val not in bool_options:
                                    default_val = 0
                                    st.session_state[state_key] = default_val
                                st.selectbox(
                                    label,
                                    bool_options,
                                    index=bool_options.index(default_val),
                                    key=state_key,
                                    format_func=lambda v, lm=labels: lm.get(v, str(v)),
                                )
                                continue

                            st.text_input(
                                label,
                                value=st.session_state.get(state_key, ""),
                                key=state_key,
                            )

                        submitted = st.form_submit_button("Save attributes")

                    if submitted:
                        st.session_state["editor_open"] = True
                        st.session_state["selected_product_id"] = to_simple(sku)

                        form_data = {
                            code: st.session_state.get(f"attr_{code}")
                            for code in edit_codes
                        }

                        original_snapshot = st.session_state["original_attrs"].get(sku, {})

                        try:
                            diff, resolved, payload = build_product_payload(
                                sku,
                                form_data,
                                meta,
                                original_attributes=original_snapshot,
                                original_extension_attributes=ext_attrs,
                            )
                        except ValueError as exc:
                            st.error(f"Failed to build product payload: {exc}")
                        else:
                            if not diff:
                                st.toast("No changes")
                            else:
                                st.code(json.dumps(diff, indent=2))

                                response: Dict[str, Any] = {}
                                mismatches: Optional[Dict[str, Any]] = None
                                update_failed = False

                                with st.spinner("Saving attributes to Magento…"):
                                    try:
                                        response, mismatches, _ = apply_product_update(
                                            sku, diff, resolved
                                        )
                                    except httpx.HTTPStatusError as exc:
                                        status_code = getattr(
                                            exc.response, "status_code", None
                                        )
                                        response_text = getattr(
                                            exc.response, "text", ""
                                        )
                                        request_url = getattr(
                                            exc.request, "url", "Unknown URL"
                                        )
                                        st.session_state.last_update = {
                                            "sku": sku,
                                            "status": "error",
                                            "request": payload,
                                            "response": {
                                                "status_code": status_code,
                                                "body": response_text,
                                                "url": str(request_url),
                                            },
                                            "diff": diff,
                                        }
                                        update_failed = True
                                    except Exception as exc:
                                        st.error(f"Unexpected error: {exc}")
                                        st.session_state.last_update = {
                                            "sku": sku,
                                            "status": "error",
                                            "request": payload,
                                            "response": {"error": str(exc)},
                                            "diff": diff,
                                        }
                                        update_failed = True

                                should_refresh = False

                                if update_failed:
                                    should_refresh = False
                                elif mismatches:
                                    st.error("Not applied")
                                    st.json(mismatches)
                                    st.session_state.last_update = {
                                        "sku": sku,
                                        "status": "error",
                                        "request": payload,
                                        "response": {"mismatches": mismatches},
                                        "diff": diff,
                                    }
                                else:
                                    st.toast("Saved")

                                    st.session_state.last_update = {
                                        "sku": sku,
                                        "status": "success",
                                        "request": payload,
                                        "response": response,
                                        "diff": diff,
                                    }
                                    should_refresh = True

                                if should_refresh:
                                    try:
                                        fresh_state = client.get_product(sku)
                                    except Exception as exc:
                                        st.warning(
                                            f"Failed to refresh product {sku}: {exc}"
                                        )
                                        fresh_state = {}

                                    if isinstance(fresh_state, dict) and fresh_state:
                                        st.session_state.product_details[sku] = fresh_state

                                        fresh_custom = {
                                            attr.get("attribute_code"): attr.get("value")
                                            for attr in (
                                                fresh_state.get("custom_attributes") or []
                                            )
                                            if attr.get("attribute_code")
                                        }
                                        fresh_ext = (
                                            fresh_state.get("extension_attributes") or {}
                                        )
                                        new_category_ids: List[int] = []
                                        for link in fresh_ext.get("category_links") or []:
                                            if not isinstance(link, dict):
                                                continue
                                            cid = link.get("category_id")
                                            if cid in (None, ""):
                                                continue
                                            try:
                                                new_category_ids.append(int(cid))
                                            except (TypeError, ValueError):
                                                continue

                                        st.session_state["original_attrs"][sku] = {
                                            **fresh_custom,
                                            "category_ids": list(new_category_ids),
                                        }

                                        custom_attrs = fresh_custom
                                        ext_attrs = fresh_ext
                                        current_categories = list(new_category_ids)

                                        display_values = dict(custom_attrs)
                                        if new_category_ids:
                                            display_values["category_ids"] = ",".join(
                                                str(cid) for cid in new_category_ids
                                            )

                                        for prod in st.session_state.products:
                                            if prod.get("sku") == sku:
                                                prod["custom_attributes"] = fresh_state.get(
                                                    "custom_attributes", []
                                                )
                                                prod["extension_attributes"] = fresh_state.get(
                                                    "extension_attributes", {}
                                                )
                                                break

                                        render_view_table(display_values)

                                        _initialise_form_state(force_reset=True)

        if st.session_state.last_update:
            with st.expander("Last update payload", expanded=False):
                st.json(st.session_state.last_update)
