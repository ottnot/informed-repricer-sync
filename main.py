"""
Dynamically calculate InformedRepricer floor prices using the Keepa API.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import urllib3

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Local Windows SSL / corporate proxy environments often fail certificate verification.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INPUT_CSV = Path("inventory_config.csv")
OUTPUT_CSV = Path("informed_sync.csv")
KEEPA_PRODUCT_URL = "https://api.keepa.com/product"
KEEPA_DOMAIN = 1  # Amazon.com
OFFERS_COUNT = 60  # larger New-offer buffer from Keepa
NEW_CONDITION = 1
SALES_RANK_CSV_INDEX = 3  # Keepa csv / stats.current index for sales rank

OUR_SELLER_ID = "A5QM7KD57PIMQ"
AMAZON_SELLER_ID = "ATVPDKIKX0DER"
SALES_RANK_LIMIT = 200000

MIN_STOCK = 3  # valid competitor must have stock >= 3
MAX_SHIPS_IN_DAYS = 7
UPPER_BOUND_RATIO = 0.92
FBM_PREMIUM = 0.30  # FBM: TARGET = landed + 0.30
FBA_UNDERCUT = 0.10  # FBA (non-Amazon): TARGET = landed - 0.10
AMAZON_UNDERCUT = 0.30  # Amazon: TARGET / floor cap = landed - 0.30

# Availability message fragments that indicate backorder / delayed dispatch
DELAYED_AVAILABILITY_PHRASES = (
    "ships in 1 to 2 weeks",
    "backordered",
    "usually ships within 1 to",
)

# Rate limiting / retry
MAX_RETRIES = 8
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 120.0
MIN_TOKENS_REQUIRED = 5
REQUEST_TIMEOUT = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("repricer")


# ---------------------------------------------------------------------------
# Keepa helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompetitorOffer:
    landed_price: float
    is_fba: bool
    seller_id: str = ""


def get_seller_id(offer: dict[str, Any]) -> str:
    """Normalized seller ID (stripped, uppercased) for case-insensitive matching."""
    return str(offer.get("sellerId") or offer.get("seller_id") or "").strip().upper()


def is_our_seller(seller_id: str) -> bool:
    return str(seller_id).strip().upper() == OUR_SELLER_ID.strip().upper()


def is_amazon_seller(seller_id: str) -> bool:
    return str(seller_id).strip().upper() == AMAZON_SELLER_ID.strip().upper()


def get_live_indices(product: dict[str, Any]) -> Optional[set[int]]:
    live_order = product.get("liveOffersOrder")
    return set(live_order) if live_order else None


def get_offer_stock(offer: dict[str, Any]) -> Optional[int]:
    """Return current stock from stockCSV (last value) or the stock property."""
    stock_csv = offer.get("stockCSV")
    if isinstance(stock_csv, list) and len(stock_csv) >= 1:
        try:
            return int(stock_csv[-1])
        except (TypeError, ValueError):
            pass

    if "stock" in offer and offer["stock"] is not None:
        try:
            return int(offer["stock"])
        except (TypeError, ValueError):
            pass

    return None


def _coerce_cents(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_offer_price_cents(offer: dict[str, Any]) -> Optional[int]:
    """Latest offer price in cents from offerCSV: [time, price, shipping, ...]."""
    offer_csv = offer.get("offerCSV")
    if not isinstance(offer_csv, list) or len(offer_csv) < 2:
        return None

    price = _coerce_cents(offer_csv[-2])
    if price is None or price < 0:
        return None
    return price


def get_offer_shipping_cents(offer: dict[str, Any]) -> int:
    """
    Shipping in cents from shipping / shippingCost fields, else offerCSV[-1].
    Missing or negative shipping is treated as 0.
    """
    for key in ("shipping", "shippingCost"):
        if key in offer and offer[key] is not None:
            shipping = _coerce_cents(offer[key])
            if shipping is None or shipping < 0:
                return 0
            return shipping

    offer_csv = offer.get("offerCSV")
    if isinstance(offer_csv, list) and len(offer_csv) >= 1:
        shipping = _coerce_cents(offer_csv[-1])
        if shipping is None or shipping < 0:
            return 0
        return shipping

    return 0


def get_landed_price_dollars(offer: dict[str, Any]) -> Optional[float]:
    """Landed Price = (Offer Price + Shipping Cost) / 100.0."""
    price_cents = get_offer_price_cents(offer)
    if price_cents is None:
        return None
    return (price_cents + get_offer_shipping_cents(offer)) / 100.0


def is_fba_offer(offer: dict[str, Any]) -> bool:
    return bool(offer.get("isFBA")) or bool(offer.get("isPrime"))


def _offer_availability_text(offer: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "availability",
        "availabilityMessage",
        "availabilityComment",
        "conditionComment",
        "shippingTime",
        "shippingInfo",
        "offerComment",
    ):
        value = offer.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " ".join(parts).lower()


def is_slow_shipping(offer: dict[str, Any]) -> bool:
    """
    True if shipping is slow: isBackordered, isPreorder, shipsInDays > 7,
    or availability text indicates delayed/backorder shipping.
    """
    if offer.get("isBackordered") is True:
        return True
    if offer.get("isPreorder") is True:
        return True

    ships_in_days = offer.get("shipsInDays")
    if ships_in_days is not None:
        try:
            if int(ships_in_days) > MAX_SHIPS_IN_DAYS:
                return True
        except (TypeError, ValueError):
            pass

    availability_text = _offer_availability_text(offer)
    if availability_text:
        for phrase in DELAYED_AVAILABILITY_PHRASES:
            if phrase in availability_text:
                return True

    return False


def is_new_condition(offer: dict[str, Any]) -> bool:
    """True for Keepa New offers (condition == 1 or an explicit new-condition flag)."""
    condition = offer.get("condition")
    if condition == NEW_CONDITION or condition == "1":
        return True
    if offer.get("isNew") is True or offer.get("new") is True:
        return True
    if isinstance(condition, str) and condition.strip().lower() == "new":
        return True
    return False


def is_active_new_offer(
    offer: dict[str, Any],
    live_indices: Optional[set[int]],
    index: int,
) -> bool:
    """Isolate active New-condition offers so the offers=60 buffer is used for New listings."""
    if not is_new_condition(offer):
        return False
    if live_indices is not None and index not in live_indices:
        return False
    return True


def get_sales_rank(product: dict[str, Any]) -> Optional[int]:
    """
    Current sales rank from stats.current[3] or csv[3] (last value).
    Returns None if unavailable / -1 (no rank).
    """
    stats = product.get("stats") or {}
    current = stats.get("current")
    if isinstance(current, list) and len(current) > SALES_RANK_CSV_INDEX:
        try:
            rank = int(current[SALES_RANK_CSV_INDEX])
            if rank > 0:
                return rank
        except (TypeError, ValueError):
            pass

    csv_data = product.get("csv")
    if isinstance(csv_data, list) and len(csv_data) > SALES_RANK_CSV_INDEX:
        series = csv_data[SALES_RANK_CSV_INDEX]
        if isinstance(series, list) and len(series) >= 1:
            try:
                rank = int(series[-1])
                if rank > 0:
                    return rank
            except (TypeError, ValueError):
                pass

    return None


def iter_active_new_offers(
    product: dict[str, Any],
) -> list[tuple[int, dict[str, Any]]]:
    """Active New-condition offers as (index, offer) pairs."""
    offers = product.get("offers") or []
    live_indices = get_live_indices(product)
    return [
        (idx, offer)
        for idx, offer in enumerate(offers)
        if is_active_new_offer(offer, live_indices, idx)
    ]


def our_seller_is_present(product: dict[str, Any]) -> bool:
    """
    Case-insensitive check for OUR_SELLER_ID across returned New-condition offers.
    Sets our_seller_found when matched.
    """
    our_seller_found = False

    # Prefer active New offers; also scan all New offers so we don't miss our listing
    # when liveOffersOrder is incomplete relative to the offers=60 buffer.
    for offer in product.get("offers") or []:
        if not is_new_condition(offer):
            continue
        if is_our_seller(get_seller_id(offer)):
            our_seller_found = True
            break

    return our_seller_found


def other_new_competitors(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Active New offers excluding OUR_SELLER_ID (case-insensitive)."""
    return [
        offer
        for _, offer in iter_active_new_offers(product)
        if not is_our_seller(get_seller_id(offer))
    ]


