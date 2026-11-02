"""
Tests for the cash flow Sankey (cash_flow.py): payee keys, card-payment and transfer
matching, role precedence, and that the diagram always balances. No DB, no network.
"""

from __future__ import annotations

from datetime import date, timedelta

import cash_flow as cf
from cash_flow import CardPayment, CardSpend, CashRow, Role

D = date(2026, 10, 1)
END = date(2026, 10, 31)
_ids = iter(range(1, 10000))


def r(day, amount, desc, acct=1):
    return CashRow(next(_ids), acct, D + timedelta(days=day), amount, desc)


def balanced(f):
    """Every middle node's money in equals its money out, within a cent per link."""
    for i, n in enumerate(f.nodes):
        if n["col"] in (1, 2):
            inn = sum(l["value"] for l in f.links if l["target"] == i)
            out = sum(l["value"] for l in f.links if l["source"] == i)
            if n["col"] == 1 or out:
                assert abs(inn - out) <= 0.02 * max(1, len(f.links)), (n, inn, out)


def test_payee_keys_group_spellings():
    k1, l1 = cf.payee("ACME CORP PAYROLL 102394 PPD ID: 999")
    k2, _ = cf.payee("ACME CORP  PAYROLL 556120 PPD ID: 999")
    assert k1 == k2 and "ACME" in l1.upper()


def test_card_payments_and_transfers_are_matched():
    rows = [r(3, -1500.00, "AMEX EPAYMENT ACH PMT"), r(5, -2000.00, "ONLINE TRANSFER TO SAVINGS", 1),
            r(6, 2000.00, "ONLINE TRANSFER FROM CHECKING", 2), r(7, -2000.00, "SOMETHING ELSE")]
    pairs = cf.detect_pairs(rows, [CardPayment(D + timedelta(days=5), 1500.00)])
    assert pairs[rows[0].id] == "card_payment"
    assert pairs[rows[1].id] == pairs[rows[2].id] == "transfer"
    assert rows[3].id not in pairs                       # the deposit is already the other half of one transfer


def test_role_precedence():
    row = r(1, -880.0, "ALLY AUTO LOAN PMT")
    assert cf.role_for(row, {}, {})[0].role == "debt"
    assert cf.role_for(row, {row.key: Role("spending", "Transportation")}, {})[1] == "you"
    assert cf.role_for(r(1, 50.0, "VENMO CASHOUT"), {}, {})[0].role == "income"      # deposits default to income
    assert cf.role_for(r(1, -50.0, "VENMO PAYMENT"), {}, {})[0] is None               # unsorted, not guessed
    assert cf.role_for(r(1, -50.0, "VANGUARD BUY"), {}, {})[0].role == "saving"
    assert cf.role_for(r(1, 75.0, "VANGUARD SELL"), {}, {})[0].role == "income"       # investing keyword is money out only


def test_build_balances_and_replaces_card_payments_with_card_spending():
    rows = [r(1, 9850.00, "ACME PAYROLL"), r(15, 9850.00, "ACME PAYROLL"), r(2, -2180.00, "NORTHWIND BANK MORTGAGE"),
            r(3, -3100.00, "CHASE CREDIT CRD AUTOPAY"), r(4, -1000.00, "VANGUARD BUY"),
            r(5, -900.00, "SUNNY DAYS CHILDCARE"), r(6, -120.00, "ODD PAYEE")]
    roles = {rows[5].key: Role("spending", "Education / Family")}
    spend = [CardSpend(D + timedelta(days=3), 2200.0, "Food & Beverage"), CardSpend(D + timedelta(days=9), 950.0, "Shopping"),
             CardSpend(D + timedelta(days=9), -50.0, "Shopping"), CardSpend(D - timedelta(days=3), 999.0, "Travel")]
    f = cf.build(rows, spend, [CardPayment(D + timedelta(days=5), 3100.00)], roles, D, END)
    t = f.totals
    assert t["income"] == 19700.0
    assert t["card_spending"] == 3100.0                  # charges in October only, refunds netted; payments not counted
    assert t["card_payments"] == 3100.0
    assert t["spending"] == 4000.0 and t["debt"] == 2180.0 and t["saving"] == 1000.0 and t["unsorted"] == 120.0
    assert t["net"] == 19700.0 - 4000.0 - 2180.0 - 1000.0 - 120.0
    names = {n["name"] for n in f.nodes}
    assert {"Food & Beverage", "Shopping", "Education / Family (cash)", "Added to cash", "Not sorted yet"} <= names
    assert "Travel" not in names
    assert f.unsorted == [{"key": rows[6].key, "label": rows[6].label, "count": 1, "total": 120.0}]
    balanced(f)


