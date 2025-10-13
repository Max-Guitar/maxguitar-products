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

# --- Debug: check stock for a specific SKU (legacy vs MSI) ---
with st.expander("🔎 Check stock for SKU (legacy qty vs MSI salable qty)", expanded=True):
    sku = st.text_input("SKU to check", value="ART-25648")
    if st.button("Check stock now"):
        import requests, json
        base = st.secrets["MAGENTO_BASE_URL"].rstrip("/")
        token = st.secrets["MAGENTO_ADMIN_TOKEN"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        timeout = (10, 60)

        def _get(url):
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()

        try:
            legacy = _get(f"{base}/rest/V1/stockItems/{sku}")  # legacy stock_item
            st.write("Legacy /V1/stockItems response:")
            st.json(legacy)

            # MSI salable qty (stockId обычно 1; если у тебя другой — поменяй)
            salable = _get(f"{base}/rest/V1/inventory/get-product-salable-quantity/{sku}/1")
            st.write("MSI /V1/inventory/get-product-salable-quantity response:")
            st.json(salable)

            try:
                legacy_qty = float(legacy.get("qty", 0) or 0)
            except Exception:
                legacy_qty = 0.0
            try:
                msi_salable_qty = float(salable)
            except Exception:
                msi_salable_qty = None

            st.success(f"SKU={sku} → legacy qty={legacy_qty} ; MSI salable qty={msi_salable_qty}")
        except Exception as e:
            st.exception(e)


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


# --- Load products (qty>0 & attribute set id=4, capped) ---
if st.button("🔄 Load Eligible Products (qty>0 & attr set=4, max 200)"):
    st.session_state.products = []
    with st.status("Loading products from Magento…", expanded=True) as status:
        status.write("Requesting product catalog (page_size=200, max_pages=3, limit=200)…")
        data = client.get_default_products(
            qty_min=0,
            page_size=200,
            max_pages=3,
            limit=200,
            attribute_set_name="Default",
        )
        if not isinstance(data, dict) or "items" not in data:
            status.update(label="Bad response shape from Magento client", state="error")
            raise RuntimeError(
                f"Unexpected response: {type(data)} keys={list(getattr(data,'keys',lambda:[])())}"
            )
        items = data.get("items") or []
        status.write(f"Received {len(items)} products from API.")
        if not items:
            status.update(label="No products returned by API", state="error")
            raise RuntimeError("Empty items from Magento")
        eligible = [p for p in items if _stock_qty(p) > 0]
        st.session_state.products = eligible
        status.update(label="Finished processing Magento catalog", state="complete")
        st.write(f"Eligible: {len(eligible)} / Total fetched: {len(items)}")

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