def should_skip_product(product: dict[str, Any], sku: str, asin: str) -> Optional[str]:
    """
    Return a skip reason string if the SKU should not be processed/output, else None.
    """
    rank = get_sales_rank(product)
    if rank is not None and rank >= SALES_RANK_LIMIT:
        return f"sales rank {rank} >= {SALES_RANK_LIMIT}"

    if not our_seller_is_present(product):
        return f"OUR_SELLER_ID {OUR_SELLER_ID} not in active offers"

    others = other_new_competitors(product)
    if len(others) == 0:
        return "no other New competitors"

    other_seller_ids = {get_seller_id(o) for o in others}
    other_seller_ids.discard("")
    if other_seller_ids == {AMAZON_SELLER_ID.strip().upper()}:
        return "only other New competitor is Amazon"

    return None


def classify_valid_competitor(offer: dict[str, Any]) -> Optional[CompetitorOffer]:
    """
    Valid competitor if:
      - stock >= 3
      - shipping speed OK (not backordered/preorder/slow)
      - landed price available
    """
    stock = get_offer_stock(offer)
    if stock is None or stock < MIN_STOCK:
        return None

    if is_slow_shipping(offer):
        return None

    landed = get_landed_price_dollars(offer)
    if landed is None:
        return None

    return CompetitorOffer(
        landed_price=landed,
        is_fba=is_fba_offer(offer),
        seller_id=get_seller_id(offer),
    )