def test_drawn_from_cash_and_monthly_average():
    rows = [r(1, 3000.0, "ACME PAYROLL"), r(2, -5000.0, "NORTHWIND BANK MORTGAGE")]
    f = cf.build(rows, [], [], {}, D, END)
    assert f.totals["net"] == -2000.0
    assert any(n["key"] == "fromcash" for n in f.nodes) and f.nodes[[n["key"] for n in f.nodes].index("hub")]["name"] == "Money in and cash used"
    balanced(f)
    q = cf.build(rows, [], [], {}, D, END, months=2)
    assert q.totals["income"] == 1500.0


def test_details_fold_into_other():
    rows = [r(1, 100000.0, "ACME PAYROLL")]
    spend = [CardSpend(D, 100.0 * (i + 1), f"Cat {i}") for i in range(10)]
    f = cf.build(rows, spend, [], {}, D, END)
    details = [n for n in f.nodes if n["group"] == "spend" and n["col"] == 3]
    assert len(details) == cf.TOP_DETAILS + 1 and details[-1]["name"] == "Other spending"
    balanced(f)


def test_ignore_and_transfer_rows_stay_out():
    rows = [r(1, 5000.0, "ACME PAYROLL"), r(2, -300.0, "REIMBURSEMENT HOLD")]
    f = cf.build(rows, [], [], {rows[1].key: Role("ignore")}, D, END)
    assert f.totals["left_out"] == 300.0 and f.totals["net"] == 5000.0
    pair = [r(3, -1000.0, "ONLINE TRANSFER TO SAVINGS", 1), r(4, 1000.0, "ONLINE TRANSFER FROM CHECKING", 2)]
    assert cf.build(pair, [], [], {}, D, END).totals["transfers"] == 1000.0


def test_months_between():
    assert cf.months_between(date(2026, 8, 1), date(2026, 10, 31)) == 3.0
    assert cf.months_between(date(2025, 11, 1), date(2026, 10, 31)) == 12.0
    assert cf.months_between(date(2026, 8, 15), date(2026, 10, 14)) == 61 / 30.4375
    assert cf.months_between(D, D) == 1.0


# ── Assistant: cash flow lookup and payee-role proposals ─────────────────────

def _ask_data():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
    import ask_data
    from test_ask import fixture
    data = fixture()                                   # today 2026-10-24; card rows for six months
    data.cash_txns = []
    for i, (day, amt, desc) in enumerate([(2, 9850.0, "ACME PAYROLL PPD"), (3, -2180.0, "NORTHWIND BANK MORTGAGE"),
                                          (5, -900.0, "SUNNY DAYS CHILDCARE"), (6, -75.0, "JANE DOE TUTORING")]):
        row = CashRow(5000 + i, 7, date(2026, 9, day), amt, desc)
        data.cash_rows.append(row)
        data.cash_txns.append(ask_data.CashTxn(7, row.date, amt, data.safe(row.label), row.key))
    return data


def test_assistant_cash_flow_lookup_is_safe_and_balanced():
    import json
    import ask_data
    data = _ask_data()
    text, sources = ask_data.run("cash_flow", {"months": 1}, data)
    out = json.loads(text)
    assert out["period"]["from"] == "2026-09-01" and out["period"]["to"] == "2026-09-30"
    assert out["per_month"]["income"] == 9850.0 and out["per_month"]["debt"] == 2180.0
    payees = {u["payee"] for u in out["not_sorted_yet"]}
    assert "Sunny Days Childcare" in payees
    assert "jane" not in text.lower() and "doe" not in text.lower()        # the person's name is withheld
    assert sources[0]["nav"] == {"section": "dashboard", "tab": "cashflow"}


