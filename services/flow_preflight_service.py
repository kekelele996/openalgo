"""Read-only preflight checks for Flow order nodes.

This service resolves the same final order payloads that ``NodeExecutor`` would
send, but replaces every broker write with contract, quote and margin checks.
It deliberately has no dependency on ``flow_workflow_executions``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from services.flow_executor_service import NodeExecutor, WorkflowContext
from services.flow_openalgo_client import FlowOpenAlgoClient
from services.flow_workflow_validator import validate_workflow
from services.option_symbol_service import (
    construct_option_symbol,
    find_option_in_database,
    get_option_exchange,
    get_option_symbol,
    parse_underlying_symbol,
)

# Nodes that create a new broker order. Modify/cancel/close nodes intentionally
# are not included: this check answers "will these new order legs be accepted?".
PREFLIGHT_ORDER_NODE_TYPES = frozenset(
    {
        "placeOrder",
        "smartOrder",
        "optionsOrder",
        "optionsMultiOrder",
        "basketOrder",
        "splitOrder",
    }
)

MAX_SPLIT_ORDERS = 100

_MARGIN_KEYS = frozenset(
    {
        "total_margin_required",
        "total_margin",
        "totalmargin",
        "totalmarginrequired",
        "totalfundsrequired",
        "margin_required",
        "required_margin",
        "reqdmrgn",
    }
)
_FUNDS_KEYS = (
    "availablecash",
    "availablemargin",
    "netmarginavailable",
    "available_cash",
    "cash",
    "balance",
)


class _Undefined:
    """Sentinel for a funds lookup that has not happened yet."""


undefined = _Undefined()


def _now() -> str:
    return datetime.now().isoformat()


def _log(logs: list[dict[str, Any]], message: str, level: str = "info") -> None:
    logs.append({"time": _now(), "message": message, "level": level})


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:,.2f}"


def _extract_available_funds(result: dict[str, Any]) -> float | None:
    data = result.get("data")
    sources: list[Any] = [result]
    if isinstance(data, dict):
        sources.append(data)

    for source in sources:
        for key in _FUNDS_KEYS:
            value = _as_float(source.get(key))
            if value is not None and value >= 0:
                return value
    return None


def _extract_margin_required(result: dict[str, Any]) -> float | None:
    """Read normalized broker margin totals, tolerating broker-specific nesting."""

    def walk(value: Any) -> float | None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in _MARGIN_KEYS:
                    number = _as_float(child)
                    if number is not None and number >= 0:
                        return number
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        return None

    return walk(result)


def _tick_aligned(price: float, tick_size: float) -> bool:
    if tick_size <= 0:
        return True
    units = price / tick_size
    return abs(units - round(units)) <= 1e-7


class PreflightNodeExecutor(NodeExecutor):
    """NodeExecutor with user-facing wording changed from placing to checking."""

    def log(self, message: str, level: str = "info"):  # noqa: A003 - parent API
        checked_message = (
            message.replace("Placing ", "Checking ")
            .replace(" order result:", " check result:")
            .replace("Order result:", "Check result:")
            .replace("Options order result:", "Options order check result:")
            .replace("Options multi-order result:", "Options multi-order check result:")
            .replace("Basket order result:", "Basket order check result:")
            .replace("Split order result:", "Split order check result:")
        )
        super().log(checked_message, level)


class FlowPreflightClient(FlowOpenAlgoClient):
    """Flow client that resolves and validates orders but refuses every write."""

    def __init__(self, api_key: str, logs: list[dict[str, Any]]):
        super().__init__(api_key)
        self.logs = logs
        self.node_label = "order node"
        self.checked_legs = 0
        self.failed_legs = 0
        self.baskets: list[dict[str, Any]] = []
        self.finalized = False
        self._available_funds: float | None | _Undefined = undefined

    def set_node_label(self, label: str) -> None:
        self.node_label = label or "order node"

    def _log(self, message: str, level: str = "info") -> None:
        _log(self.logs, message, level)

    def _safety_error(self, operation: str) -> dict[str, Any]:
        message = f"Preflight safety guard blocked {operation}; no broker write is allowed."
        self._log(message, "error")
        return {"status": "error", "message": message, "safety_violation": True}

    # Non-order writes are hard-blocked. New-order methods are replaced below
    # with read-only checks rather than being allowed to inherit broker writes.
    def modify_order(self, *args, **kwargs) -> dict[str, Any]:
        return self._safety_error("modify_order")

    def cancel_order(self, *args, **kwargs) -> dict[str, Any]:
        return self._safety_error("cancel_order")

    def cancel_all_orders(self, *args, **kwargs) -> dict[str, Any]:
        return self._safety_error("cancel_all_orders")

    def close_all_positions(self, *args, **kwargs) -> dict[str, Any]:
        return self._safety_error("close_all_positions")

    def close_position(self, *args, **kwargs) -> dict[str, Any]:
        return self._safety_error("close_position")

    def _available_cash(self) -> tuple[float | None, str | None]:
        if not isinstance(self._available_funds, _Undefined):
            if self._available_funds is None:
                return None, "Could not read available cash from the funds response."
            return self._available_funds, None

        result = self.funds()
        if result.get("status") != "success":
            self._available_funds = None
            return None, result.get("error") or result.get("message") or "Funds lookup failed."

        available = _extract_available_funds(result)
        self._available_funds = available
        if available is None:
            return None, "The funds response did not include usable available cash."
        return available, None

    def _contract_info(
        self, symbol: str, exchange: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        result = self.symbol(symbol, exchange)
        if result.get("status") != "success":
            return None, result.get("error") or result.get("message") or "Contract lookup failed."
        info = result.get("data")
        if not isinstance(info, dict):
            return None, "The contract response did not include instrument details."
        return info, None

    def _resolved_option_meta(
        self, symbol: str, exchange: str, response: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Use authoritative symbol details for a resolver-produced option."""
        info, error = self._contract_info(symbol, exchange)
        if error:
            return None, error
        for key in ("lotsize", "tick_size", "freeze_qty"):
            if info.get(key) is None and response.get(key) is not None:
                info[key] = response[key]
        return info, None

    def _quote_ltp(self, symbol: str, exchange: str) -> tuple[float | None, str | None]:
        result = self.get_quotes(symbol=symbol, exchange=exchange)
        if result.get("status") != "success":
            return None, result.get("error") or result.get("message") or "Quote lookup failed."
        data = result.get("data")
        if not isinstance(data, dict):
            return None, "The quote response did not include market data."
        ltp = _as_float(data.get("ltp"))
        if ltp is None or ltp <= 0:
            return None, f"No usable positive LTP was returned for {exchange}:{symbol}."
        return ltp, None

    @staticmethod
    def _leg_descriptor(order: dict[str, Any], index: int, total: int) -> str:
        prefix = f"leg {index}" if total > 1 else "leg"
        return (
            f"{prefix} {order['exchange']}:{order['symbol']} "
            f"{order['action']} qty={order['quantity']} {order['price_type']}"
        )

    @staticmethod
    def _contract_rule_errors(
        order: dict[str, Any], info: dict[str, Any], max_single_quantity: int
    ) -> list[str]:
        errors: list[str] = []
        quantity = int(order["quantity"])
        lot_size = _as_float(info.get("lotsize"))
        tick_size = _as_float(info.get("tick_size")) or 0.0
        freeze_qty = _as_float(info.get("freeze_qty")) or 0.0

        if lot_size is not None and lot_size > 0:
            lot_size_int = int(lot_size)
            if quantity % lot_size_int != 0:
                errors.append(
                    f"quantity {quantity} is not a whole multiple of the contract lot size {lot_size_int}"
                )

        if freeze_qty > 0 and max_single_quantity > int(freeze_qty):
            errors.append(
                f"one child order would send {max_single_quantity} units, exceeding freeze quantity {int(freeze_qty)}"
            )

        for field_name, value in (
            ("price", order.get("price") or 0.0),
            ("trigger price", order.get("trigger_price") or 0.0),
        ):
            price = _as_float(value)
            if price is not None and price > 0 and not _tick_aligned(price, tick_size):
                tick_text = f"{tick_size:g}" if tick_size else "unknown"
                errors.append(f"{field_name} {price:g} is not aligned to tick size {tick_text}")
        return errors

    @staticmethod
    def _margin_position(order: dict[str, Any], ltp: float) -> dict[str, str]:
        price_type = order["price_type"]
        if price_type == "LIMIT" and float(order.get("price") or 0) > 0:
            reference_price = float(order["price"])
        elif price_type == "SL" and float(order.get("trigger_price") or 0) > 0:
            reference_price = float(order["trigger_price"])
        else:
            reference_price = ltp
        return {
            "exchange": order["exchange"],
            "symbol": order["symbol"],
            "action": order["action"],
            "quantity": str(int(order["quantity"])),
            "product": order["product"],
            "pricetype": price_type,
            "price": str(reference_price),
        }

    def _check_order_basket(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        started_checked = self.checked_legs
        started_failed = self.failed_legs
        enriched: list[dict[str, Any]] = []
        total = len(orders)

        for index, order in enumerate(orders, start=1):
            self.checked_legs += 1
            descriptor = self._leg_descriptor(order, index, total)
            errors: list[str] = []

            meta = order.get("meta") if isinstance(order.get("meta"), dict) else None
            if meta is not None:
                info: dict[str, Any] | None = meta
            else:
                info, contract_error = self._contract_info(order["symbol"], order["exchange"])
                if contract_error:
                    errors.append(f"contract rejected: {contract_error}")

            ltp: float | None = None
            if info is not None:
                ltp, quote_error = self._quote_ltp(order["symbol"], order["exchange"])
                if quote_error:
                    errors.append(f"market data rejected: {quote_error}")

            split_size = int(order.get("split_size") or 0)
            child_count = 0
            max_single_quantity = int(order["quantity"])
            if info is not None and split_size > 0:
                quantity = int(order["quantity"])
                child_count = math.ceil(quantity / split_size)
                max_single_quantity = min(quantity, split_size)
                if child_count > MAX_SPLIT_ORDERS:
                    errors.append(
                        f"the split would create {child_count} orders, exceeding the limit {MAX_SPLIT_ORDERS}"
                    )

            if info is not None:
                errors.extend(self._contract_rule_errors(order, info, max_single_quantity))

            if errors:
                self.failed_legs += 1
                for error in errors:
                    self._log(f"{self.node_label} / {descriptor}: {error}", "error")
                continue

            enriched.append(
                {
                    "index": index,
                    "order": order,
                    "descriptor": descriptor,
                    "ltp": ltp,
                    "child_count": child_count,
                }
            )

        if enriched:
            positions = [self._margin_position(item["order"], item["ltp"]) for item in enriched]
            # The real multi-order service submits BUY legs first for margin
            # efficiency; keep the same ordering for the basket estimate.
            positions.sort(key=lambda position: 0 if position["action"] == "BUY" else 1)
            margin_result = self.margin(positions=positions)
            if margin_result.get("status") != "success":
                reason = (
                    margin_result.get("error")
                    or margin_result.get("message")
                    or "Margin calculation failed."
                )
                for item in enriched:
                    self.failed_legs += 1
                    self._log(
                        f"{self.node_label} / {item['descriptor']}: margin check failed: {reason}",
                        "error",
                    )
            else:
                margin_required = _extract_margin_required(margin_result)
                if margin_required is None:
                    reason = "the margin response did not include a total margin requirement."
                    for item in enriched:
                        self.failed_legs += 1
                        self._log(
                            f"{self.node_label} / {item['descriptor']}: margin check failed: {reason}",
                            "error",
                        )
                else:
                    self.baskets.append(
                        {
                            "label": self.node_label,
                            "items": enriched,
                            "margin_required": margin_required,
                        }
                    )

        checked_now = self.checked_legs - started_checked
        failed_now = self.failed_legs - started_failed
        return {
            "status": "success" if failed_now == 0 else "error",
            "checked_legs": checked_now,
            "failed_legs": failed_now,
        }

    def finalize_funds_check(self) -> None:
        """Compare the combined margin of every node basket with available cash."""
        if self.finalized:
            return
        self.finalized = True
        if not self.baskets:
            return

        available, funds_error = self._available_cash()
        total_required = sum(basket["margin_required"] for basket in self.baskets)
        if funds_error:
            for basket in self.baskets:
                for item in basket["items"]:
                    self.failed_legs += 1
                    self._log(
                        f"{basket['label']} / {item['descriptor']}: funds check failed: {funds_error}",
                        "error",
                    )
            return

        if total_required > available + 1e-9:
            shortfall = total_required - available
            for basket in self.baskets:
                for item in basket["items"]:
                    self.failed_legs += 1
                    self._log(
                        f"{basket['label']} / {item['descriptor']}: combined strategy funds are insufficient; "
                        f"this node requires {_money(basket['margin_required'])}, total margin "
                        f"{_money(total_required)}, available {_money(available)}, short by {_money(shortfall)}",
                        "error",
                    )
            return

        for basket in self.baskets:
            for item in basket["items"]:
                split_note = (
                    f", {item['child_count']} split child order(s)" if item["child_count"] else ""
                )
                self._log(
                    f"{basket['label']} / {item['descriptor']}: contract and LTP {item['ltp']:g} "
                    f"look acceptable{split_note}",
                    "info",
                )
            self._log(
                f"{basket['label']}: basket margin {_money(basket['margin_required'])}.", "info"
            )
        self._log(
            f"Combined order margin {_money(total_required)} is within available cash {_money(available)}.",
            "info",
        )

    @staticmethod
    def _standard_order(
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        price_type: str,
        product_type: str,
        price: float,
        trigger_price: float,
        split_size: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol": symbol,
                "exchange": exchange,
                "action": action,
                "quantity": int(quantity),
                "price_type": price_type,
                "product": product_type,
                "price": float(price or 0.0),
                "trigger_price": float(trigger_price or 0.0),
                "split_size": int(split_size or 0),
                "meta": meta,
            }
        ]

    def place_order(  # type: ignore[override]
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        price_type: str = "MARKET",
        product_type: str = "MIS",
        price: float = 0,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        strategy: str = "flow_workflow",
    ) -> dict[str, Any]:
        return self._check_order_basket(
            self._standard_order(
                symbol, exchange, action, quantity, price_type, product_type, price, trigger_price
            )
        )

    def place_smart_order(  # type: ignore[override]
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        position_size: int = 0,
        price_type: str = "MARKET",
        product_type: str = "MIS",
        price: float = 0,
        trigger_price: float = 0,
        strategy: str = "flow_workflow",
    ) -> dict[str, Any]:
        return self._check_order_basket(
            self._standard_order(
                symbol, exchange, action, quantity, price_type, product_type, price, trigger_price
            )
        )

    def split_order(  # type: ignore[override]
        self,
        symbol: str,
        exchange: str,
        action: str,
        quantity: int,
        split_size: int,
        price_type: str = "MARKET",
        product_type: str = "MIS",
        price: float = 0,
        trigger_price: float = 0,
        strategy: str = "flow_workflow",
    ) -> dict[str, Any]:
        return self._check_order_basket(
            self._standard_order(
                symbol,
                exchange,
                action,
                quantity,
                price_type,
                product_type,
                price,
                trigger_price,
                split_size=split_size,
            )
        )

    def basket_order(  # type: ignore[override]
        self, orders: list[dict[str, Any]], strategy: str = "flow_workflow"
    ) -> dict[str, Any]:
        normalized = []
        for order in orders:
            normalized.append(
                {
                    "symbol": order["symbol"],
                    "exchange": order["exchange"],
                    "action": order["action"],
                    "quantity": int(order["quantity"]),
                    "price_type": order.get("pricetype", order.get("price_type", "MARKET")),
                    "product": order.get("product", "MIS"),
                    "price": float(order.get("price", 0.0) or 0.0),
                    "trigger_price": float(
                        order.get("trigger_price", order.get("triggerprice", 0.0)) or 0.0
                    ),
                    "split_size": int(order.get("splitsize", order.get("split_size", 0)) or 0),
                }
            )
        return self._check_order_basket(normalized)

    def options_order(  # type: ignore[override]
        self,
        underlying: str,
        exchange: str,
        action: str,
        quantity: int,
        expiry_date: str,
        offset: str,
        option_type: str,
        price_type: str = "MARKET",
        product: str = "NRML",
        price: float = 0,
        splitsize: int = 0,
        strategy: str = "flow_workflow",
        trigger_price: float = 0,
    ) -> dict[str, Any]:
        success, response, _status = get_option_symbol(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry_date,
            strike_int=None,
            offset=offset,
            option_type=option_type,
            api_key=self.api_key,
        )
        if not success:
            self.checked_legs += 1
            self.failed_legs += 1
            reason = response.get("message", "Failed to resolve option contract.")
            self._log(
                f"{self.node_label} / leg {underlying} {expiry_date} {offset} {option_type}: {reason}",
                "error",
            )
            return {"status": "error", "checked_legs": 1, "failed_legs": 1}

        symbol = response["symbol"]
        resolved_exchange = response["exchange"]
        meta, meta_error = self._resolved_option_meta(symbol, resolved_exchange, response)
        if meta_error:
            self.checked_legs += 1
            self.failed_legs += 1
            self._log(
                f"{self.node_label} / leg {resolved_exchange}:{symbol}: contract rejected: {meta_error}",
                "error",
            )
            return {"status": "error", "checked_legs": 1, "failed_legs": 1}
        return self._check_order_basket(
            self._standard_order(
                symbol,
                resolved_exchange,
                action,
                quantity,
                price_type,
                product,
                price,
                trigger_price,
                split_size=splitsize,
                meta=meta,
            )
        )

    def _resolve_preflight_leg_symbol(
        self, leg: dict[str, Any], common: dict[str, Any]
    ) -> tuple[bool, dict[str, Any], int]:
        """Read-only equivalent of options multi-order's leg symbol resolver."""
        leg_expiry = leg.get("expiry_date") or common.get("expiry_date")
        strike = leg.get("strike")
        if strike is None:
            return get_option_symbol(
                underlying=common.get("underlying"),
                exchange=common.get("exchange"),
                expiry_date=leg_expiry,
                strike_int=common.get("strike_int"),
                offset=leg.get("offset"),
                option_type=leg.get("option_type"),
                api_key=self.api_key,
            )

        base_symbol, embedded_expiry = parse_underlying_symbol(common.get("underlying", ""))
        resolved_expiry = embedded_expiry or leg_expiry
        if not resolved_expiry:
            return (
                False,
                {
                    "status": "error",
                    "message": "No expiry date available to resolve the leg symbol.",
                },
                400,
            )
        options_exchange = get_option_exchange(common.get("exchange", ""))
        option_symbol = construct_option_symbol(
            base_symbol, resolved_expiry, strike, str(leg.get("option_type", "")).strip().upper()
        )
        option_info = find_option_in_database(option_symbol, options_exchange)
        if not option_info:
            return (
                False,
                {
                    "status": "error",
                    "message": (
                        f"Option symbol {option_symbol} not found in {options_exchange}. "
                        "Check the strike/expiry or update the master contract."
                    ),
                },
                404,
            )
        return (
            True,
            {
                "status": "success",
                "symbol": option_info.get("symbol", option_symbol),
                "exchange": option_info.get("exchange", options_exchange),
                "lotsize": option_info.get("lotsize"),
                "tick_size": option_info.get("tick_size"),
            },
            200,
        )

    def options_multi_order(  # type: ignore[override]
        self,
        underlying: str,
        exchange: str,
        expiry_date: str,
        legs: list[dict[str, Any]],
        strategy: str = "flow_workflow",
        strike_int: int | None = None,
    ) -> dict[str, Any]:
        common = {
            "underlying": underlying,
            "exchange": exchange,
            "expiry_date": expiry_date,
            "strike_int": strike_int,
        }
        normalized: list[dict[str, Any]] = []
        for index, leg in enumerate(legs, start=1):
            success, response, _status = self._resolve_preflight_leg_symbol(leg, common)
            if not success:
                self.checked_legs += 1
                self.failed_legs += 1
                reason = response.get("message", "Failed to resolve option leg.")
                selector = leg.get("strike") if leg.get("strike") is not None else leg.get("offset")
                self._log(
                    f"{self.node_label} / leg {index} {underlying} {leg.get('expiry_date') or expiry_date} "
                    f"{selector} {leg.get('option_type')} {leg.get('action')}: {reason}",
                    "error",
                )
                continue

            symbol = response["symbol"]
            resolved_exchange = response["exchange"]
            meta, meta_error = self._resolved_option_meta(symbol, resolved_exchange, response)
            if meta_error:
                self.checked_legs += 1
                self.failed_legs += 1
                self._log(
                    f"{self.node_label} / leg {index} {resolved_exchange}:{symbol}: contract rejected: {meta_error}",
                    "error",
                )
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": resolved_exchange,
                    "action": leg["action"],
                    "quantity": int(leg["quantity"]),
                    "price_type": leg.get("pricetype", "MARKET"),
                    "product": leg.get("product", "NRML"),
                    "price": float(leg.get("price", 0.0) or 0.0),
                    "trigger_price": float(leg.get("trigger_price", 0.0) or 0.0),
                    "split_size": int(leg.get("splitsize", 0) or 0),
                    "meta": meta,
                }
            )
        return self._check_order_basket(normalized)


