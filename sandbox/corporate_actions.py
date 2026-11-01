# sandbox/corporate_actions.py
"""
Corporate actions for the sandbox (paper) account: stock splits and bonus issues.

When the paper account meets a split (拆股) or bonus issue (送股) it has no feed
for it, so the position book keeps stale terms:

- holdings/positions still show the old quantity and average cost,
- resting orders and active GTT triggers still fire at the old prices and size,
- today's mark-to-market is therefore wrong.

This module imports a corporate action *notice*, previews every affected
account/position with before/after quantity and cost, and on confirmation
converts the books. Design rules the UI and the tests rely on:

1. **Preview first.** Nothing is written until the notice is confirmed.
2. **Effective-date scoping.** A notice can only be applied on/after its
   effective date. The current book (what is held going forward) and resting
   orders (which execute going forward) are converted; historical trades and
   realised P&L dated before the effective date are never rewritten. Orders
   and GTTs created on/after the effective date are already expressed in the
   new terms and are left alone.
3. **Idempotent.** Each notice line is keyed by ``(notice_id, symbol,
   exchange)`` and written to ``sandbox_corporate_actions`` when applied.
   Re-importing the same notice replays the stored result and changes nothing.
4. **Money-conserving.** Quantity is multiplied by the action factor while
   prices/cost are divided by it; blocked margin is unchanged because the
   notional value is conserved, so funds are never touched (which is also what
   prevents the same action being counted twice).

The math. For a split ``a:b`` every ``a`` old shares become ``b`` new shares, so
``factor = b / a``. For a bonus ``a:b`` (``b`` free shares per ``a`` held) the
holder ends with ``a + b`` shares, so ``factor = (a + b) / a``. In both cases
``qty_new = qty_old * factor`` and ``price_new = price_old / factor``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import pytz
from sqlalchemy import func as db_func
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from database.sandbox_db import (
    SandboxCorporateAction,
    SandboxGTT,
    SandboxGTTLeg,
    SandboxHoldings,
    SandboxOrders,
    SandboxPositions,
    SandboxTrades,
    db_session,
    ist_now,
)
from utils.keyed_locks import LockBusy
from utils.logging import get_logger
from sandbox.position_locks import position_lock

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

ACTION_SPLIT = "split"
ACTION_BONUS = "bonus"
VALID_ACTION_TYPES = (ACTION_SPLIT, ACTION_BONUS)

#: Orders that can still be re-priced. Everything else already executed (or was
#: cancelled/rejected), so its prices are historical facts.
PENDING_ORDER_STATUSES = ("open", "trigger pending")

_QUANTITY_QUANTUM = Decimal("1")
_PRICE_QUANTUM = Decimal("0.01")


class CorporateActionError(ValueError):
    """A notice line cannot be parsed or applied (message is shown to the user)."""


class CorporateActionBusy(Exception):
    """A fill holds a position lock the conversion needs; the caller retries."""


@dataclass(frozen=True)
class CorporateAction:
    """One line of a corporate action notice."""

    notice_id: str
    symbol: str
    exchange: str
    action_type: str
    ratio_from: int
    ratio_to: int
    effective_date: date

    @property
    def factor(self) -> Decimal:
        """Quantity multiplier (prices are divided by the same number)."""
        if self.action_type == ACTION_SPLIT:
            return Decimal(self.ratio_to) / Decimal(self.ratio_from)
        # bonus: ratio_to free shares for every ratio_from held
        return Decimal(self.ratio_from + self.ratio_to) / Decimal(self.ratio_from)

    @property
    def ledger_key(self) -> tuple[str, str, str]:
        return self.notice_id, self.symbol, self.exchange


# --------------------------------------------------------------------------- #
# Notice parsing
# --------------------------------------------------------------------------- #

_ACTION_ALIASES = {
    "split": ACTION_SPLIT,
    "stock_split": ACTION_SPLIT,
    "stocksplit": ACTION_SPLIT,
    "subdivision": ACTION_SPLIT,
    "拆股": ACTION_SPLIT,
    "bonus": ACTION_BONUS,
    "bonus_issue": ACTION_BONUS,
    "bonus_share": ACTION_BONUS,
    "bonus_shares": ACTION_BONUS,
    "送股": ACTION_BONUS,
}

_HEADER_ALIASES = {
    "notice_id": ("notice_id", "id", "notice", "reference", "ref", "corpid"),
    "symbol": ("symbol", "trading_symbol", "scrip"),
    "exchange": ("exchange", "exch"),
    "action_type": ("action_type", "action", "type", "corporate_action", "ca_type"),
    "ratio_from": (
        "ratio_from",
        "from_shares",
        "old_shares",
        "from",
        "base_shares",
        "held",
    ),
    "ratio_to": ("ratio_to", "to_shares", "new_shares", "to", "result_shares"),
    "effective_date": (
        "effective_date",
        "date",
        "ex_date",
        "exdate",
        "record_date",
        "effective",
    ),
    "ratio": ("ratio", "factor_ratio"),
}


def _normalize_headers(fieldnames):
    """Map a file's (possibly aliased) headers onto the canonical field names."""
    lookup = {name.strip().lstrip("\ufeff").lower().replace(" ", "_"): name for name in fieldnames}
    mapped = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapped[canonical] = lookup[alias]
                break
    return mapped


