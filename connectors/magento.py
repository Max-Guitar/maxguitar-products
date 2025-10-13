# connectors/magento.py
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Поля для облегчённой выдачи products (qty в products часто отсутствует при MSI)
DEFAULT_PRODUCT_FIELDS = (
    "items[sku,name,attribute_set_id,price],total_count"
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
        self._attribute_set_ids = {}

    # -------------------- low-level --------------------

    def get(self, path: str, params=None):
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json(), resp

    # -------------------- helpers --------------------

    @staticmethod
    def _normalize_extension_attributes(product: dict) -> dict:
        """Ensure extension attributes exist and are a dict."""
        ext = product.get("extension_attributes") or {}
        # Магенто может прислать custom_attributes как список; extension_attributes должен быть dict
        if not isinstance(ext, dict):
            ext = {}
        product["extension_attributes"] = ext
        return product

    @staticmethod
    def _extract_stock_qty_from_product(product: dict) -> float:
        """Try to read qty from product payload (works only if stock_item present)."""
        try:
            ext = product.get("extension_attributes") or {}
            stock = ext.get("stock_item") or {}
            return float(stock.get("qty") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _fetch_stock_for_sku(self, sku: str) -> dict:
        """GET /rest/V1/stockItems/{sku} and return dict (may contain qty, is_in_stock, etc)."""
        data, _ = self.get(f"/rest/V1/stockItems/{sku}")
        return data if isinstance(data, dict) else {}

    def _ensure_stock_on_product(self, product: dict) -> float:
        """
        Гарантировать наличие product['extension_attributes']['stock_item']['qty'].
        Возвращает найденный qty (0.0 если не получилось).
        """
        product = self._normalize_extension_attributes(product)
        qty = self._extract_stock_qty_from_product(product)
        if qty is not None and qty > 0:
            return qty

        sku = product.get("sku")
        if not sku:
            return 0.0

        stock = self._fetch_stock_for_sku(sku)
        try:
            qty = float(stock.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0

        # проставим в product, чтобы UI мог показать qty без повторного запроса
        ext = product.setdefault("extension_attributes", {})
        stock_item = ext.setdefault("stock_item", {})
        if isinstance(stock_item, dict):
            stock_item["qty"] = qty
            if "is_in_stock" in stock:
                stock_item["is_in_stock"] = stock.get("is_in_stock")
        return qty

    def get_attribute_set_id(
        self,
        attribute_set_name: str = "Default",
        fallback: int = 4,
    ) -> int:
        """Return the attribute set ID for the given name, caching lookups."""
        if attribute_set_name in self._attribute_set_ids:
            return self._attribute_set_ids[attribute_set_name]

        params = {
            "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_name",
            "searchCriteria[filter_groups][0][filters][0][value]": attribute_set_name,
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
        }

        attribute_set_id = fallback
        try:
            data, _ = self.get("/rest/V1/eav/attribute-sets/list", params=params)
            items = data.get("items") or []
            for item in items:
                if isinstance(item, dict) and item.get("attribute_set_name") == attribute_set_name:
                    try:
                        attribute_set_id = int(item.get("attribute_set_id", fallback))
                    except (TypeError, ValueError):
                        attribute_set_id = fallback
                    break
        except Exception:
            attribute_set_id = fallback

        self._attribute_set_ids[attribute_set_name] = attribute_set_id
        return attribute_set_id

    # -------------------- product listing --------------------

    def _fetch_products_page(
        self,
        page: int,
        page_size: int,
        fields: str = DEFAULT_PRODUCT_FIELDS,
        attribute_set_id: int | None = None,
    ):
        params = {
            "searchCriteria[currentPage]": page,
            "searchCriteria[pageSize]": page_size,
            "fields": fields,
        }
        if attribute_set_id is not None:
            params.update(
                {
                    "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
                    "searchCriteria[filter_groups][0][filters][0][value]": attribute_set_id,
                    "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
                }
            )
        return self.get("/rest/V1/products", params=params)

    def iter_products_qty_gt(
        self,
        qty_min: float = 1,
        page_size: int = 200,
        max_pages: int = 10,
        attribute_set_name: str = "Default",
        limit: int = 500,
        fields: str = DEFAULT_PRODUCT_FIELDS,
    ):
        """
        Итератор по продуктам с qty>qty_min и attribute_set=attribute_set_name.
        В Magento с MSI qty часто отсутствует в /products, поэтому для каждого SKU
        при необходимости делаем доп. запрос /stockItems/{sku}.
        """
        page = 1
        yielded = 0
        attrset_id = self.get_attribute_set_id(attribute_set_name)

        while page <= max_pages and yielded < limit:
            data, _ = self._fetch_products_page(page=page, page_size=page_size, fields=fields, attribute_set_id=attrset_id)
            items = data.get("items") or []
            if not items:
                break

            for product in items:
                # Проставим qty (берём из product, а если нет — подтягиваем из stockItems)
                qty = self._ensure_stock_on_product(product)
                if qty > qty_min:
                    yield product
                    yielded += 1
                    if yielded >= limit:
                        break

            total = data.get("total_count", 0)
            if page * page_size >= total:
                break
            page += 1

    def get_default_products(
        self,
        qty_min: float = 1,
        page_size: int = 200,
        max_pages: int = 10,
        attribute_set_name: str = "Default",
        limit: int = 500,
    ):
        """Return a Magento-style payload of products above the quantity threshold and in the Default attribute set."""
        return {
            "items": list(
                self.iter_products_qty_gt(
                    qty_min=qty_min,
                    page_size=page_size,
                    max_pages=max_pages,
                    attribute_set_name=attribute_set_name,
                    limit=limit,
                )
            )
        }

    def get_stock_item(self, sku: str):
        """Legacy method used by old UI paths."""
        return self._fetch_stock_for_sku(sku)


# ---- Streamlit-friendly wrapper (reads secrets toml) ----
class StreamlitClient:
    """Lazy wrapper that instantiates MagentoClient using Streamlit secrets."""
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

    # Явные методы для стабильности интерфейса
    def get_default_products(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_default_products(*args, **kwargs)

    def get_stock_item(self, *args, **kwargs):
        self._ensure()
        return self._cli.get_stock_item(*args, **kwargs)


# Preserve the historical public API
Client = StreamlitClient
client = StreamlitClient()
