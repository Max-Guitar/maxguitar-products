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

# Maintain product data across user interactions.
if "products" not in st.session_state:
    st.session_state.products = []

if st.button("🔄 Load Eligible Products"):
    data = client.get_default_products()
    items = data.get("items", [])
    eligible = []
    for product in items:
        sku = product.get("sku")
        if not sku:
            continue
        try:
            stock_item = client.get_stock_item(sku)
        except Exception:
            continue
        qty_value = stock_item.get("qty", 0)
        try:
            qty = float(qty_value)
        except (TypeError, ValueError):
            qty = 0.0
        if qty > 1:
            eligible.append(product)
    st.session_state.products = eligible
    st.success(f"Loaded {len(st.session_state.products)} eligible products")

if st.session_state.products:
    df = pd.DataFrame(
        [
            {
                "sku": p["sku"],
                "name": p["name"],
                "attribute_set_id": p["attribute_set_id"],
                "created_at": p["created_at"],
            }
            for p in st.session_state.products
        ]
    )
    st.dataframe(df)

    selected_skus = st.multiselect("Select SKUs to enrich", df["sku"])
    hint = st.text_input("Optional hint (e.g. 'telecaster electric guitar')")

    if st.button("✨ Generate Specs"):
        results = []
        for sku in selected_skus:
            product = next(
                (p for p in st.session_state.products if p["sku"] == sku), None
            )
            if not product:
                continue
            specs = extract_attributes(product["name"], hint)
            results.append({"sku": sku, **specs})
        st.session_state.generated = pd.DataFrame(results)
        st.dataframe(st.session_state.generated)

    if "generated" in st.session_state:
        st.subheader("Review & Apply Changes")
        st.dataframe(st.session_state.generated)
        if st.button("💾 Write to Magento"):
            for _, row in st.session_state.generated.iterrows():
                sku = row["sku"]
                attrs = {k: v for k, v in row.items() if k != "sku"}
                apply_product_update(sku, attrs)
            st.success("All selected products updated!")