def _derive_notice_id(symbol, exchange, action_type, ratio_from, ratio_to, effective_date) -> str:
    """Deterministic id for notices that do not carry one.

    The same file imported twice produces the same id, so even anonymous
    notices cannot be double-applied.
    """
    raw = "|".join(
        [
            symbol.upper(),
            exchange.upper(),
            action_type,
            str(ratio_from),
            str(ratio_to),
            effective_date.isoformat(),
        ]
    )
    return "CA-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()


def _parse_row(row, mapped, line_no):
    def cell(name):
        key = mapped.get(name)
        value = row.get(key) if key else None
        return (value or "").strip()

    symbol = cell("symbol").upper()
    exchange = cell("exchange").upper()
    if not symbol or not exchange:
        raise CorporateActionError(f"Row {line_no}: symbol and exchange are required")

    action_raw = cell("action_type").lower().replace("-", "_").replace(" ", "_")
    action_type = _ACTION_ALIASES.get(action_raw)
    if action_type is None:
        raise CorporateActionError(
            f"Row {line_no}: action_type must be 'split' or 'bonus' (got {cell('action_type')!r})"
        )

    ratio_from = ratio_to = None
    ratio_text = cell("ratio")
    if ratio_text and ":" in ratio_text:
        try:
            left, right = ratio_text.split(":", 1)
            ratio_from, ratio_to = int(left), int(right)
        except ValueError as exc:
            raise CorporateActionError(
                f"Row {line_no}: ratio must look like '1:5' (got {ratio_text!r})"
            ) from exc
    else:
        try:
            ratio_from = int(cell("ratio_from")) if cell("ratio_from") else None
            ratio_to = int(cell("ratio_to")) if cell("ratio_to") else None
        except ValueError as exc:
            raise CorporateActionError(
                f"Row {line_no}: ratio_from and ratio_to must be whole numbers"
            ) from exc

    if not ratio_from or not ratio_to or ratio_from <= 0 or ratio_to <= 0:
        raise CorporateActionError(
            f"Row {line_no}: positive ratio_from and ratio_to are required "
            "(e.g. split 1:5, bonus 1:1)"
        )

    date_text = cell("effective_date")
    try:
        effective_date = date.fromisoformat(date_text)
    except ValueError as exc:
        raise CorporateActionError(
            f"Row {line_no}: effective_date must be YYYY-MM-DD (got {date_text!r})"
        ) from exc

    notice_id = cell("notice_id") or _derive_notice_id(
        symbol, exchange, action_type, ratio_from, ratio_to, effective_date
    )

    return CorporateAction(
        notice_id=notice_id,
        symbol=symbol,
        exchange=exchange,
        action_type=action_type,
        ratio_from=ratio_from,
        ratio_to=ratio_to,
        effective_date=effective_date,
    )


def parse_notice_csv(raw_text):
    """Parse a notice CSV into a list of :class:`CorporateAction`.

    Expected header::

        notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date

    ``notice_id`` is optional (a deterministic id is derived); blank and
    ``#`` comment lines are skipped. A combined ``ratio`` column ("1:5") is
    accepted in place of the two ratio columns.
    """
    if not raw_text or not raw_text.strip():
        raise CorporateActionError("Notice file is empty")

    reader = csv.DictReader(io.StringIO(raw_text.strip()))
    if not reader.fieldnames:
        raise CorporateActionError("Notice file has no header row")
    mapped = _normalize_headers(reader.fieldnames)

    missing = [name for name in ("symbol", "exchange", "action_type", "effective_date") if name not in mapped]
    if missing:
        raise CorporateActionError(
            "Notice header is missing columns: " + ", ".join(sorted(missing))
        )
    if "ratio" not in mapped and ("ratio_from" not in mapped or "ratio_to" not in mapped):
        raise CorporateActionError(
            "Notice header must contain ratio_from and ratio_to (or a combined ratio column)"
        )

    actions = []
    seen_keys = set()
    for i, row in enumerate(reader, start=2):
        first_value = next((v for v in row.values() if v and str(v).strip()), None)
        if first_value is None or str(first_value).strip().startswith("#"):
            continue
        action = _parse_row(row, mapped, i)
        if action.ledger_key in seen_keys:
            raise CorporateActionError(
                f"Row {i}: duplicate notice line for {action.symbol} {action.exchange} "
                f"(notice_id {action.notice_id})"
            )
        seen_keys.add(action.ledger_key)
        actions.append(action)

    if not actions:
        raise CorporateActionError("Notice file contains no action rows")
    return actions


