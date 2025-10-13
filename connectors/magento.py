import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_PRODUCT_FIELDS = (
    "items[sku,name,price,attribute_set_id,extension_attributes[stock_item[qty,is_in_stock]]],total_count"
)


class MagentoClient:
    """Thin wrapper around the Magento REST API used by the app."""

    def __init__(self, base_url: str, token: str, timeout=(10, 60)):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
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
        response = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json(), response

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

    @staticmethod
    def _extract_stock_qty(product: dict) -> float:
        extension_attributes = product.get("extension_attributes") or {}
        stock_item = extension_attributes.get("stock_item") if isinstance(extension_attributes, dict) else {}
        if not isinstance(stock_item, dict):
            return 0.0

        try:
            return float(stock_item.get("qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def iter_products_qty_gt(
        self,
        qty_min: float = 1,
        page_size: int = 200,
        max_pages: int = 5,
        fields: str = DEFAULT_PRODUCT_FIELDS,
    ):
        """Yield products whose stock quantity is greater than ``qty_min``."""

        page = 1
        fetched = 0

        while page <= max_pages:
            data, _ = self.get(
                "/rest/V1/products",
                params={
                    "searchCriteria[currentPage]": page,
                    "searchCriteria[pageSize]": page_size,
                    "fields": fields,
                },
            )

            items = data.get("items", [])
            if not items:
                break

            for product in items:
                product = self._normalize_extension_attributes(product)
                if self._extract_stock_qty(product) > qty_min:
                    yield product

            fetched += len(items)
            if fetched >= data.get("total_count", fetched):
                break

            page += 1

    def get_default_products(self, qty_min: float = 1, page_size: int = 200, max_pages: int = 5):
        """Return a Magento-style payload of products above the quantity threshold."""

        return {
            "items": list(
                self.iter_products_qty_gt(
                    qty_min=qty_min,
                    page_size=page_size,
                    max_pages=max_pages,
                )
            )
        }

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

    def get_default_products(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_default_products(*args, **kwargs)

    def get_stock_item(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_stock_item(*args, **kwargs)


# Preserve the historical public API
Client = StreamlitClient
client = StreamlitClient()
