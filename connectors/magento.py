# connectors/magento.py
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_PRODUCT_FIELDS = (
    "items[sku,name,price,extension_attributes[stock_item[qty,is_in_stock]]],total_count"
)


class MagentoClient:
    """Thin wrapper around the Magento REST API used by the app."""

    def __init__(self, base_url: str, token: str, timeout=(10, 60)):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        retry = Retry(
            total=5,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
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

    @staticmethod
    def _normalize_extension_attributes(product: dict) -> dict:
        """Ensure extension attributes are exposed as a dictionary."""

        extension_attributes = product.get("extension_attributes") or {}
        if isinstance(extension_attributes, list):
            extension_attributes = {
                entry.get("attribute_code"): entry.get("value")
                for entry in extension_attributes
                if isinstance(entry, dict) and "attribute_code" in entry
            }

        if not isinstance(extension_attributes, dict):
            extension_attributes = {}

        product["extension_attributes"] = extension_attributes
        return product

    def _fetch_product_page(self, page: int, page_size: int, fields: str):
        params = {
            "searchCriteria[currentPage]": page,
            "searchCriteria[pageSize]": page_size,
            "fields": fields,
        }
        return self.get("/rest/V1/products", params)

    def get_default_products(self, page_size=200, max_pages=50, fields=DEFAULT_PRODUCT_FIELDS):
        """Fetch products using the Magento paginated API."""

        page = 1
        fetched = 0
        items = []
        total_count = None

        while page <= max_pages:
            data, _ = self._fetch_product_page(page, page_size, fields)
            total_count = data.get("total_count", total_count)
            page_items = data.get("items", [])
            if not page_items:
                break

            for product in page_items:
                items.append(self._normalize_extension_attributes(product))

            fetched += len(page_items)
            if total_count is not None and fetched >= total_count:
                break

            page += 1

        if total_count is None:
            total_count = fetched

        return {"items": items, "total_count": total_count}

    def get_stock_item(self, sku: str):
        data, _ = self.get(f"/rest/V1/stockItems/{sku}")
        return data

class StreamlitClient:
    """Lazy wrapper that instantiates :class:`MagentoClient` using Streamlit secrets."""

    def __init__(self):
        self._cli = None

    def _ensure(self):
        if self._cli is None:
            import streamlit as st

            self._cli = MagentoClient(
                st.secrets["MAGENTO_BASE_URL"],
                st.secrets["MAGENTO_ADMIN_TOKEN"],
            )

    def __getattr__(self, name):
        self._ensure()
        return getattr(self._cli, name)

    # Backwards compatibility for direct method calls
    def get_default_products(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_default_products(*args, **kwargs)

    def get_stock_item(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_stock_item(*args, **kwargs)


# Preserve the historical public API
Client = StreamlitClient
client = StreamlitClient()
