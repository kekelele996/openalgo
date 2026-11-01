#!/usr/bin/env python3
"""Corporate action notices (stock split / bonus issue) for the sandbox.

Covers:
- ratio/quantity/price conversion math for splits and bonus issues,
- CSV parsing (aliases, deterministic notice ids, malformed rows),
- preview is a dry run and lists before/after terms,
- apply converts positions, holdings, resting orders and active GTTs while
  leaving trades, realised P&L and post-effective-date orders untouched,
- re-importing the same notice is idempotent,
- mixed old/new-terms books rebuild from trades,
- a future effective date cannot be applied early,
- the blueprint routes work end to end.
"""

import sys
import uuid
from datetime import datetime, date
from decimal import Decimal

import pytest


def _uid():
    return uuid.uuid4().hex[:12]

from database.sandbox_db import (
    SandboxCorporateAction,
    SandboxFunds,
    SandboxGTT,
    SandboxGTTLeg,
    SandboxHoldings,
    SandboxOrders,
    SandboxPositions,
    SandboxTrades,
    db_session,
    init_db,
)
from sandbox import corporate_actions as ca

EFFECTIVE = date(2026, 10, 15)
PRE_EX = datetime(2026, 10, 10, 10, 0, 0)
POST_EX = datetime(2026, 10, 20, 10, 0, 0)


def reset_sandbox():
    """Wipe the sandbox books so tests never see one another's data."""
    SandboxCorporateAction.query.delete()
    SandboxGTTLeg.query.delete()
    SandboxGTT.query.delete()
    SandboxTrades.query.delete()
    SandboxOrders.query.delete()
    SandboxPositions.query.delete()
    SandboxHoldings.query.delete()
    SandboxFunds.query.delete()
    db_session.commit()


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    db_session.rollback()
    reset_sandbox()
    yield
    db_session.rollback()
    reset_sandbox()


def make_funds(user_id):
    funds = SandboxFunds(
        user_id=user_id,
        total_capital=Decimal("10000000.00"),
        available_balance=Decimal("10000000.00"),
    )
    db_session.add(funds)
    db_session.commit()
    return funds


def make_position(user_id, symbol, exchange, product, qty, avg, ltp=None):
    pos = SandboxPositions(
        user_id=user_id,
        symbol=symbol,
        exchange=exchange,
        product=product,
        quantity=qty,
        average_price=Decimal(str(avg)),
        ltp=Decimal(str(ltp)) if ltp else None,
        created_at=PRE_EX,
        updated_at=PRE_EX,
    )
    db_session.add(pos)
    db_session.commit()
    return pos


def make_holding(user_id, symbol, exchange, qty, avg, ltp=None, settled=date(2026, 10, 11)):
    holding = SandboxHoldings(
        user_id=user_id,
        symbol=symbol,
        exchange=exchange,
        quantity=qty,
        average_price=Decimal(str(avg)),
        ltp=Decimal(str(ltp)) if ltp else None,
        settlement_date=settled,
        created_at=PRE_EX,
        updated_at=PRE_EX,
    )
    db_session.add(holding)
    db_session.commit()
    return holding


def make_trade(user_id, symbol, exchange, product, action, qty, price, ts):
    uid = _uid()
    trade = SandboxTrades(
        tradeid=f"T-{uid}",
        orderid=f"O-{uid}",
        user_id=user_id,
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        product=product,
        trade_timestamp=ts,
    )
    db_session.add(trade)
    db_session.commit()
    return trade


def make_order(
    user_id,
    symbol,
    exchange,
    action,
    qty,
    price_type,
    status="open",
    price=None,
    trigger_price=None,
    product="CNC",
    ts=PRE_EX,
    filled=0,
):
    order = SandboxOrders(
        orderid=f"ORD-{_uid()}",
        user_id=user_id,
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=qty,
        price=Decimal(str(price)) if price is not None else None,
        trigger_price=Decimal(str(trigger_price)) if trigger_price is not None else None,
        price_type=price_type,
        product=product,
        order_status=status,
        average_price=None,
        filled_quantity=filled,
        pending_quantity=qty if filled == 0 else qty - filled,
        margin_blocked=Decimal("0"),
        order_timestamp=ts,
        update_timestamp=ts,
    )
    db_session.add(order)
    db_session.commit()
    return order


