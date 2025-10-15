"""Streamlit front end for reviewing and enriching Magento catalog data."""

import json
import sys
from pathlib import Path
from typing import Dict, Optional, Set

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
    normalise_attribute_value,
)
from services.llm_extract import extract_attributes
from services.normalize import normalize_value

st.set_page_config(page_title="Magento Product Enricher", layout="wide")

st.title("🎸 Magento Product Enricher")

st.session_state.setdefault("products", [])
st.session_state.setdefault("product_details", {})
st.session_state.setdefault("selected_product_id", None)
st.session_state.setdefault("editor_open", False)
st.session_state.setdefault("last_update", None)


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

    for product in st.session_state.products:
        raw_set_id = product.get("attribute_set_id")
        try:
            set_id = int(raw_set_id)
        except (TypeError, ValueError):
            set_id = None
        product["attribute_set_id"] = set_id
        if set_id is None:
            product["attribute_set_name"] = "Unknown"
        elif set_name_by_id:
            product["attribute_set_name"] = set_name_by_id.get(set_id, str(set_id))
        else:
            product["attribute_set_name"] = str(set_id)

    df = pd.DataFrame([
        {
            "sku": p["sku"],
            "name": p["name"],
            "attribute_set_id": p.get("attribute_set_id"),
            "attribute_set_name": p.get("attribute_set_name", "Unknown"),
            "created_at": p["created_at"],
        }
        for p in st.session_state.products
    ])

    if set_name_by_id:
        editable_df = st.data_editor(
            df,
            column_config={
                "attribute_set_name": st.column_config.SelectboxColumn(
                    options=sorted(set_name_by_id.values())
                )
            },
            hide_index=True,
            key="editable_products",
        )

        if isinstance(editable_df, pd.DataFrame):
            current_df = editable_df
        elif editable_df is not None:
            current_df = pd.DataFrame(editable_df)
        else:
            current_df = df
    else:
        st.dataframe(df)
        current_df = df

    edited_records = current_df.to_dict("records")

    name_by_sku = {
        row.get("sku"): row.get("attribute_set_name", "Unknown")
        for row in edited_records
        if row.get("sku")
    }

    for product in st.session_state.products:
        selected_name = name_by_sku.get(product["sku"], product.get("attribute_set_name", "Unknown"))
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

    selected_skus = st.multiselect("Select SKUs", current_df["sku"])
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
                    code: normalize_value(code, str(value))
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

    product_options = list(current_df["sku"].dropna())
    editable_skus = selected_skus or product_options

    if not editable_skus:
        st.info("Load products to enable the attribute editor.")
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
            "Open attribute editor", disabled=not bool(selected_product_id)
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
                    meta = {}
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
                            "group": "",  # group data may be unavailable via API; hide the column
                        }

                    # Current values on the product
                    curr = {
                        attr.get("attribute_code"): attr.get("value")
                        for attr in (prod_full.get("custom_attributes") or [])
                        if attr.get("attribute_code")
                    }

                    view_placeholder = st.empty()
                    update_placeholder = st.empty()

                    def render_view_table(current_values: Dict[str, str]):
                        view_rows = []
                        for code, m in sorted(meta.items()):
                            opts = [o for o in (m["options"] or []) if isinstance(o, dict)]
                            val_raw = current_values.get(code, "")
                            # Преобразуем raw value -> label для отображения
                            if opts:
                                val_map = {
                                    str(o.get("value")): str(o.get("label")) for o in opts
                                }
                                if (
                                    isinstance(val_raw, str)
                                    and "," in val_raw
                                    and (m["input"] or "").lower() == "multiselect"
                                ):
                                    labels = [
                                        val_map.get(x.strip(), x.strip())
                                        for x in val_raw.split(",")
                                    ]
                                    val_disp = ", ".join([x for x in labels if x])
                                else:
                                    val_disp = val_map.get(str(val_raw), str(val_raw))
                            else:
                                val_disp = str(val_raw)
                            view_rows.append(
                                {
                                    "attribute_code": code,
                                    "label": m["label"],
                                    "value": val_disp,
                                }
                            )

                        if view_rows:
                            view_placeholder.dataframe(
                                pd.DataFrame(view_rows),
                                hide_index=True,
                                use_container_width=True,
                            )
                        elif current_values:
                            st.warning(
                                "No attribute metadata available; showing current custom attributes only."
                            )
                            fallback_rows = [
                                {"attribute_code": k, "label": k, "value": str(v)}
                                for k, v in sorted(current_values.items())
                            ]
                            view_placeholder.dataframe(
                                pd.DataFrame(fallback_rows),
                                hide_index=True,
                                use_container_width=True,
                            )

                    render_view_table(curr)

                    def render_update_report():
                        update_placeholder.empty()
                        last_update = st.session_state.get("last_update") or {}
                        if last_update.get("sku") != sku:
                            return
                        status = last_update.get("status") or "info"
                        with update_placeholder.container():
                            if status == "success":
                                st.success(f"Attributes saved for {sku}")
                            elif status == "error":
                                st.error(f"Failed to update {sku}")
                            else:
                                st.info(f"Update status for {sku}")
                            st.json(
                                {
                                    "request": last_update.get("request"),
                                    "diff": last_update.get("diff"),
                                    "response": last_update.get("response"),
                                }
                            )

                    render_update_report()

                    st.markdown("#### Edit values")
                    with st.form(key=f"attr_form_{sku}", clear_on_submit=False):
                        new_values = {}
                        edit_targets = (
                            sorted(meta.keys()) if meta else sorted(curr.keys())
                        )
                        for code in edit_targets:
                            m = meta.get(
                                code,
                                {"label": code, "input": "text", "options": []},
                            )
                            label = m["label"]
                            input_type = (m.get("input") or "").lower()
                            opts = [
                                o for o in (m.get("options") or []) if isinstance(o, dict)
                            ]
                            current_raw = curr.get(code, "")

                            if opts:
                                labels = [str(o.get("label")) for o in opts]
                                lbl_to_val = {
                                    str(o.get("label")): str(o.get("value")) for o in opts
                                }
                                # Текущее значение -> label
                                if input_type == "multiselect":
                                    cur_labels = []
                                    if isinstance(current_raw, str) and current_raw:
                                        for v in [
                                            x.strip()
                                            for x in current_raw.split(",")
                                            if x.strip()
                                        ]:
                                            found = next(
                                                (
                                                    str(o.get("label"))
                                                    for o in opts
                                                    if str(o.get("value")) == v
                                                ),
                                                v,
                                            )
                                            cur_labels.append(found)
                                    sel = st.multiselect(
                                        label,
                                        labels,
                                        default=cur_labels,
                                        key=f"{sku}_{code}",
                                    )
                                    mapped = [lbl_to_val.get(s, s) for s in sel]
                                    new_values[code] = ",".join(mapped)
                                else:
                                    cur_label = next(
                                        (
                                            str(o.get("label"))
                                            for o in opts
                                            if str(o.get("value"))
                                            == str(current_raw)
                                        ),
                                        str(current_raw),
                                    )
                                    sel = st.selectbox(
                                        label,
                                        labels,
                                        index=(
                                            labels.index(cur_label)
                                            if cur_label in labels
                                            else 0
                                        )
                                        if labels
                                        else None,
                                        key=f"{sku}_{code}",
                                    )
                                    new_values[code] = lbl_to_val.get(sel, sel)
                            else:
                                new_values[code] = st.text_input(
                                    label,
                                    value=str(current_raw or ""),
                                    key=f"{sku}_{code}",
                                )

                        submitted = st.form_submit_button(
                            f"💾 Save attributes for {sku}"
                        )
                        if submitted:
                            st.session_state["editor_open"] = True
                            st.session_state["selected_product_id"] = to_simple(sku)

                            errors = []
                            cleaned_values = {}
                            for code, val in new_values.items():
                                attr_meta = meta.get(
                                    code,
                                    {"label": code, "input": "text", "options": []},
                                )
                                try:
                                    normalised = normalise_attribute_value(
                                        code, val, attr_meta
                                    )
                                except ValueError as exc:
                                    errors.append(f"{code}: {exc}")
                                    continue
                                if normalised is None or normalised == "":
                                    continue
                                cleaned_values[code] = normalised

                            if errors:
                                for err in errors:
                                    st.error(f"⚠️ Unable to normalise {err}")
                            elif not cleaned_values:
                                st.info("Nothing to update.")
                            else:
                                try:
                                    payload = build_product_payload(
                                        sku, cleaned_values, meta
                                    )
                                except ValueError as exc:
                                    st.error(
                                        f"Failed to build product payload: {exc}"
                                    )
                                else:
                                    product_payload = payload.get("product", {})
                                    st.code(json.dumps(product_payload, indent=2))

                                    with st.spinner("Saving attributes to Magento…"):
                                        try:
                                            response = apply_product_update(
                                                sku,
                                                cleaned_values,
                                                meta,
                                                payload=payload,
                                            )
                                            st.toast("Saved")
                                        except httpx.HTTPStatusError as exc:
                                            status_code = getattr(exc.response, "status_code", None)
                                            request_url = str(
                                                getattr(exc.request, "url", "Unknown URL")
                                            )
                                            request_body: Optional[str]
                                            body_candidate = getattr(
                                                exc.request, "content", b""
                                            )
                                            if isinstance(body_candidate, bytes):
                                                request_body = body_candidate.decode(
                                                    "utf-8", errors="replace"
                                                )
                                            else:
                                                request_body = str(body_candidate)
                                            response_text = getattr(
                                                exc.response, "text", ""
                                            )
                                            st.error(
                                                "\n".join(
                                                    [
                                                        "❌ Failed to save attributes:",
                                                        f"Status: {status_code or '?'}",
                                                        f"URL: {request_url}",
                                                        "Request body:",
                                                        request_body
                                                        or "<empty request body>",
                                                        "Response text:",
                                                        response_text
                                                        or "<empty response>",
                                                    ]
                                                )
                                            )

                                            st.session_state.last_update = {
                                                "sku": sku,
                                                "status": "error",
                                                "request": payload,
                                                "response": {
                                                    "status_code": status_code,
                                                    "url": request_url,
                                                    "request_body": request_body,
                                                    "body": response_text,
                                                },
                                            }
                                            render_update_report()
                                        except Exception as exc:
                                            st.error(f"Unexpected error: {exc}")
                                            st.session_state.last_update = {
                                                "sku": sku,
                                                "status": "error",
                                                "request": payload,
                                                "response": {"error": str(exc)},
                                            }
                                            render_update_report()
                                        else:
                                            diff = {
                                                code: {
                                                    "previous": curr.get(code),
                                                    "new": value,
                                                }
                                                for code, value in cleaned_values.items()
                                                if curr.get(code) != value
                                            }
                                            st.session_state.last_update = {
                                                "sku": sku,
                                                "status": "success",
                                                "request": payload,
                                                "response": response,
                                                "diff": diff,
                                            }

                                            custom_attr_updates = {
                                                code: value
                                                for code, value in cleaned_values.items()
                                                if code
                                                not in {
                                                    "category_ids",
                                                    "quantity_and_stock_status",
                                                }
                                            }
                                            curr.update(custom_attr_updates)
                                            for forbidden in (
                                                "category_ids",
                                                "quantity_and_stock_status",
                                            ):
                                                curr.pop(forbidden, None)
                                            prod_full["custom_attributes"] = [
                                                {
                                                    "attribute_code": code,
                                                    "value": value,
                                                }
                                                for code, value in curr.items()
                                            ]
                                            st.session_state.product_details[sku] = prod_full

                                            payload_extension = (
                                                product_payload.get(
                                                    "extension_attributes", {}
                                                )
                                            )
                                            if payload_extension:
                                                prod_full.setdefault(
                                                    "extension_attributes", {}
                                                ).update(payload_extension)

                                            for prod in st.session_state.products:
                                                if prod.get("sku") == sku:
                                                    prod["custom_attributes"] = prod_full.get(
                                                        "custom_attributes", []
                                                    )
                                                    if payload_extension:
                                                        prod.setdefault(
                                                            "extension_attributes", {}
                                                        ).update(payload_extension)
                                                    break

                                            render_view_table(curr)
                                            render_update_report()

        if st.session_state.last_update:
            with st.expander("Last update payload", expanded=False):
                st.json(st.session_state.last_update)
