"""HTTP client helpers for interacting with Magento's REST API."""

from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Union
from urllib.parse import quote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError

from config import settings


class MagentoClient:
    """Thin wrapper around Magento REST endpoints used by the Streamlit app."""

    def __init__(self):
        self.base_url = settings.MAGENTO_BASE_URL.rstrip("/")
        self.token = settings.MAGENTO_ADMIN_TOKEN
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get(self, endpoint, params=None):
        """Perform a GET request against a Magento REST endpoint."""
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.get(url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def patch(self, endpoint, payload):
        """Perform an update (HTTP PUT) request against a Magento REST endpoint."""
        url = f"{self.base_url}/rest/V1/{endpoint.lstrip('/')}"
        r = httpx.put(url, headers=self.headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_default_products(self):
        """Return in-stock products that belong to the default attribute set."""

        set_id = self.get_default_attribute_set_id()
        query = """
        query ($pageSize: Int!, $currentPage: Int!, $setId: String!) {
          products(
            pageSize: $pageSize,
            currentPage: $currentPage,
            filter: {
              attribute_set_id: { eq: $setId }
              stock_status: { eq: IN_STOCK }
            }
          ) {
            items {
              sku
              name
              created_at
              attribute_set_id
              stock_status
            }
            total_count
            page_info {
              total_pages
              current_page
            }
          }
        }
        """

        page_size = 100
        current_page = 1
        items: List[Dict[str, Any]] = []
        total_count: Optional[int] = None

        while True:
            variables = {
                "pageSize": page_size,
                "currentPage": current_page,
                "setId": str(set_id),
            }
            data = self.graphql(query, variables=variables)
            products = (data.get("data") or {}).get("products") or {}
            page_items = products.get("items") or []
            items.extend(page_items)
            if total_count is None:
                total_count = products.get("total_count")
            page_info = products.get("page_info") or {}
            total_pages = page_info.get("total_pages") or 0
            if total_pages and current_page >= total_pages:
                break
            if not page_items:
                break
            if len(page_items) < page_size and not total_pages:
                break
            current_page += 1

        return {"items": items, "total_count": total_count or len(items)}

    def graphql(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute a GraphQL query against Magento."""

        url = f"{self.base_url}/graphql"
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        r = httpx.post(url, headers=self.headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        errors = data.get("errors")
        if errors:
            raise RuntimeError(f"Magento GraphQL error: {errors}")
        return data

    @lru_cache(maxsize=1)
    def get_default_attribute_set_id(self) -> int:
        """Return the attribute set id for the Magento default set."""

        try:
            sets = self.get_attribute_sets()
        except Exception:
            return 4

        for set_id, name in sets.items():
            if isinstance(name, str) and name.lower() == "default":
                return set_id

        try:
            return next(iter(sets.keys()))
        except StopIteration:
            return 4

    def get_product(self, sku: Union[int, str]) -> Dict[str, Any]:
        """Return a single product by SKU, ensuring the SKU is safe for URL usage."""

        encoded_sku = quote(str(sku), safe="")
        return self.get(f"products/{encoded_sku}")

    def get_product_quantity(self, sku: Union[int, str]) -> Optional[float]:
        """Return available quantity for a SKU, supporting both MSI and legacy stock APIs."""

        encoded_sku = quote(str(sku), safe="")
        msi_endpoints = [
            f"inventory/get-source-items/{encoded_sku}",
            f"inventory/get-product-sources/{encoded_sku}",
        ]

        for endpoint in msi_endpoints:
            try:
                data = self.get(endpoint)
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in (400, 404):
                    continue
                raise
            except Exception:
                continue

            if isinstance(data, dict):
                for key in (
                    "items",
                    "source_items",
                    "sources",
                    "sourceItems",
                    "sourceItemList",
                ):
                    if key in data:
                        candidate = data.get(key) or []
                        break
                else:
                    candidate = [data] if data else []
            else:
                candidate = data or []

            total = 0.0
            found_value = False
            for item in candidate:
                quantity = (
                    item.get("salable_quantity")
                    or item.get("quantity")
                    or item.get("available_quantity")
                )
                if quantity in (None, ""):
                    continue
                try:
                    total += float(quantity)
                    found_value = True
                except (TypeError, ValueError):
                    continue
            if found_value:
                return total

        try:
            data = self.get(f"stockItems/{encoded_sku}")
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code in (400, 404):
                return None
            raise
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        quantity = data.get("qty") or data.get("quantity")
        if quantity in (None, ""):
            return None
        try:
            return float(quantity)
        except (TypeError, ValueError):
            return None

    @lru_cache(maxsize=1)
    def get_attribute_sets(self):
        """Return a cached mapping of attribute set id -> name using robust endpoint discovery."""

        candidates = [
            ("products/attribute-sets/sets/list", {"searchCriteria[pageSize]": 200}),
            ("eav/attribute-sets/list", {"searchCriteria[pageSize]": 200}),
            ("products/attribute-sets/list", {"searchCriteria[pageSize]": 200}),
            ("products/attribute-sets/setsList", {"searchCriteria[current_page]": 1}),
        ]
        last_err = None

        for endpoint, params in candidates:
            try:
                data = self.get(endpoint, params=params)
            except Exception as exc:  # pragma: no cover - defensive against Magento variants
                last_err = exc
                continue

            items = data.get("items", data) if isinstance(data, dict) else data
            sets = {}
            for item in items or []:
                try:
                    set_id = int(item.get("attribute_set_id") or item.get("id"))
                except (TypeError, ValueError):
                    continue
                name = item.get("attribute_set_name") or item.get("name")
                if set_id and name:
                    sets[set_id] = name
            if sets:
                return sets

        raise RuntimeError("Failed to fetch attribute sets via all known endpoints") from last_err

    def get_attribute_groups(self, attribute_set_id: Union[int, str]) -> List[Dict[str, Any]]:
        """Return attribute groups for the given attribute set.

        We try the modern `/products/attribute-sets/{setId}/groups` endpoint first
        and gracefully fall back to the search-based variant if necessary."""

        try:
            data = self.get(f"products/attribute-sets/{attribute_set_id}/groups")
        except (RetryError, httpx.HTTPStatusError):
            params = {
                "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
                "searchCriteria[filter_groups][0][filters][0][value]": str(attribute_set_id),
                "searchCriteria[filter_groups][0][filters][0][condition_type]": "eq",
                "searchCriteria[pageSize]": 500,
            }
            data = self.get("products/attribute-sets/groups/list", params=params)
        items: Iterable[Dict[str, Any]]
        if isinstance(data, dict):
            if "items" in data:
                items = data.get("items", []) or []
            else:
                items = []
        else:
            items = data or []
        return list(items)

    def get_attributes_for_set(self, set_id: Union[int, str]) -> List[Dict[str, Any]]:
        """Return all attributes associated with a specific attribute set.

        Magento instances can expose this information via different endpoints. We
        attempt the modern attribute-set endpoint first and gracefully fall back
        to the legacy search-based endpoint if necessary.
        """

        try:
            data = self.get(f"products/attribute-sets/{set_id}/attributes")
        except Exception:  # pragma: no cover - compatibility with Magento variants
            params = {
                "searchCriteria[filter_groups][0][filters][0][field]": "attribute_set_id",
                "searchCriteria[filter_groups][0][filters][0][value]": str(set_id),
                "searchCriteria[pageSize]": 500,
            }
            data = self.get("products/attributes", params=params)
        items: Iterable[Dict[str, Any]]

        if isinstance(data, dict):
            for key in ("items", "attribute_set_attributes", "attributeSetAttributes"):
                if key in data:
                    candidate = data.get(key) or []
                    break
            else:
                candidate = []
            items = candidate
        else:
            items = data or []
        return list(items)

    def get_all_attributes(self) -> List[Dict[str, Any]]:
        """Return all attribute metadata from the global attributes listing."""

        data = self.get("products/attributes")
        if isinstance(data, dict):
            for key in ("items", "attributes", "attribute_list"):
                if key in data:
                    candidate = data.get(key) or []
                    break
            else:
                candidate = []
            return list(candidate)
        return list(data or [])

    def get_attributes_for_group(
        self, attribute_set_id: Union[int, str], group_id: Union[int, str]
    ) -> List[Dict[str, Any]]:
        """Return attributes belonging to a specific attribute group.

        Some Magento endpoints only need the group identifier in the path, but we
        keep the attribute set id in the signature for clarity and potential
        future validation.
        """

        endpoint = f"products/attribute-sets/groups/{group_id}/attributes"
        try:
            data = self.get(endpoint)
        except RetryError as retry_exc:
            last_exc_factory = getattr(retry_exc.last_attempt, "exception", None)
            last_exc = last_exc_factory() if callable(last_exc_factory) else None
            if (
                isinstance(last_exc, httpx.HTTPStatusError)
                and last_exc.response is not None
                and last_exc.response.status_code in (400, 404)
            ):
                return []
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code in (400, 404):
                return []
            raise
        items: Iterable[Dict[str, Any]]
        if isinstance(data, dict):
            items = data.get("items", []) or []
        else:
            items = data or []
        return list(items)

    def get_attribute(self, attribute_code: Union[int, str]) -> Dict[str, Any]:
        """Return metadata for a single attribute."""

        encoded_code = quote(str(attribute_code), safe="")
        return self.get(f"products/attributes/{encoded_code}")

    def get_attribute_options(self, attribute_code: Union[int, str]) -> List[Dict[str, Any]]:
        """Return dropdown/options metadata for a given attribute."""

        encoded_code = quote(str(attribute_code), safe="")
        data = self.get(f"products/attributes/{encoded_code}/options")
        if isinstance(data, dict):
            return list(data.get("items", []) or [])
        return list(data or [])


client = MagentoClient()
