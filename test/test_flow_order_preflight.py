"""Isolated tests for Flow's no-order preflight checks."""

import inspect
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.flow_preflight_service import (  # noqa: E402
    FlowPreflightClient,
    preflight_workflow,
)

POSITION = {"x": 0, "y": 0}


def workflow(nodes, edges=None):
    return SimpleNamespace(
        id=1,
        name="preflight test",
        nodes=nodes,
        edges=edges if edges is not None else [{"id": "e1", "source": "start", "target": "order"}],
    )


def place_order_node(**overrides):
    data = {
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "priceType": "MARKET",
        "product": "MIS",
        **overrides,
    }
    return [
        {"id": "start", "type": "start", "position": POSITION, "data": {}},
        {"id": "order", "type": "placeOrder", "position": POSITION, "data": data},
    ]


def patch_market(monkeypatch, *, available=1000.0, margin=100.0, quote_ltp=100.0, info=None):
    info = info or {"lotsize": 1, "tick_size": 0.05, "freeze_qty": 0}

    monkeypatch.setattr(
        FlowPreflightClient,
        "symbol",
        lambda self, symbol, exchange: {"status": "success", "data": info},
    )
    monkeypatch.setattr(
        FlowPreflightClient,
        "get_quotes",
        lambda self, symbol, exchange: {"status": "success", "data": {"ltp": quote_ltp}},
    )
    monkeypatch.setattr(
        FlowPreflightClient,
        "funds",
        lambda self: {"status": "success", "data": {"availablecash": available}},
    )
    monkeypatch.setattr(
        FlowPreflightClient,
        "margin",
        lambda self, positions=None, **kwargs: {
            "status": "success",
            "data": {"total_margin_required": margin},
        },
    )


def test_preflight_accepts_a_valid_order_leg_without_execution_record(monkeypatch):
    patch_market(monkeypatch)

    result = preflight_workflow(workflow(place_order_node()), api_key="test-key")

    assert result["status"] == "success"
    assert result["checked_legs"] == 1
    assert result["failed_legs"] == 0
    messages = "\n".join(entry["message"] for entry in result["logs"])
    assert "不会下单" in messages
    assert "不会写入正式执行记录" in messages
    assert "Combined order margin 100.00 is within available cash 1,000.00" in messages
    source = inspect.getsource(preflight_workflow)
    assert "create_execution" not in source
    assert "update_execution_status" not in source


def test_preflight_points_insufficient_funds_to_the_leg(monkeypatch):
    patch_market(monkeypatch, available=80.0, margin=100.0)

    result = preflight_workflow(workflow(place_order_node()), api_key="test-key")

    assert result["status"] == "error"
    assert result["checked_legs"] == 1
    assert result["failed_legs"] == 1
    messages = "\n".join(entry["message"] for entry in result["logs"])
    assert "leg NSE:INFY BUY qty=1 MARKET" in messages
    assert "combined strategy funds are insufficient" in messages
    assert "short by 20.00" in messages


def test_preflight_reports_contract_and_lot_size_rejections(monkeypatch):
    monkeypatch.setattr(
        FlowPreflightClient,
        "symbol",
        lambda self, symbol, exchange: {
            "status": "error",
            "error": "Symbol 'INFY' not found for exchange 'NSE'",
        },
    )

    def fail_margin(self, positions=None, **kwargs):
        raise AssertionError("margin must not be calculated for an unresolvable contract")

    monkeypatch.setattr(FlowPreflightClient, "margin", fail_margin)

    result = preflight_workflow(
        workflow(place_order_node(quantity=2, product="NRML")), api_key="test-key"
    )

    assert result["status"] == "error"
    assert result["failed_legs"] == 1
    messages = "\n".join(entry["message"] for entry in result["logs"])
    assert "contract rejected" in messages
    assert "Symbol 'INFY' not found" in messages


