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

def _stock_qty(product: dict) -> float:
    extension_attributes = product.get("extension_attributes") or {}
    stock_item = (
        extension_attributes.get("stock_item")
        if isinstance(extension_attributes, dict)
        else {}
    )
    if not isinstance(stock_item, dict):
        return 0.0

    try:
        return float(stock_item.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# --- Load products (eligible: qty > 1) ---
if st.button("🔄 Load Eligible Products (qty > 1)"):
    st.session_state.products = []

    with st.status("Loading products from Magento…", expanded=True) as status:
        progress = st.progress(0)
        progress_state = {"pages": None, "count": None}

        def _update_progress(*, page, total_pages, fetched, total_count):
            if total_pages is not None:
                progress_state["pages"] = total_pages
            if total_count is not None:
                progress_state["count"] = total_count

            if progress_state["pages"]:
                total_pages_known = progress_state["pages"]
                current = min(page, total_pages_known)
                progress_ratio = current / max(total_pages_known, 1)
                progress.progress(int(min(progress_ratio, 1.0) * 100))
                status.update(
                    label=f"Fetching Magento products (page {current}/{total_pages_known})",
                    state="running",
                )
            elif progress_state["count"]:
                total_count_known = progress_state["count"]
                progress_ratio = fetched / max(total_count_known, 1)
                progress.progress(int(min(progress_ratio, 1.0) * 100))
                status.update(
                    label=f"Fetching Magento products ({fetched}/{total_count_known})",
                    state="running",
                )
            else:
                status.update(
                    label=f"Fetching Magento products (page {page})",
                    state="running",
                )

        try:
            status.write("Requesting product catalog…")
            data = client.get_default_products(qty_min=None, progress_cb=_update_progress)
        except Exception as exc:
            status.update(label="Failed to load Magento products", state="error")
            st.error(f"Error loading products: {exc}")
        else:
            items = data.get("items", [])
            total_count = data.get("total_count") or len(items)

            status.update(label="Filtering Magento products by stock…", state="running")
            eligible = [product for product in items if _stock_qty(product) > 1]

            progress.progress(100)
            status.update(label="Finished processing Magento catalog", state="complete")
            st.session_state.products = eligible

            if st.session_state.products:
                st.success(
                    f"Loaded {len(st.session_state.products)} eligible products (qty > 1)"
                )
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
