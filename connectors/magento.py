# connectors/magento.py
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class MagentoClient:
    def __init__(self, base_url: str, token: str, timeout=(10, 60)):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        })
        retry = Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.timeout = timeout

    def get(self, path: str, params=None):
        t0 = time.time()
        r = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json(), time.time() - t0

    def iter_products_qty_gt(self, qty_min=1, page_size=200, max_pages=50):
        page = 1
        fetched = 0
        while page <= max_pages:
            params = {
                "searchCriteria[currentPage]": page,
                "searchCriteria[pageSize]": page_size,
                "fields": "items[sku,name,price,extension_attributes[stock_item[qty,is_in_stock]]],total_count",
            }
            data, _ = self.get("/rest/V1/products", params)
            items = data.get("items", [])
            if not items:
                break

            for it in items:
                qty = (it.get("extension_attributes", {})
                        .get("stock_item", {})
                        .get("qty"))
                if qty is None:
                    # fallback для legacy эндпоинта stockItems/{sku}
                    try:
                        stock, _ = self.get(f"/rest/V1/stockItems/{it['sku']}")
                        qty = stock.get("qty")
                    except Exception:
                        qty = None

                if qty is not None and float(qty) > float(qty_min):
                    yield it

            fetched += len(items)
            if fetched >= data.get("total_count", fetched):
                break
            page += 1

# ---- Backward compatibility export ----
def client(base_url: str, token: str, timeout=(10, 60)) -> MagentoClient:
    """Factory kept for legacy imports: from connectors.magento import client"""
    return MagentoClient(base_url, token, timeout)

# ---- Legacy API for streamlit_app.py ----
class Client:
    def __init__(self):
        self._cli = None

    def _ensure(self):
        if self._cli is None:
            import streamlit as st
            self._cli = MagentoClient(
                st.secrets["MAGENTO_BASE_URL"],
                st.secrets["MAGENTO_ADMIN_TOKEN"]
            )

    def get_default_products(self, qty_min=1, page_size=200):
        self._ensure()
        items = list(self._cli.iter_products_qty_gt(qty_min=qty_min, page_size=page_size))
        return {"items": items}  # UI expects dict with "items"

    def get_stock_item(self, sku: str):
        self._ensure()
        data, _ = self._cli.get(f"/rest/V1/stockItems/{sku}")
        return data

client = Client()
