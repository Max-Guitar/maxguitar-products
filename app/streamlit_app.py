"""Streamlit front end for reviewing and enriching Magento catalog data."""

import sys
from pathlib import Path

import streamlit as st
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connectors.magento import client
from services.llm_extract import extract_attributes
from services.normalize import normalize_value
from services.apply import apply_product_update

st.set_page_config(page_title="Magento Product Enricher", layout="wide")

st.title("🎸 Magento Product Enricher")

if "products" not in st.session_state:
    st.session_state.products = []

if st.button("🔄 Load Default Products"):
    data = client.get_default_products()
    st.session_state.products = data.get("items", [])
    st.session_state.pop("generated", None)
    st.session_state.pop("attribute_details", None)
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

    selected_skus = st.multiselect("Select SKUs to enrich", current_df["sku"])
    hint = st.text_input("Optional hint (e.g. 'telecaster electric guitar')")

    if st.button("✨ Generate Specs"):
        results = []
        attribute_details = {}
        for sku in selected_skus:
            product = next((p for p in st.session_state.products if p["sku"] == sku), None)
            if not product:
                continue
            attr_set_id = product.get("attribute_set_id")
            attr_set_name = product.get("attribute_set_name")
            if set_name_by_id and attr_set_name in name_to_id:
                attr_set_id = name_to_id[attr_set_name]
            elif attr_set_id is None and attr_set_name:
                try:
                    attr_set_id = int(attr_set_name)
                except (TypeError, ValueError):
                    attr_set_id = None
            try:
                specs = extract_attributes(product["name"], hint)
                normalized = {k: normalize_value(k, str(v)) for k, v in specs.items()}
                results.append({"sku": sku, **normalized})
                if attr_set_id:
                    attributes = client.get_attributes_for_set(attr_set_id)
                else:
                    attributes = []
                custom_values = {
                    attr.get("attribute_code"): attr.get("value")
                    for attr in product.get("custom_attributes", [])
                }
                details_rows = [
                    {
                        "attribute_code": attr.get("attribute_code"),
                        "frontend_label": attr.get("frontend_label"),
                        "current_value": custom_values.get(attr.get("attribute_code"), ""),
                    }
                    for attr in attributes
                ]
                attribute_details[sku] = pd.DataFrame(
                    details_rows,
                    columns=["attribute_code", "frontend_label", "current_value"],
                )
            except Exception as e:
                st.warning(f"Failed to extract for {sku}: {e}")
        if results:
            st.session_state.generated = pd.DataFrame(results)
            st.session_state.attribute_details = attribute_details
            st.dataframe(st.session_state.generated)
        else:
            st.info("No specs generated.")
            st.session_state.pop("attribute_details", None)

    if "generated" in st.session_state:
        st.subheader("Review & Apply Changes")
        st.dataframe(st.session_state.generated)
        if st.session_state.get("attribute_details"):
            st.subheader("Attribute Set Details")
            for sku, details_df in st.session_state.attribute_details.items():
                st.markdown(f"**{sku}**")
                if details_df.empty:
                    st.info("No attributes found for this set.")
                else:
                    st.dataframe(details_df[["attribute_code", "frontend_label", "current_value"]])
        if st.button("💾 Write to Magento"):
            for _, row in st.session_state.generated.iterrows():
                sku = row["sku"]
                attrs = {k: v for k, v in row.items() if k != "sku"}
                apply_product_update(sku, attrs)
            st.success("All selected products updated!")