def make_gtt(user_id, symbol, exchange, qty, trigger, price, last_price, ts=PRE_EX):
    gtt = SandboxGTT(
        gtt_id=f"GTT-{user_id}-{symbol}",
        user_id=user_id,
        trigger_type="single",
        symbol=symbol,
        exchange=exchange,
        last_price=Decimal(str(last_price)),
        gtt_status="active",
        margin_blocked=Decimal("0"),
        created_at=ts,
        updated_at=ts,
    )
    db_session.add(gtt)
    db_session.flush()
    leg = SandboxGTTLeg(
        gtt_id=gtt.gtt_id,
        leg_number=1,
        trigger_price=Decimal(str(trigger)),
        trigger_direction="below",
        action="SELL",
        quantity=qty,
        price=Decimal(str(price)),
        pricetype="LIMIT",
        product="CNC",
        leg_status="pending",
        leg_margin=Decimal("0"),
        created_at=ts,
        updated_at=ts,
    )
    db_session.add(leg)
    db_session.commit()
    return gtt, leg


def split_action(symbol="RELIANCE", exchange="NSE", **kw):
    from dataclasses import replace

    defaults = dict(
        notice_id="N-REL-SPLIT",
        symbol=symbol,
        exchange=exchange,
        action_type="split",
        ratio_from=1,
        ratio_to=5,
        effective_date=EFFECTIVE,
    )
    defaults.update(kw)
    return ca.CorporateAction(**defaults)


# --------------------------------------------------------------------------- #
# Math
# --------------------------------------------------------------------------- #


def test_split_and_bonus_factors():
    split = split_action()
    assert split.factor == Decimal("5")
    bonus = ca.CorporateAction(
        "N", "TCS", "BSE", "bonus", 1, 1, EFFECTIVE
    )  # 1 free per 1 held
    assert bonus.factor == Decimal("2")
    bonus_odd = ca.CorporateAction("N", "X", "NSE", "bonus", 10, 5, EFFECTIVE)  # 5 per 10
    assert bonus_odd.factor == Decimal("1.5")


def test_quantity_scales_and_rounds_half_up():
    assert ca.scale_quantity(100, Decimal("5")) == (500, False)
    assert ca.scale_quantity(-100, Decimal("5")) == (-500, False)
    # 33 * 1.5 = 49.5 -> rounds half up to 50 and flags the rounding
    assert ca.scale_quantity(33, Decimal("1.5")) == (50, True)


def test_price_divides_by_factor():
    assert ca.scale_price("90", Decimal("5")) == Decimal("18.00")
    assert ca.scale_price("89", Decimal("5")) == Decimal("17.80")
    assert ca.scale_price(None, Decimal("5")) is None


def test_cost_is_conserved():
    # 100 shares at 100 become 500 shares; total cost stays 10,000
    assert ca.cost_conserving_average(100, "100", 500) == Decimal("20.00")
    # 33 shares @ 100 total cost 3300, rounded to 50 shares -> 66 each
    assert ca.cost_conserving_average(33, "100", 50) == Decimal("66.00")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parse_notice_csv_basic():
    text = (
        "notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n"
        "CA-1,RELIANCE,NSE,split,1,5,2026-10-15\n"
    )
    actions = ca.parse_notice_csv(text)
    assert len(actions) == 1
    assert actions[0].notice_id == "CA-1"
    assert actions[0].symbol == "RELIANCE"
    assert actions[0].factor == Decimal("5")


def test_parse_notice_aliases_ratio_column_and_bonus():
    text = (
        "id,symbol,exchange,type,ratio,date\n"
        "CA-2,TCS,BSE,bonus,1:1,2026-10-15\n"
    )
    actions = ca.parse_notice_csv(text)
    assert actions[0].action_type == "bonus"
    assert actions[0].ratio_from == 1 and actions[0].ratio_to == 1
    assert actions[0].factor == Decimal("2")


def test_parse_derives_stable_notice_id_without_one():
    body = "RELIANCE,NSE,split,1,5,2026-10-15\n"
    header = "symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n"
    first = ca.parse_notice_csv(header + body)[0].notice_id
    second = ca.parse_notice_csv(header + body)[0].notice_id
    assert first == second and first.startswith("CA-")