def parse_action_dicts(rows):
    """Build actions from plain dicts (the JSON ``actions`` request form)."""
    if not isinstance(rows, list) or not rows:
        raise CorporateActionError("actions must be a non-empty list")
    pseudo = io.StringIO()
    writer = csv.DictWriter(
        pseudo,
        fieldnames=[
            "notice_id",
            "symbol",
            "exchange",
            "action_type",
            "ratio_from",
            "ratio_to",
            "effective_date",
        ],
    )
    writer.writeheader()
    for row in rows:
        if not isinstance(row, dict):
            raise CorporateActionError("each action must be an object")
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in writer.fieldnames})
    return parse_notice_csv(pseudo.getvalue())


# --------------------------------------------------------------------------- #
# Conversion math
# --------------------------------------------------------------------------- #


def scale_quantity(quantity, factor):
    """Multiply a (signed) share quantity by the action factor.

    Returns ``(new_quantity, rounded)``; ``rounded`` flags a fractional
    entitlement that had to be rounded to a whole share (odd-lot bonus).
    """
    qty = int(quantity)
    exact = Decimal(abs(qty)) * Decimal(factor)
    scaled = int(exact.quantize(_QUANTITY_QUANTUM, rounding=ROUND_HALF_UP))
    return (scaled if qty >= 0 else -scaled), scaled != exact


def scale_price(price, factor):
    """Divide a price/cost by the action factor, rounded to the tick precision."""
    if price is None:
        return None
    value = Decimal(str(price)) / Decimal(factor)
    return value.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)


def cost_conserving_average(qty_before, avg_before, qty_after):
    """Average after the action, keeping total cost identical.

    ``avg_new = (|qty_old| * avg_old) / |qty_new|`` — the company action never
    creates or destroys cost by itself.
    """
    if not qty_after:
        return Decimal(str(avg_before)).quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    total_cost = Decimal(abs(int(qty_before))) * Decimal(str(avg_before))
    return (total_cost / Decimal(abs(int(qty_after)))).quantize(
        _PRICE_QUANTUM, rounding=ROUND_HALF_UP
    )


def _replay_position_trades(trades, action, convert_pre_ex):
    """Replay fills with the same average-price rules as the execution engine.

    When ``convert_pre_ex`` is set, fills dated before the effective date are
    expressed in post-action units; later fills are left as recorded. This
    rebuilds the correct quantity/average when the notice is imported after the
    account already traded the stock in new terms.
    """
    factor = action.factor
    effective = action.effective_date
    qty = 0
    avg = None

    for trade in trades:
        tqty = int(trade.quantity)
        price = Decimal(str(trade.price))
        ts = trade.trade_timestamp
        if convert_pre_ex and ts is not None and ts.date() < effective:
            tqty, _ = scale_quantity(tqty, factor)
            price = scale_price(price, factor)

        new = tqty if trade.action == "BUY" else -tqty
        old = qty
        final = old + new

        if old == 0:
            qty, avg = new, price
        elif final == 0:
            qty = 0
        elif (old > 0 and final > old) or (old < 0 and final < old):
            avg = (abs(old) * avg + abs(new) * price) / (abs(old) + abs(new))
            qty = final
        elif abs(new) > abs(old):
            # Reversal through zero: excess opens a fresh position at this price
            remaining = abs(new) - abs(old)
            qty = remaining if trade.action == "BUY" else -remaining
            avg = price
        else:
            qty = final  # reduction: average price is unchanged

    if avg is not None:
        avg = avg.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    return qty, avg


def _replay_holdings_trades(trades, action, convert_pre_ex):
    """Replay CNC fills as the holdings ledger: adds re-average, sells reduce."""
    factor = action.factor
    effective = action.effective_date
    qty = 0
    avg = None

    for trade in trades:
        tqty = int(trade.quantity)
        price = Decimal(str(trade.price))
        ts = trade.trade_timestamp
        if convert_pre_ex and ts is not None and ts.date() < effective:
            tqty, _ = scale_quantity(tqty, factor)
            price = scale_price(price, factor)

        if trade.action == "BUY":
            if avg is None or qty <= 0:
                avg = price
            else:
                avg = (abs(qty) * avg + tqty * price) / (abs(qty) + tqty)
            qty += tqty
        else:
            qty -= tqty
            if qty == 0:
                avg = None

    if avg is not None:
        avg = avg.quantize(_PRICE_QUANTUM, rounding=ROUND_HALF_UP)
    return qty, avg


