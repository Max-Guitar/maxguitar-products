# app/streamlit_app.py (фрагмент загрузки)
import streamlit as st
from connectors.magento import MagentoClient

@st.cache_data(ttl=300, show_spinner=False)
def load_eligible_products(qty_min: int, page_size: int):
    cli = MagentoClient(st.secrets["MAGENTO_BASE_URL"], st.secrets["MAGENTO_ADMIN_TOKEN"])
    rows=[]
    for i, p in enumerate(cli.iter_products_qty_gt(qty_min=qty_min, page_size=page_size), start=1):
        rows.append({
            "sku": p["sku"],
            "name": p.get("name"),
            "price": p.get("price"),
            "qty": p.get("extension_attributes",{}).get("stock_item",{}).get("qty")
        })
        if i % 50 == 0:
            st.session_state["load_progress"]=i
    return rows

st.title("Magento Product Enricher")
qty_min = 1
page_size = st.sidebar.number_input("Page size", 50, 500, 200, 50)

if st.button(f"Load Eligible Products (qty > {qty_min})"):
    try:
        with st.status("Loading products from Magento…", expanded=True) as s:
            st.write("Connecting with retries & timeouts…")
            data = load_eligible_products(qty_min, page_size)
            if not data:
                s.update(state="error", label="No products found with qty filter.")
            else:
                s.update(state="complete", label=f"Loaded {len(data)} products.")
                st.dataframe(data, use_container_width=True)
    except Exception as e:
        st.error(f"Failed to load: {e}")
