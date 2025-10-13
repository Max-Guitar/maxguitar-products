# app/streamlit_app.py

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# make project root importable (for connectors/services)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Streamlit config MUST be the first Streamlit command
st.set_page_config(page_title="Magento Product Enricher", layout="wide")

from connectors.magento import client  # noqa: E402
from services.llm_extract import extract_attributes  # noqa: E402
from services.normalize import normalize_value  # noqa: E402
from services.apply import apply_product_update  # noqa: E402

st.title("🎸 Magento Product Enricher")

# session state
if "products" not in st.session_state:
    st.session_state.products = []
if "generated" not in st.session_state:
    st.session_state.generated = None

st.caption("Streamlit front end for reviewing and enriching Magento catalog data.")

# --- Load products (eligible: qty > 1) ---
if st.button("🔄 Load Eligible Products (qty > 1)"):
    with st.status("Loading products from Magento…", expanded=True) as status:
        status.write("Requesting product catalog…")
        data = client.get_default_products()
        items = data.get("items", [])
        total_count = data.get("total_count") or len(items)

        eligible = []
        progress = st.progress(0)

        for index, product in enumerate(items, start=1):
            status.update(
                label=f"Filtering Magento products ({index}/{total_count or len(items)})",
                state="running",
            )

            extension_attributes = product.get("extension_attributes") or {}
            stock_item = extension_attributes.get("stock_item") if isinstance(extension_attributes, dict) else {}
            if not isinstance(stock_item, dict):
                stock_item = {}

            try:
                qty = float(stock_item.get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0.0

            if qty > 1:
                eligible.append(product)

            if total_count:
                progress.progress(int(min(index / total_count, 1.0) * 100))
            else:
                progress.progress(100)

        status.update(label="Finished processing Magento catalog", state="complete")
        st.session_state.products = eligible

    if st.session_state.products:
        st.success(f"Loaded {len(st.session_state.products)} eligible products (qty > 1)")
    else:
        st.warning("No products found with quantity greater than 1.")

# --- Table + selection ---
if st.session_state.products:
    df = pd.DataFrame(
        [
            {
                "sku": p.get("sku"),
                "name": p.get("name"),
                "attribute_set_id": p.get("attribute_set_id"),
                "created_at": p.get("created_at"),
            }
            for p in st.session_state.products
        ]
    )
    st.dataframe(df, use_container_width=True)

    selected_skus = st.multiselect("Select SKUs to enrich", df["sku"].tolist())
    hint = st.text_input("Optional hint (e.g. 'telecaster electric guitar')")

    if st.button("✨ Generate Specs"):
        results = []
        for sku in selected_skus:
            product = next((p for p in st.session_state.products if p.get("sku") == sku), None)
            if not product:
                continue
            specs = extract_attributes(product.get("name", ""), hint)

            # normalize simple string specs if mapping exists
            normalized = {}
            for k, v in (specs or {}).items():
                if k.startswith("_"):
                    normalized[k] = v  # pass through error keys
                else:
                    normalized[k] = normalize_value(k, v) if isinstance(v, str) else v

            results.append({"sku": sku, **normalized})

        st.session_state.generated = pd.DataFrame(results) if results else None
        if st.session_state.generated is not None:
            st.dataframe(st.session_state.generated, use_container_width=True)

# --- Review & apply ---
if st.session_state.get("generated") is not None and not st.session_state.generated.empty:
    st.subheader("Review & Apply Changes")
    st.dataframe(st.session_state.generated, use_container_width=True)

    if st.button("💾 Write to Magento"):
        with st.spinner("Writing updates to Magento…"):
            for _, row in st.session_state.generated.iterrows():
                sku = row["sku"]
                attrs = {k: row[k] for k in row.index if k != "sku"}
                apply_product_update(sku, attrs)
        st.success("All selected products updated!")