def _contract_value(symbol, exchange):
    """Multiplier for P&L math (1 for equities; some instruments carry a value)."""
    try:
        from database.token_db import get_symbol_info

        info = get_symbol_info(symbol, exchange)
        if info is not None and getattr(info, "contract_value", None):
            return Decimal(str(info.contract_value))
    except Exception:
        pass
    return Decimal("1")


def _position_pnl_after(quantity, avg_after, ltp, contract_value):
    """Recompute the position's unrealised P&L from its stored LTP, if known."""
    if ltp is None or avg_after is None or avg_after <= 0:
        return None, None
    ltp = Decimal(str(ltp))
    if quantity > 0:
        pnl = (ltp - avg_after) * quantity * contract_value
        pct = (ltp - avg_after) / avg_after * Decimal("100")
    else:
        pnl = (avg_after - ltp) * abs(quantity) * contract_value
        pct = (avg_after - ltp) / avg_after * Decimal("100")
    return pnl.quantize(_PRICE_QUANTUM, ROUND_HALF_UP), pct.quantize(
        Decimal("0.0001"), ROUND_HALF_UP
    )


def _holding_pnl_after(quantity, avg_after, ltp):
    if ltp is None or avg_after is None or avg_after <= 0:
        return None, None
    ltp = Decimal(str(ltp))
    pnl = (ltp - avg_after) * abs(quantity)
    pct = (ltp - avg_after) / avg_after * Decimal("100")
    return pnl.quantize(_PRICE_QUANTUM, ROUND_HALF_UP), pct.quantize(
        Decimal("0.0001"), ROUND_HALF_UP
    )


# --------------------------------------------------------------------------- #
# Planning (shared by preview and apply)
# --------------------------------------------------------------------------- #


def _trades_for(user_id, action, product=None):
    query = SandboxTrades.query.filter_by(
        user_id=user_id, symbol=action.symbol, exchange=action.exchange
    )
    if product is not None:
        query = query.filter_by(product=product)
    return query.order_by(
        SandboxTrades.trade_timestamp.asc(), SandboxTrades.id.asc()
    ).all()


def _plan_position(pos, action):
    """Before/after terms for one open position row."""
    factor = action.factor
    trades = _trades_for(pos.user_id, action, pos.product)
    post_ex = any(
        t.trade_timestamp is not None and t.trade_timestamp.date() >= action.effective_date
        for t in trades
    )

    qty_after = avg_after = rounded = None
    if post_ex and trades:
        plain_qty, _ = _replay_position_trades(trades, action, convert_pre_ex=False)
        if plain_qty == pos.quantity:
            qty_after, avg_after = _replay_position_trades(trades, action, convert_pre_ex=True)
            rounded = False

    if qty_after is None:
        qty_after, rounded = scale_quantity(pos.quantity, factor)
        avg_after = cost_conserving_average(pos.quantity, pos.average_price, qty_after)

    cv = _contract_value(pos.symbol, pos.exchange)
    pnl_after, pct_after = _position_pnl_after(qty_after, avg_after, pos.ltp, cv)

    return {
        "kind": "position",
        "user_id": pos.user_id,
        "product": pos.product,
        "quantity_before": int(pos.quantity),
        "quantity_after": int(qty_after),
        "average_price_before": float(pos.average_price),
        "average_price_after": float(avg_after),
        "rounded": bool(rounded),
        "pnl_after": float(pnl_after) if pnl_after is not None else None,
        "pnl_percent_after": float(pct_after) if pct_after is not None else None,
        "_row": pos,
    }


