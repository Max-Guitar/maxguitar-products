"""Streamlit front end for reviewing and enriching Magento catalog data."""

import sys
from pathlib import Path
from typing import Set

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.magento import client
from services.apply import apply_product_update
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, JsCode
from services.llm_extract import extract_attributes
from services.normalize import normalize_value

st.set_page_config(page_title="Magento Product Enricher", layout="wide")

st.title("🎸 Magento Product Enricher")

if "products" not in st.session_state:
    st.session_state.products = []

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
    if st.button("Open attribute editor for selected"):
        if not selected_skus:
            st.info("Select at least one SKU.")
        else:
            tabs = st.tabs([f"{sku}" for sku in selected_skus])
            for tab, sku in zip(tabs, selected_skus):
                with tab:
                    product = next((p for p in st.session_state.products if p.get("sku") == sku), None)
                    if not product:
                        st.info("Product not found in session.")
                        continue
                    try:
                        prod_full = client.get_product(sku)
                    except Exception as e:
                        st.error(f"Failed to load product {sku}: {e}")
                        continue
                    raw_set_id = prod_full.get("attribute_set_id") or product.get("attribute_set_id")
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
                            "input": a.get("frontend_input") or a.get("frontendInput") or "text",
                            "options": a.get("options") or [],
                            "group": "",  # group data may be unavailable via API; hide the column
                        }

                    # Current values on the product
                    curr = {
                        attr.get("attribute_code"): attr.get("value")
                        for attr in (prod_full.get("custom_attributes") or [])
                        if attr.get("attribute_code")
                    }

                    # --- ЕДИНАЯ ТАБЛИЦА-РЕДАКТОР С ПОДДЕРЖКОЙ ВЫПАДАЮЩИХ СПИСКОВ ---
                    rows = []
                    for code, m in sorted(meta.items()):
                        opts = [o for o in (m.get("options") or []) if isinstance(o, dict)]
                        current_raw = curr.get(code, "")
                        input_type = (m.get("input") or "").lower()

                        if opts and input_type != "multiselect":
                            val_map = {str(o.get("value")): str(o.get("label")) for o in opts}
                            display_value = val_map.get(str(current_raw), str(current_raw))
                        else:
                            display_value = str(current_raw or "")

                        rows.append(
                            {
                                "attribute_code": code,
                                "label": m.get("label", code),
                                "input": input_type,
                                "options": [str(o.get("label")) for o in opts],
                                "opt_map_json": {str(o.get("label")): str(o.get("value")) for o in opts},
                                "value_display": display_value,
                            }
                        )

                    if not rows and curr:
                        # Фолбэк, если нет метаданных
                        rows = [
                            {
                                "attribute_code": k,
                                "label": k,
                                "input": "text",
                                "options": [],
                                "opt_map_json": {},
                                "value_display": str(v),
                            }
                            for k, v in sorted(curr.items())
                        ]

                    df = pd.DataFrame(rows)

                    gob = GridOptionsBuilder.from_dataframe(df, editable=True)
                    gob.configure_column("attribute_code", header_name="Code", editable=False)
                    gob.configure_column("label", header_name="Attribute", editable=False)
                    gob.configure_column("input", hide=True)
                    gob.configure_column("options", hide=True)
                    gob.configure_column("opt_map_json", hide=True)

                    cell_editor_selector = JsCode(
                        """
                        function(params) {
                          if (params.data && params.data.options && params.data.options.length > 0 && params.data.input !== 'multiselect') {
                            return { component: 'agSelectCellEditor', params: { values: params.data.options } };
                          }
                          return null;
                        }
                        """
                    )

                    gob.configure_column(
                        "value_display",
                        header_name="Value",
                        editable=True,
                        cellEditorSelector=cell_editor_selector,
                    )
                    gob.configure_grid_options(suppressRowClickSelection=True)

                    grid_response = AgGrid(
                        df,
                        gridOptions=gob.build(),
                        update_mode=GridUpdateMode.VALUE_CHANGED,
                        allow_unsafe_jscode=True,
                        fit_columns_on_grid_load=True,
                        height=520,
                    )

                    updated_df = pd.DataFrame(grid_response.get("data", []))
                    updates = {}

                    for _, row in updated_df.iterrows():
                        code = row.get("attribute_code")
                        if not code:
                            continue
                        display_value = str(row.get("value_display") or "")
                        input_type = str(row.get("input") or "")
                        opt_map = row.get("opt_map_json") or {}

                        if isinstance(opt_map, str):
                            try:
                                import json as _json

                                opt_map = _json.loads(opt_map)
                            except Exception:
                                opt_map = {}

                        if input_type == "multiselect":
                            labels = [x.strip() for x in display_value.split(",") if x.strip()]
                            mapped = [opt_map.get(lbl, lbl) for lbl in labels]
                            updates[code] = ",".join(mapped)
                        else:
                            updates[code] = opt_map.get(display_value, display_value)

                    if st.button(f"💾 Save attributes for {sku}", key=f"save_{sku}"):
                        try:
                            apply_product_update(sku, updates)
                            st.success("Saved to Magento.")
                        except Exception as e:
                            st.error(f"Failed to save: {e}")