def test_payee_role_proposal():
    import ask_proposals as P
    data = _ask_data()
    _, card = P.run("propose_payee_role", {"payee": "Sunny Days Childcare", "role": "spending",
                                           "category": "Education / Family", "reason": "Childcare."}, data, P.State())
    key = cf.payee("SUNNY DAYS CHILDCARE")[0]
    assert card["apply"] == {"method": "PUT", "url": f"/api/cashflow/payees/{key}",
                             "body": {"role": "spending", "label": "Sunny Days Childcare", "category": "Education / Family",
                                      "target": ""}}
    assert "$900.00 out" in " ".join(card["lines"])
    for bad in ({"payee": "Nobody", "role": "spending", "category": ""},
                {"payee": "Sunny Days Childcare", "role": "debt", "category": "Travel"},
                {"payee": "Sunny Days Childcare", "role": "spending", "category": "Made up"},
                {"payee": ask_data_withheld(), "role": "spending", "category": ""}):
        try:
            P.run("propose_payee_role", {**bad, "reason": ""}, data, P.State())
            assert False, bad
        except P.ProposalError:
            pass


def ask_data_withheld():
    import ask_data
    return ask_data.WITHHELD


# ── Transfers that go nowhere Flow can see ───────────────────────────────────

def test_unmatched_outgoing_transfer_is_left_for_the_user():
    rows = [r(1, 10000.0, "ACME PAYROLL"), r(3, -4000.0, "ONLINE TRANSFER REF IB0X TO XXXXXX1234"),
            r(4, -1000.0, "ONLINE TRANSFER TO SAVINGS", 1), r(5, 1000.0, "ONLINE TRANSFER FROM CHECKING", 2)]
    role, why = cf.role_for(rows[1], {}, cf.detect_pairs(rows, []))
    assert role is None and why == "unmatched_transfer"
    f = cf.build(rows, [], [], {}, D, END)
    assert f.totals["transfers"] == 1000.0                       # only the matched pair is a transfer
    assert f.totals["unsorted"] == 4000.0 and f.unsorted[0]["total"] == 4000.0
    # Once the user says where it goes, it counts as saving.
    f = cf.build(rows, [], [], {rows[1].key: Role("saving", target="Brokerage")}, D, END)
    assert f.totals["saving"] == 4000.0 and f.totals["unsorted"] == 0.0
    # A transfer whose description names an investment account is suggested as saving.
    assert cf.role_for(r(3, -500.0, "ONLINE TRANSFER TO BROKERAGE"), {}, {})[0].role == "saving"
    # An incoming "transfer" that matched nothing stays out of income.
    assert cf.role_for(r(6, 500.0, "TRANSFER FROM BROKERAGE"), {}, {})[0].role == "transfer"


# ── Drill-in: every bar is the sum of its transactions ───────────────────────

def test_tagged_transactions_add_up_to_each_bar():
    rows = [r(1, 9850.0, "ACME PAYROLL"), r(2, -2180.0, "NORTHWIND BANK MORTGAGE"), r(5, -900.0, "SUNNY DAYS CHILDCARE"),
            r(6, 120.0, "SUNNY DAYS CHILDCARE REFUND"), r(7, -60.0, "ODD PAYEE")]
    roles = {rows[2].key: Role("spending", "Education / Family"), rows[3].key: Role("spending", "Education / Family")}
    spend = [CardSpend(D + timedelta(days=i), 10.0 * (i + 1), f"Cat {i % 9}", id=100 + i, sub=f"Sub {i % 2}",
                       merchant=f"M{i}", card_id=3) for i in range(30)]
    f = cf.build(rows, spend, [], roles, D, END)
    entries, _ = cf.tag(rows, spend, [], roles, D, END)
    for n in f.nodes:
        if n["col"] in (0, 3) and n["group"] != "cash":
            total = sum(e.amount for e in entries if e.group == n["group"] and e.detail in n["details"])
            shown = sum(l["value"] for l in f.links if f.nodes[l["target"]] is n or f.nodes[l["source"]] is n)
            assert abs(total - shown) < 0.01, (n["name"], total, shown)
    other = next(n for n in f.nodes if n["name"] == "Other spending")
    assert len(other["details"]) == 10 - cf.TOP_DETAILS - 0 and all(d.startswith("Cat") for d in other["details"])
    cash = next(e for e in entries if e.id == rows[3].id)
    assert cash.group == "spend" and cash.amount == -120.0 and cash.detail == "Education / Family (cash)"
    card = next(e for e in entries if e.source == "card")
    assert card.sub.startswith("Sub") and card.label.startswith("M") and card.account_id == 3
    unsorted = [e for e in entries if e.group == "unsorted"]
    assert [(e.label, e.amount) for e in unsorted] == [(rows[4].label, 60.0)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