def _plan_holding(holding, action):
    factor = action.factor
    trades = [t for t in _trades_for(holding.user_id, action) if t.product == "CNC"]
    post_ex = any(
        t.trade_timestamp is not None and t.trade_timestamp.date() >= action.effective_date
        for t in trades
    )

    qty_after = avg_after = None
    if post_ex and trades:
        plain_qty, _ = _replay_holdings_trades(trades, action, convert_pre_ex=False)
        if plain_qty == holding.quantity:
            qty_after, avg_after = _replay_holdings_trades(trades, action, convert_pre_ex=True)

    rounded = False
    if qty_after is None:
        qty_after, rounded = scale_quantity(holding.quantity, factor)
        avg_after = cost_conserving_average(
            holding.quantity, holding.average_price, qty_after
        )

    pnl_after, pct_after = _holding_pnl_after(qty_after, avg_after, holding.ltp)

    return {
        "kind": "holding",
        "user_id": holding.user_id,
        "settlement_date": holding.settlement_date.isoformat()
        if holding.settlement_date
        else None,
        "quantity_before": int(holding.quantity),
        "quantity_after": int(qty_after),
        "average_price_before": float(holding.average_price),
        "average_price_after": float(avg_after),
        "rounded": bool(rounded),
        "pnl_after": float(pnl_after) if pnl_after is not None else None,
        "pnl_percent_after": float(pct_after) if pct_after is not None else None,
        "_row": holding,
    }


def _plan_order(order, action):
    factor = action.factor
    qty_after, rounded = scale_quantity(order.quantity, factor)
    price_after = scale_price(order.price, factor) if order.price is not None else None
    trigger_after = (
        scale_price(order.trigger_price, factor)
        if order.trigger_price is not None
        else None
    )
    return {
        "kind": "order",
        "user_id": order.user_id,
        "orderid": order.orderid,
        "action": order.action,
        "price_type": order.price_type,
        "quantity_before": int(order.quantity),
        "quantity_after": int(qty_after),
        "price_before": float(order.price) if order.price is not None else None,
        "price_after": float(price_after) if price_after is not None else None,
        "trigger_price_before": float(order.trigger_price)
        if order.trigger_price is not None
        else None,
        "trigger_price_after": float(trigger_after) if trigger_after is not None else None,
        "rounded": bool(rounded),
        "_row": order,
    }


def _plan_gtt(gtt, action):
    factor = action.factor
    last_before = Decimal(str(gtt.last_price))
    last_after = scale_price(last_before, factor)
    legs = []
    for leg in gtt.legs:
        if leg.leg_status != "pending":
            continue
        qty_after, rounded = scale_quantity(leg.quantity, factor)
        legs.append(
            {
                "leg_number": leg.leg_number,
                "action": leg.action,
                "pricetype": leg.pricetype,
                "quantity_before": int(leg.quantity),
                "quantity_after": int(qty_after),
                "price_before": float(leg.price),
                "price_after": float(scale_price(leg.price, factor)),
                "trigger_price_before": float(leg.trigger_price),
                "trigger_price_after": float(scale_price(leg.trigger_price, factor)),
                "rounded": bool(rounded),
                "_row": leg,
            }
        )
    return {
        "kind": "gtt",
        "user_id": gtt.user_id,
        "gtt_id": gtt.gtt_id,
        "trigger_type": gtt.trigger_type,
        "last_price_before": float(last_before),
        "last_price_after": float(last_after),
        "legs": legs,
        "_row": gtt,
    }


def _action_base_dict(action):
    return {
        "notice_id": action.notice_id,
        "symbol": action.symbol,
        "exchange": action.exchange,
        "action_type": action.action_type,
        "ratio_from": action.ratio_from,
        "ratio_to": action.ratio_to,
        "effective_date": action.effective_date.isoformat(),
        "factor": float(action.factor),
    }


def _collect_plans(action):
    """Read every affected row across every account and plan its conversion."""
    positions = (
        SandboxPositions.query.filter_by(symbol=action.symbol, exchange=action.exchange)
        .filter(SandboxPositions.quantity != 0)
        .all()
    )
    holdings = (
        SandboxHoldings.query.filter_by(symbol=action.symbol, exchange=action.exchange)
        .filter(SandboxHoldings.quantity != 0)
        .all()
    )

    orders = (
        SandboxOrders.query.filter_by(symbol=action.symbol, exchange=action.exchange)
        .filter(SandboxOrders.order_status.in_(PENDING_ORDER_STATUSES))
        .order_by(SandboxOrders.id.asc())
        .all()
    )
    gtts = (
        SandboxGTT.query.filter_by(
            symbol=action.symbol, exchange=action.exchange, gtt_status="active"
        )
        .order_by(SandboxGTT.id.asc())
        .all()
    )

    planned_positions = [_plan_position(p, action) for p in positions]
    planned_holdings = [_plan_holding(h, action) for h in holdings]

    planned_orders = []
    skipped = []
    for order in orders:
        placed = order.order_timestamp.date() if order.order_timestamp else None
        if placed is not None and placed >= action.effective_date:
            skipped.append(
                {
                    "kind": "order",
                    "id": order.orderid,
                    "user_id": order.user_id,
                    "reason": "created on/after effective date; already in new terms",
                }
            )
            continue
        if (order.filled_quantity or 0) != 0:
            skipped.append(
                {
                    "kind": "order",
                    "id": order.orderid,
                    "user_id": order.user_id,
                    "reason": "order already partially filled; convert manually",
                }
            )
            continue
        planned_orders.append(_plan_order(order, action))

    planned_gtts = []
    for gtt in gtts:
        created = gtt.created_at.date() if gtt.created_at else None
        if created is not None and created >= action.effective_date:
            skipped.append(
                {
                    "kind": "gtt",
                    "id": gtt.gtt_id,
                    "user_id": gtt.user_id,
                    "reason": "created on/after effective date; already in new terms",
                }
            )
            continue
        if any(leg.leg_status == "triggering" for leg in gtt.legs):
            skipped.append(
                {
                    "kind": "gtt",
                    "id": gtt.gtt_id,
                    "user_id": gtt.user_id,
                    "reason": "a GTT leg is firing right now; retry in a moment",
                }
            )
            continue
        planned_gtts.append(_plan_gtt(gtt, action))

    account_ids = {p["user_id"] for p in planned_positions}
    account_ids |= {h["user_id"] for h in planned_holdings}
    account_ids |= {o["user_id"] for o in planned_orders}
    account_ids |= {g["user_id"] for g in planned_gtts}

    return {
        "positions": planned_positions,
        "holdings": planned_holdings,
        "orders": planned_orders,
        "gtts": planned_gtts,
        "skipped": skipped,
        "account_ids": account_ids,
    }


