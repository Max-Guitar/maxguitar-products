# connectors/magento.py
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Поля для облегчённой выдачи products (qty в products часто отсутствует при MSI)
DEFAULT_PRODUCT_FIELDS = (
    "items[sku,name,attribute_set_id,price,extension_attributes[stock_item[qty,is_in_stock]]],total_count"
)


DEFAULT_ATTRIBUTE_SET_ID = 4


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

    def _fetch_salable_qty(self, sku: str, stock_id: int = 1) -> float:
        try:
            data, _ = self.get(
                f"/rest/V1/inventory/get-product-salable-quantity/{sku}/{stock_id}"
            )
            if isinstance(data, (int, float, str)):
                return float(data)
            return float(data or 0)
        except Exception:
            return 0.0

    def _get_effective_qty(self, product: dict, stock_id: int = 1) -> float:
        sku = product.get("sku") or ""
        if not sku:
            return 0.0
        msi_qty = self._fetch_salable_qty(sku, stock_id=stock_id)
        legacy = self._fetch_stock_for_sku(sku)
        try:
            legacy_qty = float(legacy.get("qty") or 0)
        except Exception:
            legacy_qty = 0.0
        qty = max(msi_qty, legacy_qty)
        ext = product.setdefault("extension_attributes", {})
        stock_item = ext.setdefault("stock_item", {})
        if isinstance(stock_item, dict):
            stock_item["qty"] = qty
            stock_item["is_in_stock"] = bool(qty and qty > 0)
        return qty

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
        fallback: int = DEFAULT_ATTRIBUTE_SET_ID,
    ) -> int:
        """Return the attribute set ID for the given name, caching lookups."""
        if attribute_set_name in self._attribute_set_ids:
            return self._attribute_set_ids[attribute_set_name]

        if attribute_set_name == "Default":
            self._attribute_set_ids[attribute_set_name] = DEFAULT_ATTRIBUTE_SET_ID
            return DEFAULT_ATTRIBUTE_SET_ID

        params = {
            "searchCriteria[filter_groups][0][filters][0][field]": "entity_type_id",
            "searchCriteria[filter_groups][0][filters][0][value]": 4,
            "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
            "searchCriteria[filter_groups][1][filters][0][field]": "attribute_set_name",
            "searchCriteria[filter_groups][1][filters][0][value]": attribute_set_name,
            "searchCriteria[filter_groups][1][filters][0][condition_type]": "eq",
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
        attr_id = DEFAULT_ATTRIBUTE_SET_ID if attribute_set_id is None else attribute_set_id
        params.update(
            {
                "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
                "searchCriteria[filter_groups][0][filters][0][value]": attr_id,
                "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
            }
        )
        return self.get("/rest/V1/products", params=params)

    def iter_products_qty_gt(
        self,
        qty_min: float = 0,
        page_size: int = 500,
        max_pages: int | None = None,
        attribute_set_name: str = "Default",
        attribute_set_id: int | None = DEFAULT_ATTRIBUTE_SET_ID,
        limit: int = 1000,
        fields: str = DEFAULT_PRODUCT_FIELDS,
        use_parallel: bool = True,
        progress_callback=None,
    ):
        """
        Итератор по продуктам с qty>qty_min и attribute_set=attribute_set_name.
        В Magento с MSI qty часто отсутствует в /products, поэтому для каждого SKU
        при необходимости делаем доп. запрос /stockItems/{sku}.
        """
        page = 1
        yielded = 0
        total = None
        planned_pages = max_pages if max_pages is not None else None
        pages_read = 0
        attrset_id = (
            attribute_set_id
            if attribute_set_id is not None
            else self.get_attribute_set_id(attribute_set_name, fallback=DEFAULT_ATTRIBUTE_SET_ID)
        )

        while True:
            if planned_pages is not None and page > planned_pages:
                break
            try:
                data, _ = self._fetch_products_page(
                    page=page,
                    page_size=page_size,
                    fields=fields,
                    attribute_set_id=attrset_id,
                )
            except requests.HTTPError as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code in {429, 500, 502, 503, 504}:
                    time.sleep(0.5)
                    continue
                raise
            except Exception:
                time.sleep(0.1)
                continue

            pages_read += 1
            items = data.get("items") or []

            if total is None:
                total = int(data.get("total_count") or 0)
                pages = math.ceil(total / page_size) if page_size else 0
                if pages <= 0:
                    pages = 1
                if max_pages is None:
                    planned_pages = pages
                else:
                    planned_pages = min(pages, max_pages or pages)
                if planned_pages is None or planned_pages <= 0:
                    planned_pages = 1

            products = []
            missing_qty_products = []
            for product in items:
                product = self._normalize_extension_attributes(product)
                try:
                    product_attrset_id = int(product.get("attribute_set_id"))
                except (TypeError, ValueError):
                    product_attrset_id = None
                if product_attrset_id != attrset_id:
                    continue
                qty = self._extract_stock_qty_from_product(product)
                if qty <= 0:
                    missing_qty_products.append(product)
                products.append(product)

            if use_parallel and missing_qty_products:
                with ThreadPoolExecutor(max_workers=16) as executor:
                    futures = {
                        executor.submit(self._get_effective_qty, product, 1): product
                        for product in missing_qty_products
                    }
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception:
                            pass

            for product in products:
                qty = self._extract_stock_qty_from_product(product)
                if qty == 0:
                    qty = self._get_effective_qty(product, stock_id=1)
                if qty > qty_min:
                    yield product
                    yielded += 1
                    if yielded >= limit:
                        break

            if progress_callback is not None:
                try:
                    progress_callback(
                        {
                            "page": page,
                            "planned_pages": planned_pages or 1,
                            "pages_read": pages_read,
                            "items_received": len(items),
                            "eligible": yielded,
                            "total_count": total or 0,
                        }
                    )
                except Exception:
                    pass

            if yielded >= limit:
                break

            if planned_pages is not None and page >= planned_pages:
                break

            page += 1
            time.sleep(0.05)

    def get_default_products(
        self,
        qty_min: float = 0,
        page_size: int = 500,
        max_pages: int | None = None,
        attribute_set_name: str = "Default",
        attribute_set_id: int | None = DEFAULT_ATTRIBUTE_SET_ID,
        limit: int = 1000,
        use_parallel: bool = True,
        progress_callback=None,
    ):
        """Return a Magento-style payload of products above the quantity threshold and in the Default attribute set."""
        items = list(
            self.iter_products_qty_gt(
                qty_min=qty_min,
                page_size=page_size,
                max_pages=max_pages,
                attribute_set_name=attribute_set_name,
                attribute_set_id=attribute_set_id,
                limit=limit,
                use_parallel=use_parallel,
                progress_callback=progress_callback,
            )
        )

        return {"items": items}

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
