from pathlib import Path
from functools import lru_cache
from typing import Any, Dict, Iterable
from ruamel.yaml import YAML
from rapidfuzz import fuzz

yaml = YAML(typ="safe")  # safe loader вместо yaml.safe_load()

@lru_cache(maxsize=1)
def load_map() -> Dict[str, Any]:
    path = Path("data/attributes_map.yaml")
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f) or {}
        return data if isinstance(data, dict) else {}

def _iter_variants(x: Any) -> Iterable[str]:
    if x is None:
        return []
    if isinstance(x, (list, tuple, set)):
        return [str(v) for v in x if v is not None]
    return [str(x)]

def normalize_value(attr_code: str, raw_value: Any) -> Any:
    # если нет карты — вернём исходное
    data = load_map()
    if attr_code not in data:
        return raw_value

    text = "" if raw_value is None else str(raw_value)
    options = data.get(attr_code) or {}

    for canonical, variants in options.items():
        for v in _iter_variants(variants):
            try:
                if fuzz.ratio(text.lower(), v.lower()) > 85:
                    return canonical
            except Exception:
                continue
    return raw_value