def _strip_internal(plan_items):
    """Drop ORM row handles before a plan leaves this module (JSON/ledger)."""
    clean = []
    for item in plan_items:
        clean.append({k: v for k, v in item.items() if not k.startswith("_")})
    return clean


def _stored_result(action, ledger_row):
    """Rehydrate the preview dict stored when an earlier import was applied."""
    result = _action_base_dict(action)
    result.update(
        {
            "already_applied": True,
            "applied_at": ledger_row.applied_at.strftime("%Y-%m-%d %H:%M:%S")
            if ledger_row.applied_at
            else None,
            "applied_by": ledger_row.applied_by,
        }
    )
    try:
        payload = json.loads(ledger_row.result_json) if ledger_row.result_json else {}
    except (ValueError, TypeError):
        payload = {}
    result["counts"] = payload.get(
        "counts",
        {
            "accounts": ledger_row.affected_accounts,
            "positions": ledger_row.positions_adjusted,
            "holdings": ledger_row.holdings_adjusted,
            "orders": ledger_row.orders_adjusted,
            "gtts": ledger_row.gtts_adjusted,
        },
    )
    for key in ("positions", "holdings", "orders", "gtts", "skipped"):
        result[key] = payload.get(key, [])
    return result


def _get_ledger(action):
    return SandboxCorporateAction.query.filter_by(
        notice_id=action.notice_id, symbol=action.symbol, exchange=action.exchange
    ).first()


def preview_action(action):
    """Dry-run one action: every affected row with before/after terms.

    Never writes. If the notice was already applied, its stored result is
    returned with ``already_applied=True``.
    """
    ledger_row = _get_ledger(action)
    if ledger_row is not None:
        return _stored_result(action, ledger_row)

    plans = _collect_plans(action)
    result = _action_base_dict(action)
    result.update(
        {
            "already_applied": False,
            "applied_at": None,
            "applied_by": None,
            "counts": {
                "accounts": len(plans["account_ids"]),
                "positions": len(plans["positions"]),
                "holdings": len(plans["holdings"]),
                "orders": len(plans["orders"]),
                "gtts": len(plans["gtts"]),
            },
            "positions": _strip_internal(plans["positions"]),
            "holdings": _strip_internal(plans["holdings"]),
            "orders": _strip_internal(plans["orders"]),
            "gtts": _strip_internal(_clean_gtt_plans(plans["gtts"])),
            "skipped": plans["skipped"],
        }
    )
    return result


def _clean_gtt_plans(gtt_plans):
    clean = []
    for gtt in gtt_plans:
        entry = {k: v for k, v in gtt.items() if not k.startswith("_")}
        entry["legs"] = [
            {k: v for k, v in leg.items() if not k.startswith("_")} for leg in gtt["legs"]
        ]
        clean.append(entry)
    return clean


def preview_actions(actions):
    return [preview_action(a) for a in actions]


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


def _today_ist():
    return datetime.now(IST).date()


