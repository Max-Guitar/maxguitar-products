import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_PRODUCT_FIELDS = (
    "items[sku,name,attribute_set_id,extension_attributes[stock_item[qty]]],total_count"
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
        self._last_total_count = None

    def get(self, path: str, params=None):
        response = self.session.get(
            f"{self.base}{path}", params=params, timeout=self.timeout
        )
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

    def _fetch_product_page(
        self,
        page: int,
        page_size: int,
        fields: str,
        attribute_set_id: int | None,
    ):
        params = {
            "searchCriteria[currentPage]": page,
            "searchCriteria[pageSize]": page_size,
            "fields": fields,
        }
        if attribute_set_id is not None:
            params.update(
                {
                    "searchCriteria[filterGroups][0][filters][0][field]": "attribute_set_id",
                    "searchCriteria[filterGroups][0][filters][0][value]": attribute_set_id,
                    "searchCriteria[filterGroups][0][filters][0][condition_type]": "eq",
                }
            )

        data, _ = self.get("/rest/V1/products", params)
        if not isinstance(data, dict):
            print(
                f"[MagentoClient] Unexpected response type on page {page}: {type(data)}"
            )
            raise TypeError("Magento API returned non-dict payload")
        return data

    def iter_products(
        self,
        qty_min: float | None = 0,
        page_size: int = 200,
        max_pages: int = 50,
        attribute_set_id: int | None = 4,
        progress_cb=None,
        fields: str = DEFAULT_PRODUCT_FIELDS,
    ):
        """Yield products from the paginated Magento API."""

        page_size = max(1, min(int(page_size), 1000))
        qty_threshold = None
        if qty_min is not None:
            try:
                qty_threshold = float(qty_min)
            except (TypeError, ValueError):
                qty_threshold = 0.0
        page = 1
        fetched = 0
        total_count = None
        self._last_total_count = None

        while page <= max_pages:
            data = self._fetch_product_page(page, page_size, fields, attribute_set_id)
            total_count = data.get("total_count", total_count)
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                print(
                    f"[MagentoClient] Unexpected items type on page {page}: {type(page_items)}"
                )
                raise TypeError("Magento API returned non-list items payload")

            print(
                f"[MagentoClient] Page {page} fetched {len(page_items)} items (total_count={total_count})"
            )

            if not page_items:
                if progress_cb is not None:
                    total_pages = (
                        (total_count + page_size - 1) // page_size
                        if total_count
                        else None
                    )
                    progress_cb(
                        page=page,
                        total_pages=total_pages,
                        fetched=fetched,
                        total_count=total_count,
                    )
                break

            for product in page_items:
                normalized = self._normalize_extension_attributes(product)
                qty = self._extract_stock_qty(normalized)
                if qty_threshold is None or qty >= qty_threshold:
                    yield normalized

            fetched += len(page_items)

            if progress_cb is not None:
                total_pages = (
                    (total_count + page_size - 1) // page_size
                    if total_count
                    else None
                )
                progress_cb(
                    page=page,
                    total_pages=total_pages,
                    fetched=fetched,
                    total_count=total_count,
                )

            if total_count is not None and fetched >= total_count:
                break

            page += 1

        self._last_total_count = total_count if total_count is not None else fetched

    def iter_products_qty_gt(
        self,
        qty_min: float | None = 1,
        page_size: int = 200,
        max_pages: int = 5,
        attribute_set_id: int | None = 4,
        progress_cb=None,
    ):
        yield from self.iter_products(
            qty_min=qty_min,
            page_size=page_size,
            max_pages=max_pages,
            attribute_set_id=attribute_set_id,
            progress_cb=progress_cb,
            fields=
            "items[sku,name,attribute_set_id,extension_attributes[stock_item[qty]]],total_count",
        )

    def get_default_products(
        self,
        qty_min: float | None = 0,
        page_size: int = 200,
        max_pages: int = 50,
        attribute_set_id: int | None = 4,
        progress_cb=None,
    ):
        """Return a dictionary containing the fetched products."""

        items = list(
            self.iter_products(
                qty_min=qty_min,
                page_size=page_size,
                max_pages=max_pages,
                attribute_set_id=attribute_set_id,
                progress_cb=progress_cb,
            )
        )
        total_count = self._last_total_count or len(items)
        return {"items": items, "total_count": total_count}


class Client:
    def __init__(self):
        self._cli = None

    def _ensure(self):
        if self._cli is None:
            import streamlit as st

            self._cli = MagentoClient(
                st.secrets["MAGENTO_BASE_URL"],
                st.secrets["MAGENTO_ADMIN_TOKEN"],
            )

    def get_default_products(self, qty_min=1, page_size=200):
        self._ensure()
        items = list(
            self._cli.iter_products_qty_gt(
                qty_min=qty_min, page_size=page_size, max_pages=5
            )
        )
        return {"items": items}

    def get_stock_item(self, sku: str):
        self._ensure()
        data, _ = self._cli.get(f"/rest/V1/stockItems/{sku}")
        return data


client = Client()