def test_parse_rejects_bad_rows():
    with pytest.raises(ca.CorporateActionError, match="action_type"):
        ca.parse_notice_csv(
            "symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n"
            "REL,NSE,merger,1,5,2026-10-15\n"
        )
    with pytest.raises(ca.CorporateActionError, match="YYYY-MM-DD"):
        ca.parse_notice_csv(
            "symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n"
            "REL,NSE,split,1,5,15-10-2026\n"
        )
    with pytest.raises(ca.CorporateActionError, match="empty"):
        ca.parse_notice_csv("")


def test_parse_rejects_duplicate_line():
    text = (
        "notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n"
        "DUP,REL,NSE,split,1,5,2026-10-15\n"
        "DUP,REL,NSE,split,1,5,2026-10-15\n"
    )
    with pytest.raises(ca.CorporateActionError, match="duplicate"):
        ca.parse_notice_csv(text)


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def _seed_two_accounts():
    make_funds("alice")
    make_funds("bob")
    make_position("alice", "RELIANCE", "NSE", "CNC", 100, "100.00", ltp="22.00")
    make_holding("alice", "TCS", "BSE", 50, "4000.00", ltp="2050.00")
    make_position("bob", "RELIANCE", "NSE", "NRML", 200, "99.00", ltp="22.00")

    # resting SL order placed before the ex-date: must be re-priced/resized
    sl = make_order(
        "alice", "RELIANCE", "NSE", "SELL", 100, "SL", price=89, trigger_price=90
    )
    # limit order placed AFTER the ex-date at new terms: must be left alone
    post = make_order(
        "bob",
        "RELIANCE",
        "NSE",
        "BUY",
        500,
        "LIMIT",
        price="20.50",
        ts=POST_EX,
    )
    # historical complete order: prices are facts, never touched
    done = make_order(
        "alice",
        "RELIANCE",
        "NSE",
        "BUY",
        100,
        "LIMIT",
        status="complete",
        price=100,
        filled=100,
    )
    # historical trade before the ex-date stays recorded in old terms
    make_trade("alice", "RELIANCE", "NSE", "CNC", "BUY", 100, "100.00", PRE_EX)

    gtt, leg = make_gtt("alice", "RELIANCE", "NSE", 100, 90, 89, last_price=100)
    return sl, post, done, gtt, leg


def test_preview_is_readonly_and_shows_before_after():
    sl, post, done, gtt, leg = _seed_two_accounts()
    results = ca.preview_actions([split_action(), ca.CorporateAction(
        "N-TCS", "TCS", "BSE", "bonus", 1, 1, EFFECTIVE
    )])

    by_symbol = {r["symbol"]: r for r in results}
    rel = by_symbol["RELIANCE"]
    assert rel["already_applied"] is False
    assert rel["counts"]["accounts"] == 2
    assert rel["counts"]["positions"] == 2
    assert rel["counts"]["orders"] == 1  # the pre-ex SL; post-ex and complete excluded
    assert rel["counts"]["gtts"] == 1

    alice = next(p for p in rel["positions"] if p["user_id"] == "alice")
    assert alice["quantity_before"] == 100 and alice["quantity_after"] == 500
    assert alice["average_price_before"] == 100.0
    assert alice["average_price_after"] == 20.0
    assert alice["pnl_after"] == 1000.0  # (22 - 20) * 500

    order_row = rel["orders"][0]
    assert order_row["orderid"] == sl.orderid
    assert order_row["quantity_after"] == 500
    assert order_row["trigger_price_after"] == 18.0
    assert order_row["price_after"] == 17.8

    gtt_row = rel["gtts"][0]
    assert gtt_row["last_price_after"] == 20.0
    assert gtt_row["legs"][0]["trigger_price_after"] == 18.0
    assert gtt_row["legs"][0]["quantity_after"] == 500

    skip_ids = {s["id"] for s in rel["skipped"]}
    assert post.orderid in skip_ids

    tcs = by_symbol["TCS"]
    holding_row = tcs["holdings"][0]
    assert holding_row["quantity_before"] == 50 and holding_row["quantity_after"] == 100
    assert holding_row["average_price_after"] == 2000.0
    assert holding_row["pnl_after"] == 5000.0  # (2050 - 2000) * 100

    # Preview changed nothing
    db_session.refresh(sl)
    assert sl.trigger_price == Decimal("90.00") and sl.quantity == 100
    db_session.refresh(leg)
    assert leg.trigger_price == Decimal("90.00") and leg.quantity == 100
    assert SandboxCorporateAction.query.count() == 0