def collect_valid_competitors(other_offers: list[dict[str, Any]]) -> list[CompetitorOffer]:
    valid: list[CompetitorOffer] = []
    for offer in other_offers:
        competitor = classify_valid_competitor(offer)
        if competitor is not None:
            valid.append(competitor)
    return valid


def find_amazon_landed_anywhere(product: dict[str, Any]) -> Optional[float]:
    """
    Amazon landed price if Amazon appears anywhere on active New offers
    (valid or not). Prefers lowest available landed price.
    """
    prices: list[float] = []
    for _, offer in iter_active_new_offers(product):
        if not is_amazon_seller(get_seller_id(offer)):
            continue
        landed = get_landed_price_dollars(offer)
        if landed is not None:
            prices.append(landed)

    # Fallback: any New Amazon offer if live filter excluded them
    if not prices:
        for offer in product.get("offers") or []:
            if is_new_condition(offer) and is_amazon_seller(get_seller_id(offer)):
                landed = get_landed_price_dollars(offer)
                if landed is not None:
                    prices.append(landed)

    if not prices:
        return None
    return min(prices)


def compute_target_price(
    valid_competitors: list[CompetitorOffer],
    other_new_count: int,
    abs_max: float,
) -> tuple[float, str]:
    """
    Scenario A — valid competitors exist: price off lowest landed.
      FBM:           landed + 0.30
      FBA non-Amazon: landed - 0.10
      Amazon:         landed - 0.30
    Scenario B — no valid competitors, but 1+ other New sellers:
      TARGET = ABS_MAX * 0.92
    """
    if valid_competitors:
        lowest = min(valid_competitors, key=lambda c: c.landed_price)

        if is_amazon_seller(lowest.seller_id):
            target = round(lowest.landed_price - AMAZON_UNDERCUT, 2)
            return (
                target,
                f"Scenario A Amazon (${lowest.landed_price:.2f} - ${AMAZON_UNDERCUT:.2f})",
            )

        if lowest.is_fba:
            target = round(lowest.landed_price - FBA_UNDERCUT, 2)
            return (
                target,
                f"Scenario A FBA (${lowest.landed_price:.2f} - ${FBA_UNDERCUT:.2f})",
            )

        target = round(lowest.landed_price + FBM_PREMIUM, 2)
        return (
            target,
            f"Scenario A FBM (${lowest.landed_price:.2f} + ${FBM_PREMIUM:.2f})",
        )

    if other_new_count >= 1:
        target = round(abs_max * UPPER_BOUND_RATIO, 2)
        return target, f"Scenario B (no valid comps → ABS_MAX * {UPPER_BOUND_RATIO})"

    # Should be unreachable when skip checks run first
    target = round(abs_max * UPPER_BOUND_RATIO, 2)
    return target, "Scenario B fallback"