def preflight_workflow(workflow: Any, api_key: str) -> dict[str, Any]:
    """Check every new-order node in a saved workflow without broker writes."""

    logs: list[dict[str, Any]] = []
    _log(logs, "只读预检开始：不会下单，也不会写入正式执行记录。", "info")

    validation_errors = validate_workflow(
        {"name": workflow.name, "nodes": workflow.nodes or [], "edges": workflow.edges or []},
        strict=True,
    )
    if validation_errors:
        for error in validation_errors:
            _log(
                logs,
                f"workflow structure rejected at {error.get('path')}: {error.get('message')}",
                "error",
            )
        message = validation_errors[0].get("message", "Workflow is not runnable.")
        _log(logs, f"预检失败：{message}", "error")
        return {
            "status": "error",
            "message": f"Workflow cannot be checked: {message}",
            "logs": logs,
            "checked_legs": 0,
            "failed_legs": 0,
            "failed_nodes": len(validation_errors),
        }

    nodes = workflow.nodes or []
    order_nodes = [
        node
        for node in nodes
        if isinstance(node, dict) and node.get("type") in PREFLIGHT_ORDER_NODE_TYPES
    ]
    if not order_nodes:
        _log(logs, "No new-order nodes were found; nothing can place an order.", "info")
        return {
            "status": "success",
            "message": "No order legs to check.",
            "logs": logs,
            "checked_legs": 0,
            "failed_legs": 0,
            "failed_nodes": 0,
        }

    client = FlowPreflightClient(api_key, logs)
    context = WorkflowContext(workflow_id=getattr(workflow, "id", None))
    failed_nodes = 0

    handlers = {
        "placeOrder": "execute_place_order",
        "smartOrder": "execute_smart_order",
        "optionsOrder": "execute_options_order",
        "optionsMultiOrder": "execute_options_multi_order",
        "basketOrder": "execute_basket_order",
        "splitOrder": "execute_split_order",
    }

    for position, node in enumerate(order_nodes, start=1):
        node_type = node.get("type")
        node_id = node.get("id") or f"node_{position}"
        label = f"{node_type} node {node_id}"
        client.set_node_label(label)
        executor = PreflightNodeExecutor(
            client, context, logs, default_strategy=getattr(workflow, "name", "flow_workflow")
        )
        _log(logs, f"Checking {label}...", "info")
        try:
            result = getattr(executor, handlers[node_type])(node.get("data") or {})
        except Exception as exc:  # a check must never escape as a pseudo-execution
            failed_nodes += 1
            _log(logs, f"{label}: preflight error: {exc}", "error")
            continue

        if not isinstance(result, dict) or result.get("status") != "success":
            failed_nodes += 1
            reason = (
                result.get("message") or result.get("error")
                if isinstance(result, dict)
                else "The order node returned an unusable check result."
            )
            _log(logs, f"{label}: {reason}", "error")

    try:
        client.finalize_funds_check()
    except Exception as exc:
        failed_nodes += 1
        _log(logs, f"Combined funds check failed: {exc}", "error")

    if client.failed_legs or failed_nodes:
        message = (
            f"预检发现 {client.failed_legs} 条问题腿"
            + (f"、{failed_nodes} 个问题节点" if failed_nodes else "")
            + "；未下单。"
        )
        _log(logs, message, "error")
        return {
            "status": "error",
            "message": message,
            "logs": logs,
            "checked_legs": client.checked_legs,
            "failed_legs": client.failed_legs,
            "failed_nodes": failed_nodes,
        }

    message = f"预检通过：{client.checked_legs} 条下单腿均可进入提交前校验；未下单。"
    _log(logs, message, "info")
    return {
        "status": "success",
        "message": message,
        "logs": logs,
        "checked_legs": client.checked_legs,
        "failed_legs": 0,
        "failed_nodes": 0,
    }