def ensure_apply_is_current(actions):
    """Reject applying a notice before its effective date.

    The book must keep trading in old terms until the action is in force;
    applying early would misprice every fill between now and the date.
    """
    today = _today_ist()
    future = [
        f"{a.symbol} {a.exchange} (effective {a.effective_date.isoformat()})"
        for a in actions
        if a.effective_date > today
    ]
    if future:
        raise CorporateActionError(
            "Notice is not in force yet; confirm it on or after the effective date: "
            + "; ".join(future)
        )


def _apply_one(action, applied_by, now):
    """Convert the books for one action line, under the positions' locks."""
    ledger_row = _get_ledger(action)
    if ledger_row is not None:
        return _stored_result(action, ledger_row), False

    plans = _collect_plans(action)

    # One lock per affected position, sorted for a consistent global order;
    # fills take the same locks, so a position cannot move while its terms are
    # rewritten. Holdings fold in at T+1 settlement, never on a fill path.
    lock_keys = sorted(
        {
            (p["user_id"], action.exchange, action.symbol, p["product"])
            for p in plans["positions"]
        }
    )
    try:
        with ExitStack() as stack:
            for user_id, exchange, symbol, product in lock_keys:
                stack.enter_context(position_lock(user_id, exchange, symbol, product))
            result = _write_conversions(action, plans, applied_by, now)
        return result, True
    except LockBusy as exc:
        raise CorporateActionBusy(
            f"A trade for {action.symbol} is still settling; retry in a moment."
        ) from exc