def test_apply_converts_all_books_and_leaves_history_alone():
    sl, post, done, gtt, leg = _seed_two_accounts()
    trade = SandboxTrades.query.first()

    results, summary = ca.apply_actions([split_action()], applied_by="alice")

    assert summary["newly_applied"] == 1
    assert summary["accounts_affected"] == 2
    assert summary["positions"] == 2
    assert summary["orders"] == 1
    assert summary["gtts"] == 1

    alice_pos = SandboxPositions.query.filter_by(
        user_id="alice", symbol="RELIANCE", exchange="NSE"
    ).first()
    assert alice_pos.quantity == 500
    assert alice_pos.average_price == Decimal("20.00")
    assert alice_pos.pnl == Decimal("1000.00")

    bob_pos = SandboxPositions.query.filter_by(
        user_id="bob", symbol="RELIANCE", exchange="NSE"
    ).first()
    assert bob_pos.quantity == 1000
    assert bob_pos.average_price == Decimal("19.80")

    db_session.refresh(sl)
    assert sl.quantity == 500 and sl.pending_quantity == 500
    assert sl.price == Decimal("17.80") and sl.trigger_price == Decimal("18.00")

    db_session.refresh(post)
    assert post.quantity == 500 and post.price == Decimal("20.50")  # untouched
    db_session.refresh(done)
    assert done.quantity == 100 and done.price == Decimal("100.00")  # untouched

    db_session.refresh(gtt)
    assert gtt.last_price == Decimal("20.00")
    db_session.refresh(leg)
    assert leg.quantity == 500
    assert leg.price == Decimal("17.80")
    assert leg.trigger_price == Decimal("18.00")

    # Historical trades and realised history are immutable
    db_session.refresh(trade)
    assert trade.quantity == 100 and trade.price == Decimal("100.00")

    ledger = SandboxCorporateAction.query.one()
    assert ledger.status == "applied"
    assert ledger.positions_adjusted == 2
    assert ledger.applied_by == "alice"


def test_reimporting_same_notice_changes_nothing():
    _seed_two_accounts()
    ca.apply_actions([split_action()], applied_by="alice")
    ledger_count_after_first = SandboxCorporateAction.query.count()

    # Second import: same books must not be converted again
    results, summary = ca.apply_actions([split_action()], applied_by="alice")
    assert summary["newly_applied"] == 0
    assert summary["already_applied"] == 1
    assert summary["positions"] == 0
    assert results[0]["already_applied"] is True
    assert SandboxCorporateAction.query.count() == ledger_count_after_first

    alice_pos = SandboxPositions.query.filter_by(
        user_id="alice", symbol="RELIANCE", exchange="NSE"
    ).first()
    assert alice_pos.quantity == 500 and alice_pos.average_price == Decimal("20.00")

    # Preview of an already-applied notice replays the stored result too
    preview = ca.preview_action(split_action())
    assert preview["already_applied"] is True
    assert preview["counts"]["positions"] == 2


def test_mixed_old_and_new_terms_book_rebuilds_from_trades():
    """Notice imported late: pre-ex 100@100 plus post-ex BUY 500@20 (F=5)."""
    make_funds("carol")
    # pre-ex lot
    make_trade("carol", "RELIANCE", "NSE", "CNC", "BUY", 100, "100.00", PRE_EX)
    # post-ex trade already in new terms
    make_trade("carol", "RELIANCE", "NSE", "CNC", "BUY", 500, "20.00", POST_EX)
    # current book = 600 @ 33.33 (weighted), ltp 22
    pos = make_position("carol", "RELIANCE", "NSE", "CNC", 600, "33.33", ltp="22.00")
    pos.created_at = POST_EX
    pos.updated_at = POST_EX
    db_session.commit()

    results, _ = ca.apply_actions([split_action()], applied_by="carol")
    row = results[0]["positions"][0]
    # 100 old shares -> 500 new, plus the 500 already-new = 1000 @ 20
    assert row["quantity_after"] == 1000
    assert row["average_price_after"] == 20.0
    db_session.refresh(pos)
    assert pos.quantity == 1000 and pos.average_price == Decimal("20.00")

    # Rebuild converted only pre-ex trade terms; post-ex trade stays as recorded
    post_trade = SandboxTrades.query.filter_by(trade_timestamp=POST_EX).one()
    assert post_trade.quantity == 500 and post_trade.price == Decimal("20.00")
    pre_trade = SandboxTrades.query.filter_by(trade_timestamp=PRE_EX).one()
    assert pre_trade.quantity == 100 and pre_trade.price == Decimal("100.00")


