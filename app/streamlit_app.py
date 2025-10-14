"""Streamlit front end for reviewing and enriching Magento catalog data."""

import sys
from pathlib import Path
from typing import Dict, Set

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.magento import client
from services.apply import apply_product_update
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

    allowed_codes_cache: Dict[int, Set[str]] = {}

    def fetch_allowed_codes_for_set(set_id: int | None) -> Set[str]:
        if set_id is None:
            return set()
        if set_id in allowed_codes_cache:
            return allowed_codes_cache[set_id]

        allowed: Set[str] = set()
        try:
            groups_resp = client.get_attribute_groups(set_id)
        except Exception as exc:
            st.warning(f"Failed to load attribute groups for set {set_id}: {exc}")
            groups_resp = []
        if isinstance(groups_resp, dict):
            groups = groups_resp.get("items", []) or []
        else:
            groups = groups_resp or []
        for group in groups:
            group_id = (
                group.get("attribute_group_id")
                or group.get("group_id")
                or group.get("id")
            )
            if not group_id:
                continue
            try:
                attrs = client.get_attributes_for_group(set_id, group_id)
            except Exception as exc:
                st.warning(
                    f"Failed to load attributes for group {group_id} in set {set_id}: {exc}"
                )
                continue
            for attr in attrs or []:
                code = attr.get("attribute_code") or attr.get("code")
                if code:
                    allowed.add(code)

        if not allowed:
            try:
                attrs = client.get_attributes_for_set(set_id)
            except Exception as exc:
                st.warning(f"Failed to load attributes for set {set_id}: {exc}")
                attrs = []
            for attr in attrs or []:
                code = attr.get("attribute_code") or attr.get("code")
                if code:
                    allowed.add(code)

        allowed_codes_cache[set_id] = allowed
        return allowed

    if st.button("✨ Generate Specs"):
        if not selected_skus:
            st.info("Select at least one SKU.")
        else:
            results = []
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
                raw_set_id = product.get("attribute_set_id")
                try:
                    set_id = int(raw_set_id)
                except (TypeError, ValueError):
                    set_id = None
                allowed_codes = fetch_allowed_codes_for_set(set_id)
                filtered = {
                    code: val
                    for code, val in normalized.items()
                    if not allowed_codes or code in allowed_codes
                }
                results.append({"sku": sku, **filtered})
            if results:
                st.dataframe(pd.DataFrame(results))
            else:
                st.info("No specs generated yet. Try adjusting the hint or selection.")

    target_groups = {
        "General": {"only_codes": {"brand"}},
        "Filters": {"only_codes": None},
        "Specifications": {"only_codes": None},
        "Content": {"only_codes": {"short_description"}},
    }

    def fetch_target_group_meta_for_set(set_id: int):
        groups = client.get_attribute_groups(set_id)
        name_to_id = {g["attribute_group_name"]: g["attribute_group_id"] for g in groups}
        meta = {}
        for gname, conf in target_groups.items():
            gid = name_to_id.get(gname)
            if not gid:
                continue
            attrs = client.get_attributes_for_group(set_id, gid)
            for attr in attrs:
                code = attr.get("attribute_code")
                if not code:
                    continue
                if conf["only_codes"] and code not in conf["only_codes"]:
                    continue
                meta[code] = {
                    "group": gname,
                    "label": attr.get("frontend_label") or code,
                    "input": attr.get("frontend_input"),
                    "options": attr.get("options"),
                }
        return meta

    attr_meta_cache = {}  # set_id -> meta

    if st.button("🔎 Get Specs"):
        if not selected_skus:
            st.info("Select at least one SKU.")
        else:
            tabs = st.tabs([f"{sku}" for sku in selected_skus])
            for tab, sku in zip(tabs, selected_skus):
                with tab:
                    product = next((p for p in st.session_state.products if p["sku"] == sku), None)
                    if not product:
                        st.info("Product not found in session.")
                        continue
                    prod_full = client.get_product(sku)
                    raw_set_id = prod_full.get("attribute_set_id") or product.get("attribute_set_id")
                    try:
                        set_id = int(raw_set_id)
                    except (TypeError, ValueError):
                        st.warning("Attribute set is missing for this product; cannot load attributes.")
                        continue
                    if set_id not in attr_meta_cache:
                        attr_meta_cache[set_id] = fetch_target_group_meta_for_set(set_id)
                    meta = attr_meta_cache[set_id]
                    curr = {
                        attr["attribute_code"]: attr.get("value")
                        for attr in prod_full.get("custom_attributes", [])
                    }

                    rows = []
                    for code, m in meta.items():
                        rows.append(
                            {
                                "attribute_code": code,
                                "group": m["group"],
                                "label": m["label"],
                                "input": m["input"],
                                "options": ", ".join([opt["label"] for opt in m["options"]]) if m["options"] else "",
                                "value": curr.get(code, ""),
                            }
                        )
                    edit_df = pd.DataFrame(rows)

                    col_cfg = {
                        "attribute_code": st.column_config.TextColumn(disabled=True),
                        "group": st.column_config.TextColumn(disabled=True),
                        "label": st.column_config.TextColumn(disabled=True),
                        "input": st.column_config.TextColumn(disabled=True),
                        "options": st.column_config.TextColumn(disabled=True),
                    }
                    st.caption("Edit values below; select fields expect option *labels* (we'll map to values on save).")
                    edited = st.data_editor(edit_df, hide_index=True, column_config=col_cfg, key=f"attr_editor_{sku}")

                    if st.button(f"💾 Save attributes for {sku}", key=f"write_{sku}"):
                        updates = {}
                        for _, r in edited.iterrows():
                            code = r["attribute_code"]
                            val = r["value"]
                            m = meta[code]
                            if m["options"]:
                                lbl_to_val = {o["label"]: str(o["value"]) for o in m["options"]}
                                if m["input"] == "multiselect":
                                    labels = [x.strip() for x in str(val).split(",") if x.strip()]
                                    mapped = [lbl_to_val.get(x, x) for x in labels]
                                    val = ",".join(mapped)
                                else:
                                    val = lbl_to_val.get(str(val), str(val))
                            updates[code] = val
                        try:
                            apply_product_update(sku, updates)
                            st.success("Saved to Magento.")
                        except Exception as e:
                            st.error(f"Failed to save: {e}")