def _write_conversions(action, plans, applied_by, now):
    # --- positions ------------------------------------------------------- #
    for plan in plans["positions"]:
        row = plan["_row"]
        row.quantity = plan["quantity_after"]
        row.average_price = Decimal(str(plan["average_price_after"]))
        if plan["pnl_after"] is not None:
            row.pnl = Decimal(str(plan["pnl_after"]))
            row.pnl_percent = Decimal(str(plan["pnl_percent_after"]))
        db_session.flush()

    # --- holdings -------------------------------------------------------- #
    for plan in plans["holdings"]:
        row = plan["_row"]
        row.quantity = plan["quantity_after"]
        row.average_price = Decimal(str(plan["average_price_after"]))
        if plan["pnl_after"] is not None:
            row.pnl = Decimal(str(plan["pnl_after"]))
            row.pnl_percent = Decimal(str(plan["pnl_percent_after"]))
        db_session.flush()

    # --- resting orders (conditional: only while still unfilled/pending) - #
    applied_orders = []
    for plan in plans["orders"]:
        row = plan["_row"]
        values = {
            "quantity": plan["quantity_after"],
            "pending_quantity": plan["quantity_after"],
            "update_timestamp": now,
        }
        if row.price is not None:
            values["price"] = Decimal(str(plan["price_after"]))
        if row.trigger_price is not None:
            values["trigger_price"] = Decimal(str(plan["trigger_price_after"]))
        # COALESCE: legacy rows can carry NULL filled_quantity, which would
        # never match an `== 0` predicate and so would wrongly be skipped.
        result = db_session.execute(
            sa_update(SandboxOrders)
            .where(
                SandboxOrders.id == row.id,
                SandboxOrders.order_status.in_(PENDING_ORDER_STATUSES),
                db_func.coalesce(SandboxOrders.filled_quantity, 0) == 0,
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        )
        if result.rowcount == 1:
            applied_orders.append(plan)
        else:
            plans["skipped"].append(
                {
                    "kind": "order",
                    "id": row.orderid,
                    "user_id": row.user_id,
                    "reason": "order changed while the notice was being confirmed",
                }
            )

    # --- active GTTs (conditional: only while still active/pending) ------ #
    applied_gtts = []
    for plan in plans["gtts"]:
        gtt = plan["_row"]
        parent_result = db_session.execute(
            sa_update(SandboxGTT)
            .where(SandboxGTT.id == gtt.id, SandboxGTT.gtt_status == "active")
            .values(last_price=Decimal(str(plan["last_price_after"])), updated_at=now),
            execution_options={"synchronize_session": False},
        )
        if parent_result.rowcount != 1:
            plans["skipped"].append(
                {
                    "kind": "gtt",
                    "id": gtt.gtt_id,
                    "user_id": gtt.user_id,
                    "reason": "GTT changed while the notice was being confirmed",
                }
            )
            continue

        all_legs_ok = True
        for leg_plan in plan["legs"]:
            leg = leg_plan["_row"]
            leg_result = db_session.execute(
                sa_update(SandboxGTTLeg)
                .where(SandboxGTTLeg.id == leg.id, SandboxGTTLeg.leg_status == "pending")
                .values(
                    quantity=leg_plan["quantity_after"],
                    price=Decimal(str(leg_plan["price_after"])),
                    trigger_price=Decimal(str(leg_plan["trigger_price_after"])),
                    updated_at=now,
                ),
                execution_options={"synchronize_session": False},
            )
            if leg_result.rowcount != 1:
                all_legs_ok = False
                plans["skipped"].append(
                    {
                        "kind": "gtt-leg",
                        "id": f"{gtt.gtt_id} leg {leg.leg_number}",
                        "user_id": gtt.user_id,
                        "reason": "GTT leg changed while the notice was being confirmed",
                    }
                )
        if all_legs_ok:
            applied_gtts.append(plan)

    positions_clean = _strip_internal(plans["positions"])
    holdings_clean = _strip_internal(plans["holdings"])
    orders_clean = _strip_internal(applied_orders)
    gtts_clean = _clean_gtt_plans(applied_gtts)

    counts = {
        "accounts": len(plans["account_ids"]),
        "positions": len(positions_clean),
        "holdings": len(holdings_clean),
        "orders": len(orders_clean),
        "gtts": len(gtts_clean),
    }

    payload = {
        "counts": counts,
        "positions": positions_clean,
        "holdings": holdings_clean,
        "orders": orders_clean,
        "gtts": gtts_clean,
        "skipped": plans["skipped"],
    }

    ledger = SandboxCorporateAction(
        notice_id=action.notice_id,
        symbol=action.symbol,
        exchange=action.exchange,
        action_type=action.action_type,
        ratio_from=action.ratio_from,
        ratio_to=action.ratio_to,
        effective_date=action.effective_date,
        status="applied",
        affected_accounts=counts["accounts"],
        positions_adjusted=counts["positions"],
        holdings_adjusted=counts["holdings"],
        orders_adjusted=counts["orders"],
        gtts_adjusted=counts["gtts"],
        result_json=json.dumps(payload, default=str),
        applied_by=applied_by,
        applied_at=now,
    )
    db_session.add(ledger)
    db_session.commit()

    logger.info(
        "Corporate action applied: %s %s %s (%s %s:%s, eff %s) - "
        "accounts=%s positions=%s holdings=%s orders=%s gtts=%s",
        action.action_type,
        action.symbol,
        action.exchange,
        action.notice_id,
        action.ratio_from,
        action.ratio_to,
        action.effective_date.isoformat(),
        counts["accounts"],
        counts["positions"],
        counts["holdings"],
        counts["orders"],
        counts["gtts"],
    )

    result = _action_base_dict(action)
    result.update(
        {
            "already_applied": False,
            "applied_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "applied_by": applied_by,
            "counts": counts,
            "positions": positions_clean,
            "holdings": holdings_clean,
            "orders": orders_clean,
            "gtts": gtts_clean,
            "skipped": plans["skipped"],
        }
    )
    return result


def apply_actions(actions, applied_by=None):
    """Apply every action line. Idempotent: applied lines replay their result.

    Returns ``(results, summary)`` where summary counts only conversions made
    by THIS call (replayed notices contribute zero), which is what the UI shows
    after confirming an import.
    """
    ensure_apply_is_current(actions)
    now = ist_now()
    results = []
    newly_applied = 0
    summary = {"accounts": set(), "positions": 0, "holdings": 0, "orders": 0, "gtts": 0}

    for action in actions:
        try:
            result, changed = _apply_one(action, applied_by, now)
        except IntegrityError:
            # Two confirms of the same notice raced; the unique
            # (notice_id, symbol, exchange) constraint let the other one win.
            # Drop this request's conversions and replay its stored result,
            # so both callers see the same idempotent outcome.
            db_session.rollback()
            ledger_row = _get_ledger(action)
            if ledger_row is None:  # an unrelated constraint failed
                raise
            result, changed = _stored_result(action, ledger_row), False
        except Exception:
            db_session.rollback()
            raise
        results.append(result)
        if changed:
            newly_applied += 1
            summary["accounts"].update(
                {p["user_id"] for p in result["positions"]}
                | {h["user_id"] for h in result["holdings"]}
                | {o["user_id"] for o in result["orders"]}
                | {g["user_id"] for g in result["gtts"]}
            )
            summary["positions"] += result["counts"]["positions"]
            summary["holdings"] += result["counts"]["holdings"]
            summary["orders"] += result["counts"]["orders"]
            summary["gtts"] += result["counts"]["gtts"]

    summary_out = {
        "notice_lines": len(actions),
        "newly_applied": newly_applied,
        "already_applied": len(actions) - newly_applied,
        "accounts_affected": len(summary["accounts"]),
        "positions": summary["positions"],
        "holdings": summary["holdings"],
        "orders": summary["orders"],
        "gtts": summary["gtts"],
    }
    return results, summary_out