def test_bonus_odd_lot_flags_rounding():
    make_funds("dave")
    make_position("dave", "X", "NSE", "CNC", 33, "100.00", ltp="70.00")
    bonus = ca.CorporateAction("N-X", "X", "NSE", "bonus", 10, 5, EFFECTIVE)  # x1.5

    results, _ = ca.apply_actions([bonus], applied_by="dave")
    row = results[0]["positions"][0]
    assert row["quantity_before"] == 33
    assert row["quantity_after"] == 50  # 49.5 rounds half up
    assert row["rounded"] is True
    # total cost 3300 preserved across the rounded quantity
    assert row["average_price_after"] == 66.0


def test_future_notice_previews_but_cannot_be_applied():
    make_funds("alice")
    make_position("alice", "RELIANCE", "NSE", "CNC", 100, "100.00")
    future = split_action(effective_date=date(2099, 1, 1))

    # Preview is allowed (it changes nothing)
    preview = ca.preview_action(future)
    assert preview["counts"]["positions"] == 1

    with pytest.raises(ca.CorporateActionError, match="not in force"):
        ca.apply_actions([future], applied_by="alice")
    assert SandboxCorporateAction.query.count() == 0


def test_gtt_with_a_firing_leg_is_skipped_not_half_converted():
    make_funds("alice")
    gtt, leg = make_gtt("alice", "RELIANCE", "NSE", 100, 90, 89, last_price=100)
    leg.leg_status = "triggering"
    db_session.commit()

    results, summary = ca.apply_actions([split_action()], applied_by="alice")
    assert summary["gtts"] == 0
    assert any(s["id"] == gtt.gtt_id for s in results[0]["skipped"])
    db_session.refresh(gtt)
    assert gtt.last_price == Decimal("100.00")  # untouched


# --------------------------------------------------------------------------- #
# Blueprint
# --------------------------------------------------------------------------- #


@pytest.fixture
def sandbox_client(monkeypatch):
    """The sandbox blueprint with the session check lifted (fresh import)."""
    import utils.session as us

    monkeypatch.setattr(us, "check_session_validity", lambda f: f)
    sys.modules.pop("blueprints.sandbox", None)

    from flask import Flask

    import blueprints.sandbox as bp_module

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"
    app.register_blueprint(bp_module.sandbox_bp)

    @app.route("/login")
    def _login():
        return "login", 401

    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = "alice"
        yield client


CSV_NOTICE = (
    "notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date\r\n"
    "N-REL-SPLIT,RELIANCE,NSE,split,1,5,2026-10-15\r\n"
)


def test_preview_route_is_readonly(sandbox_client):
    make_funds("alice")
    make_position("alice", "RELIANCE", "NSE", "CNC", 100, "100.00", ltp="22.00")

    resp = sandbox_client.post(
        "/sandbox/api/corporate-actions/preview", json={"csv": CSV_NOTICE}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    action = body["data"]["actions"][0]
    assert action["positions"][0]["quantity_after"] == 500
    assert SandboxCorporateAction.query.count() == 0


def test_apply_then_reapply_route_is_idempotent(sandbox_client):
    make_funds("alice")
    make_position("alice", "RELIANCE", "NSE", "CNC", 100, "100.00", ltp="22.00")
    make_order("alice", "RELIANCE", "NSE", "SELL", 100, "SL", price=89, trigger_price=90)

    first = sandbox_client.post(
        "/sandbox/api/corporate-actions/apply", json={"csv": CSV_NOTICE}
    )
    assert first.status_code == 200
    assert first.get_json()["data"]["summary"]["newly_applied"] == 1

    second = sandbox_client.post(
        "/sandbox/api/corporate-actions/apply", json={"csv": CSV_NOTICE}
    )
    body = second.get_json()
    assert second.status_code == 200
    assert body["data"]["summary"]["newly_applied"] == 0
    assert body["data"]["actions"][0]["already_applied"] is True

    pos = SandboxPositions.query.filter_by(user_id="alice", symbol="RELIANCE").one()
    assert pos.quantity == 500 and pos.average_price == Decimal("20.00")


def test_apply_route_rejects_bad_payload(sandbox_client):
    resp = sandbox_client.post(
        "/sandbox/api/corporate-actions/preview", json={"csv": "not,a,real,header\n1,2,3,4"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