def apply_amazon_floor_cap(target: float, amazon_landed: Optional[float]) -> float:
    """If Amazon is present, TARGET must be <= Amazon Landed - 0.30."""
    if amazon_landed is None:
        return target
    cap = round(amazon_landed - AMAZON_UNDERCUT, 2)
    if target > cap:
        return cap
    return target


def finalize_new_min(target: float, abs_min: float) -> float:
    """NEW_MIN = max(TARGET_PRICE, ABS_MIN); MIN_PRICE can never be below ABS_MIN."""
    return round(max(target, abs_min), 2)


# ---------------------------------------------------------------------------
# Keepa API client with token-bucket awareness + exponential backoff
# ---------------------------------------------------------------------------

class KeepaClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = requests.Session()
        self.session.verify = False
        self.tokens_left: Optional[int] = None
        self.refill_in_ms: int = 0
        self.refill_rate: int = 5

    def _wait_for_tokens(self) -> None:
        if self.tokens_left is None:
            return
        if self.tokens_left >= MIN_TOKENS_REQUIRED:
            return

        needed = MIN_TOKENS_REQUIRED - self.tokens_left
        rate = max(self.refill_rate, 1)
        wait_ms = self.refill_in_ms + (needed / rate) * 60_000
        wait_s = min(wait_ms / 1000.0, MAX_BACKOFF_SECONDS)
        if wait_s > 0:
            log.info(
                "Token bucket low (%s left). Waiting %.1fs for refill...",
                self.tokens_left,
                wait_s,
            )
            time.sleep(wait_s)

    def _update_token_state(self, payload: dict[str, Any]) -> None:
        if "tokensLeft" in payload:
            self.tokens_left = int(payload["tokensLeft"])
        if "refillIn" in payload:
            self.refill_in_ms = int(payload["refillIn"])
        if "refillRate" in payload:
            self.refill_rate = int(payload["refillRate"])

    def fetch_product(self, asin: str) -> Optional[dict[str, Any]]:
        """Fetch a single product; retries with exponential backoff on rate limits/errors."""
        params = {
            "key": self.api_key,
            "domain": KEEPA_DOMAIN,
            "asin": asin,
            "offers": OFFERS_COUNT,
            "stock": 1,
            "stats": 1,
        }

        for attempt in range(MAX_RETRIES):
            self._wait_for_tokens()

            try:
                response = self.session.get(
                    KEEPA_PRODUCT_URL,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    verify=False,
                )
            except requests.RequestException as exc:
                backoff = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
                log.warning(
                    "Request error for %s (attempt %s/%s): %s. Backing off %.1fs",
                    asin,
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if response.status_code == 429:
                backoff = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        backoff = max(backoff, float(retry_after))
                    except ValueError:
                        pass
                log.warning(
                    "HTTP 429 for %s. Backing off %.1fs (attempt %s/%s)",
                    asin,
                    backoff,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(backoff)
                continue

            if response.status_code != 200:
                backoff = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
                log.warning(
                    "HTTP %s for %s. Backing off %.1fs (attempt %s/%s)",
                    response.status_code,
                    asin,
                    backoff,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(backoff)
                continue

            try:
                payload = response.json()
            except ValueError:
                backoff = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
                log.warning("Invalid JSON for %s. Backing off %.1fs", asin, backoff)
                time.sleep(backoff)
                continue

            self._update_token_state(payload)

            error = payload.get("error")
            tokens_left = payload.get("tokensLeft")
            if error or (
                tokens_left is not None
                and tokens_left <= 0
                and not payload.get("products")
            ):
                backoff = min(BASE_BACKOFF_SECONDS * (2**attempt), MAX_BACKOFF_SECONDS)
                refill_s = (payload.get("refillIn") or 0) / 1000.0
                wait = max(backoff, refill_s)
                log.warning(
                    "Keepa rate/token limit for %s: %s. Waiting %.1fs (attempt %s/%s)",
                    asin,
                    error or "tokens exhausted",
                    wait,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            products = payload.get("products") or []
            if not products:
                log.error("No product data returned for ASIN %s", asin)
                return None

            return products[0]

        log.error("Exhausted retries fetching ASIN %s", asin)
        return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def load_inventory(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        required = {"SKU", "ASIN", "MARKETPLACE_ID", "ABS_MIN", "ABS_MAX"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(
                f"inventory_config.csv must have columns: {', '.join(sorted(required))}"
            )
        rows = list(reader)

    if not rows:
        raise ValueError("inventory_config.csv has no data rows")
    return rows


def process_row(row: dict[str, str], client: KeepaClient) -> Optional[dict[str, Any]]:
    """
    Process one inventory row. Returns an output dict, or None to skip the SKU.
    """
    sku = row["SKU"].strip()
    asin = row["ASIN"].strip()
    marketplace_id = row["MARKETPLACE_ID"].strip()
    abs_min = float(row["ABS_MIN"])
    abs_max = float(row["ABS_MAX"])

    product = client.fetch_product(asin)
    if product is None:
        log.warning("SKIP %s (%s): no Keepa data", sku, asin)
        return None

    skip_reason = should_skip_product(product, sku, asin)
    if skip_reason:
        log.info("SKIP %s (%s): %s", sku, asin, skip_reason)
        return None

    others = other_new_competitors(product)
    valid = collect_valid_competitors(others)
    amazon_landed = find_amazon_landed_anywhere(product)

    target, rule = compute_target_price(valid, len(others), abs_max)
    capped = apply_amazon_floor_cap(target, amazon_landed)
    if amazon_landed is not None and capped < target:
        log.info(
            "%s: Amazon floor cap applied (Amazon @ $%.2f → max TARGET $%.2f)",
            sku,
            amazon_landed,
            capped,
        )
        target = capped
        rule = f"{rule} + Amazon floor cap"

    new_min = finalize_new_min(target, abs_min)
    log.info(
        "%s: %s → TARGET=%.2f ABS_MIN=%.2f ABS_MAX=%.2f → MIN_PRICE=%.2f "
        "(valid_comps=%s other_new=%s)",
        sku,
        rule,
        target,
        abs_min,
        abs_max,
        new_min,
        len(valid),
        len(others),
    )

    return {
        "SKU": sku,
        "MARKETPLACE_ID": marketplace_id,
        "MIN_PRICE": f"{round(new_min, 2):.2f}",
        "MAX_PRICE": f"{abs_max:.2f}",
    }


def write_output(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["SKU", "MARKETPLACE_ID", "MIN_PRICE", "MAX_PRICE"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %s (%s rows)", path, len(rows))


def main() -> int:
    api_key = os.environ.get("KEEPA_API_KEY")
    if not api_key:
        log.error("KEEPA_API_KEY is not set. Add it to your environment or .env file.")
        return 1

    try:
        inventory = load_inventory(INPUT_CSV)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1

    client = KeepaClient(api_key)
    output_rows: list[dict[str, Any]] = []

    for i, row in enumerate(inventory, start=1):
        log.info("Processing %s/%s: %s", i, len(inventory), row.get("SKU", "?"))
        try:
            result = process_row(row, client)
            if result is not None:
                output_rows.append(result)
        except Exception as exc:  # noqa: BLE001 — continue batch; skip failed SKU
            log.exception("SKIP %s due to unexpected error: %s", row.get("SKU"), exc)

    write_output(OUTPUT_CSV, output_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
