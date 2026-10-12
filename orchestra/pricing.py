"""Turning token counts into money.

Your own price file wins: prices differ by contract. Without one, Cuelight uses Anthropic's public
list prices (orchestra.listprices), labelled with the date they were checked wherever they are
shown; ORCHESTRA_PRICES=off shows tokens only.

File format (JSON), prices per MILLION tokens, first matching pattern wins:

    {"currency": "USD",
     "models": {
       "claude-sonnet-*": {"input": 3.0, "output": 15.0,
                           "cache_read": 0.3, "cache_create": 3.75},
       "*":               {"input": 3.0, "output": 15.0}}}

`input` and `output` are required. `cache_read` / `cache_create` default to the
`input` price, which can overstate but never understates.
"""

import fnmatch
import json
import os
from typing import Any, Dict, List, Optional, Tuple

REQUIRED = ("input", "output")
OPTIONAL = ("cache_read", "cache_create")


def default_prices_path() -> str:
    """ORCHESTRA_PRICES, else the per-user config dir. Never under ~/.claude."""
    override = os.environ.get("ORCHESTRA_PRICES")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return _keep_legacy(os.path.join(base, "cuelight", "prices.json"),
                        os.path.join(base, "workflow", "prices.json"))


def _keep_legacy(path: str, legacy: str) -> str:
    """The product was called "Workflow" before 0.4.0. A file already saved under the
    old directory keeps working until the person moves it; a new install uses the new one."""
    if not os.path.exists(path) and os.path.exists(legacy):
        return legacy
    return path


class PriceError(ValueError):
    """The price file exists but cannot be used."""


class PriceTable:
    def __init__(self, models: List[Tuple[str, Dict[str, float]]],
                 currency: str = "USD", source: str = "yours", as_of: str = "") -> None:
        self.models = models
        self.currency = currency
        # Where the prices came from: "yours" (your price file) or "list" (orchestra.listprices,
        # Anthropic's public prices on the date in as_of). Shown next to every figure it prices.
        self.source = source
        self.as_of = as_of

    @staticmethod
    def parse(raw: Any) -> "PriceTable":
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
            raise PriceError('expected an object with a "models" object')
        currency = raw.get("currency", "USD")
        if not isinstance(currency, str) or not currency.strip():
            raise PriceError('"currency" must be a non-empty string')
        models: List[Tuple[str, Dict[str, float]]] = []
        for pattern, prices in raw["models"].items():
            if not isinstance(prices, dict):
                raise PriceError("{}: expected an object of prices".format(pattern))
            clean: Dict[str, float] = {}
            for key in REQUIRED + OPTIONAL:
                if key not in prices:
                    continue
                value = prices[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or value < 0 or value != value:
                    raise PriceError("{}: {} must be a number >= 0".format(pattern, key))
                clean[key] = float(value)
            for key in REQUIRED:
                if key not in clean:
                    raise PriceError("{}: missing required price {}".format(pattern, key))
            clean.setdefault("cache_read", clean["input"])
            clean.setdefault("cache_create", clean["input"])
            models.append((str(pattern).lower(), clean))
        return PriceTable(models, currency.strip())

    def price_for(self, model: str) -> Optional[Dict[str, float]]:
        name = (model or "").lower()
        for pattern, prices in self.models:
            if fnmatch.fnmatchcase(name, pattern):
                return prices
        return None

    def cost(self, tokens_by_model: Dict[str, Dict[str, int]]
             ) -> Tuple[float, List[str]]:
        """(cost, models that had no price). Unpriced usage adds nothing."""
        total = 0.0
        unpriced: List[str] = []
        for model, tokens in tokens_by_model.items():
            prices = self.price_for(model)
            if prices is None:
                if any(tokens.values()):
                    unpriced.append(model or "(unknown model)")
                continue
            for kind, count in tokens.items():
                total += count * prices.get(kind, 0.0) / 1_000_000.0
        return total, sorted(set(unpriced))


class PriceSource:
    """The price file, re-read whenever it changes on disk.

    Never raises: a missing file means the fallback prices (the list prices) or, with none,
    "cost not enabled"; a broken one is reported in `error` so the dashboard can say so
    instead of showing nothing.
    """

    def __init__(self, path: Optional[str] = None,
                 fallback: Optional[PriceTable] = None) -> None:
        # ORCHESTRA_PRICES=off: no cost at all, not even the built-in list prices.
        self.off = path is None and os.environ.get("ORCHESTRA_PRICES", "").strip().lower() == "off"
        self.path = path or default_prices_path()
        self.fallback = fallback
        self._stamp: Optional[Tuple[int, int]] = None
        self._table: Optional[PriceTable] = None
        self.error: str = ""

    def get(self) -> Optional[PriceTable]:
        if self.off:
            return None
        try:
            info = os.stat(self.path)
        except OSError:
            self._stamp, self._table, self.error = None, None, ""
            return self.fallback
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._table = PriceTable.parse(json.load(fh))
                self.error = ""
            except (OSError, ValueError) as exc:
                self._table = None
                self.error = "prices file unusable: {}".format(exc)
        return self._table


def default_source() -> "PriceSource":
    """Your price file, falling back to the dated list prices (orchestra.listprices)."""
    from orchestra import listprices
    return PriceSource(fallback=listprices.table())