def test_preflight_adds_margin_across_separate_order_nodes(monkeypatch):
    patch_market(monkeypatch, available=1000.0)

    def margin(self, positions=None, **kwargs):
        required = 600.0 if positions and positions[0]["symbol"] == "INFY" else 600.0
        return {"status": "success", "data": {"total_margin_required": required}}

    monkeypatch.setattr(FlowPreflightClient, "margin", margin)
    nodes = [
        {"id": "start", "type": "start", "position": POSITION, "data": {}},
        {
            "id": "order-1",
            "type": "placeOrder",
            "position": POSITION,
            "data": {
                "symbol": "INFY",
                "exchange": "NSE",
                "action": "BUY",
                "quantity": 1,
                "priceType": "MARKET",
                "product": "MIS",
            },
        },
        {
            "id": "order-2",
            "type": "placeOrder",
            "position": POSITION,
            "data": {
                "symbol": "TCS",
                "exchange": "NSE",
                "action": "BUY",
                "quantity": 1,
                "priceType": "MARKET",
                "product": "MIS",
            },
        },
    ]
    edges = [
        {"id": "e1", "source": "start", "target": "order-1"},
        {"id": "e2", "source": "order-1", "target": "order-2"},
    ]

    result = preflight_workflow(workflow(nodes, edges), api_key="test-key")

    assert result["checked_legs"] == 2
    assert result["failed_legs"] == 2
    messages = "\n".join(entry["message"] for entry in result["logs"])
    assert "total margin 1,200.00" in messages
    assert "available 1,000.00" in messages
    assert "short by 200.00" in messages


def test_preflight_checks_every_option_leg_and_names_the_failing_leg(monkeypatch):
    from services.flow_executor_service import NodeExecutor

    monkeypatch.setattr(NodeExecutor, "_resolve_lot_size", lambda self, underlying, exchange: 25)

    def resolve_leg(self, leg, common):
        symbol = f"NIFTY28OCT2524500{leg['option_type']}"
        return (
            True,
            {
                "status": "success",
                "symbol": symbol,
                "exchange": "NFO",
                "lotsize": 25,
                "tick_size": 0.05,
                "freeze_qty": 1800,
            },
            200,
        )

    monkeypatch.setattr(FlowPreflightClient, "_resolve_preflight_leg_symbol", resolve_leg)
    monkeypatch.setattr(
        FlowPreflightClient,
        "symbol",
        lambda self, symbol, exchange: {
            "status": "success",
            "data": {"lotsize": 25, "tick_size": 0.05, "freeze_qty": 1800},
        },
    )

    def quote(self, symbol, exchange):
        if symbol.endswith("PE"):
            return {"status": "error", "message": "No usable LTP for PE"}
        return {"status": "success", "data": {"ltp": 12.5}}

    monkeypatch.setattr(FlowPreflightClient, "get_quotes", quote)
    monkeypatch.setattr(
        FlowPreflightClient,
        "funds",
        lambda self: {"status": "success", "data": {"availablecash": 100000}},
    )
    monkeypatch.setattr(
        FlowPreflightClient,
        "margin",
        lambda self, positions=None, **kwargs: {
            "status": "success",
            "data": {"total_margin_required": 3000},
        },
    )

    nodes = [
        {"id": "start", "type": "start", "position": POSITION, "data": {}},
        {
            "id": "order",
            "type": "optionsMultiOrder",
            "position": POSITION,
            "data": {
                "strategy": "custom",
                "underlying": "NIFTY",
                "expiryDate": "28OCT25",
                "quantity": 1,
                "product": "NRML",
                "priceType": "MARKET",
                "legs": [
                    {"optionType": "CE", "action": "BUY", "quantity": 1, "offset": "ATM"},
                    {"optionType": "PE", "action": "SELL", "quantity": 1, "offset": "ATM"},
                ],
            },
        },
    ]

    result = preflight_workflow(workflow(nodes), api_key="test-key")

    assert result["checked_legs"] == 2
    assert result["failed_legs"] == 1
    messages = "\n".join(entry["message"] for entry in result["logs"])
    assert "leg 2 NFO:NIFTY28OCT2524500PE SELL qty=25 MARKET" in messages
    assert "No usable LTP for PE" in messages


def test_readonly_client_hard_blocks_non_order_writes():
    client = FlowPreflightClient("test-key", [])

    for method_name, args in (
        ("modify_order", ("1", "INFY", "NSE", "BUY", 1)),
        ("cancel_order", ("1",)),
        ("cancel_all_orders", ()),
        ("close_all_positions", ()),
        ("close_position", ("INFY", "NSE")),
    ):
        result = getattr(client, method_name)(*args)
        assert result["status"] == "error"
        assert result["safety_violation"] is True
