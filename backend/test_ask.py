"""
Tests for Ask: grounding, the number check, lookup output, privacy, budgets,
topic control, history validation, the monthly count and request logging.

Claude is a fake that plays a scripted list of rounds (text and tool calls).
No database or network. Runnable directly (``python3 test_ask.py``) or via pytest.
"""

from __future__ import annotations

import json
import os
import types
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects
os.environ.setdefault("SECRET_KEY", "test-" * 8)                                     # auth import (router tests)

import ask  # noqa: E402
import ask_data  # noqa: E402
import card_ai  # noqa: E402
import balance_sheet  # noqa: E402
from ask_data import AskData, BalanceAccount, Card, CashAccount, CashTxn, Upcoming  # noqa: E402
from card_analytics import Row  # noqa: E402

NS = types.SimpleNamespace
TODAY = date(2026, 10, 24)
_ids = iter(range(1, 100000))


def row(d, amount, merchant, l1, l2, l3=None, card=1, txn_type="purchase", subtype=""):
    is_spend = txn_type in ("purchase", "refund", "benefit_credit", "card_fee")
    return Row(id=next(_ids), card_account_id=card, card={1: "Travel Rewards Card", 2: "Everyday Cash Back"}[card],
               date=d, amount=amount, txn_type=txn_type, txn_subtype=subtype, is_spend=is_spend,
               canonical_merchant=merchant, merchant_family=merchant, cat_l1=l1, cat_l2=l2, cat_l3=l3 or l2,
               raw_description=f"{merchant.upper()} #4471 OAK GROVE IL JANE DOE")


def fixture() -> AskData:
    rows = []
    for i in range(24):            # Kroger: weekly, bigger in the last three months
        d = TODAY - timedelta(days=2 + 7 * i)
        rows.append(row(d, -(120.0 if i < 13 else 70.0), "Kroger", "Food & Beverage", "Groceries", "Traditional grocery"))
    for i in range(6):
        rows.append(row(TODAY - timedelta(days=5 + 30 * i), -22.99, "Netflix", "Digital & Media", "Streaming", card=2))
        rows.append(row(TODAY - timedelta(days=9 + 30 * i), -38.50, "DoorDash", "Food & Beverage", "Dining", "Delivery"))
    rows.append(row(TODAY - timedelta(days=40), -6.12, "Foreign fee", "Financial / Investment", "Fees",
                    txn_type="card_fee", subtype="foreign transaction fee"))
    recurring = [{"merchant": "Netflix", "card_account_id": 2, "card": "Everyday Cash Back", "cat_l1": "Digital & Media",
                  "cat_l2": "Streaming", "cat_l3": "Streaming", "kind": "subscription", "first_charge": "2026-05-08",
                  "most_recent_charge": "2026-10-19", "occurrences": 6, "typical_amount": 22.99,
                  "monthly_equivalent": 22.99, "cadence": "monthly", "change_detected": "price change 15.49 -> 22.99",
                  "notes": "", "verdict": None}]
    cash = [CashAccount(7, "Joint Checking", "Account A")]
    cash_txns = [CashTxn(7, date.min, 1000.0, "Opening balance"),
                 CashTxn(7, TODAY - timedelta(days=10), 4200.0, "Acme Payroll"),
                 CashTxn(7, TODAY - timedelta(days=8), -1850.0, "Mortgage"),
                 CashTxn(7, TODAY - timedelta(days=3), -60.0, ask_data.WITHHELD)]
    upcoming = [Upcoming(7, "Mortgage", -1850.0, TODAY + timedelta(days=22), "monthly")]
    # The balance sheet: July, August and October 2026 (no September at all), the brokerage not updated in
    # October (carried), one ignored account. Liabilities are stored negative.
    periods = [date(2026, 7, 1), date(2026, 8, 1), date(2026, 10, 1)]
    balance_accounts = [BalanceAccount(1, "Brokerage at Example Bank", "Asset A", "asset"),
                        BalanceAccount(2, "Example Home", "Asset B", "asset"),
                        BalanceAccount(3, "Example Mortgage", "Liability A", "liability"),
                        BalanceAccount(4, "Old Savings", "Asset C", "asset", ignored=True)]
    raw = {1: [50000.0, 52000.0, None], 2: [300000.0, 300000.0, 300000.0],
           3: [-200000.0, -199000.0, -198000.0], 4: [5000.0, 5000.0, 5000.0]}
    matrix = {aid: balance_sheet.carry_forward(vals) for aid, vals in raw.items()}
    return AskData(today=TODAY, cards=[Card(1, "Travel Rewards Card", "Card A", TODAY - timedelta(days=200), TODAY - timedelta(days=1)),
                                       Card(2, "Everyday Cash Back", "Card B", TODAY - timedelta(days=200), TODAY - timedelta(days=1))],
                   rows=rows, recurring=recurring, cash=cash, cash_txns=cash_txns, upcoming=upcoming,
                   names=["Jane Doe"], balance_accounts=balance_accounts, balance_periods=periods, balance_matrix=matrix)


# ── Fake Claude ──────────────────────────────────────────────────────────────

def text(t):
    return NS(type="text", text=t)


def tool(name, inp, tid=None):
    return NS(type="tool_use", id=tid or f"tu_{name}_{next(_ids)}", name=name, input=inp)


def msg(*blocks, stop=None, inp=1000, out=200):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return NS(content=list(blocks), stop_reason=stop, model="claude-opus-5-5",
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeStream:
    def __init__(self, m):
        self.m = m

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.m.content:
            if b.type == "text":
                yield NS(type="text", text=b.text)

    def get_final_message(self):
        return self.m


class FakeMessages:
    def __init__(self, rounds, relevant=True):
        self.rounds, self.relevant, self.calls, self.parse_calls = list(rounds), relevant, [], []

    def stream(self, **kw):
        self.calls.append(json.loads(json.dumps(card_ai._jsonable(kw))))
        return FakeStream(self.rounds.pop(0))

    def parse(self, **kw):
        self.parse_calls.append(kw)
        if isinstance(self.relevant, Exception):
            raise self.relevant
        return NS(stop_reason="end_turn", parsed_output=ask.TopicVerdict(relevant=self.relevant), model="claude-opus-5-5",
                  usage=NS(input_tokens=200, output_tokens=5, cache_read_input_tokens=0))


def client(rounds, relevant=True):
    return NS(beta=NS(messages=FakeMessages(rounds, relevant)))


def run(rounds, question="Where can I trim spending?", page=None, data=None, history=None):
    c, logged = client(rounds), []
    hist = ask.validate_history((history or []) + [{"role": "user", "text": question}])
    events = list(ask.answer(c, data or fixture(), hist, page, lambda *a: logged.append(a)))
    return events, c.beta.messages, logged


def final(events):
    return next(e for e in events if e["type"] in ("final", "declined", "error"))


# ── Answers, index and sources ───────────────────────────────────────────────

def test_answer_from_lookup_with_app_sources_and_real_names():
    events, fake, logged = run([
        msg(text("Let me check."), tool("subscriptions", {"include_habitual": False})),
        msg(text("**Subscriptions: $22.99/mo.** Netflix on Card B went up from $15.49 to $22.99.")),
    ])
    types_ = [e["type"] for e in events]
    assert types_ == ["text", "reset", "status", "text", "final"]
    f = final(events)
    assert "Everyday Cash Back" in f["text"] and "Card B" not in f["text"]
    assert f["sources"] == [{"label": "Subscriptions", "nav": {"section": "cards", "tab": "subscriptions"}}]
    assert f["unverified"] == []
    # Round 2 sends round 1's content back unchanged and all results in one user message.
    second = fake.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][1]["type"] == "tool_use"
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result"
    assert len(logged) == 2 and all(entry[0] == "ask" for entry in logged)


def test_index_has_page_and_trim_list_in_app_order():
    _, fake, _ = run([msg(text("Subscriptions first."))], page={"section": "cards", "tab": "monthly", "card_id": 2})
    index = json.loads(fake.calls[0]["system"][1]["text"].split("\n", 1)[1])
    assert index["page"] == {"section": "cards", "tab": "monthly", "card": "Card B"}
    kinds = [t["kind"] for t in index["trim_items"]]
    assert set(kinds[:3]) == {"subscriptions", "delivery", "card_fees"}       # the easy ones lead
    assert "growing_merchant" in kinds and "dining_out" not in kinds
    assert [t["rank"] for t in index["trim_items"]] == list(range(1, len(kinds) + 1))
    eases = [ask_data.EASE_ORDER[t["ease"]] for t in index["trim_items"]]
    assert eases == sorted(eases)
    grow = next(t for t in index["trim_items"] if t["kind"] == "growing_merchant")
    assert grow["merchant"] == "Kroger" and grow["change_pct"] > 20


def test_unknown_page_ids_are_dropped():
    data = fixture()
    assert ask_data.page_context(data, {"section": "cards", "tab": "categories", "category": "Food & Beverage"})["category"] == "Food & Beverage"
    assert "category" not in ask_data.page_context(data, {"section": "cards", "tab": "categories", "category": "<script>"})
    assert ask_data.page_context(data, {"section": "cards", "tab": "merchants", "merchant": "Kroger"})["merchant"] == "Kroger"
    assert "merchant" not in ask_data.page_context(data, {"section": "cards", "tab": "merchants", "merchant": "Nobody"})
    assert ask_data.page_context(data, {"section": "cashflow", "account_id": 999}) == {"section": "cashflow", "account": None}
    assert ask_data.page_context(data, {"section": "evil"})["section"] == "unknown"


# ── Numbers and lookup output ────────────────────────────────────────────────

def test_made_up_number_is_flagged():
    events, _, _ = run([msg(text("Netflix costs $22.99, and you could save 999.9 a year."))])
    assert final(events)["unverified"] == ["999.9"]


def test_whole_dollar_quotes_and_iso_date_parts_pass():
    known = ask.known_numbers(['{"total": 653.3, "last": "2026-10-19", "count": 48}'])
    assert ask.unverified_numbers("About $653 (653.30) on Oct 19, 2026 over 48 trips, top 3", known) == []
    assert ask.unverified_numbers("Spent $1,140 in 31 orders", known) == ["1,140", "31"]


def test_lookup_output_has_no_links_and_page_precision():
    data = fixture()
    for name, args in [("trim_details", {"rank": 1}), ("spending_breakdown",
                        {"group_by": "merchant", "months": 6, "category": "Groceries", "merchant": "", "card": ""}),
                       ("subscriptions", {"include_habitual": True}), ("cash_summary", {"account": "", "months": 3}),
                       ("net_worth", {"months": 0, "account": ""}), ("net_worth", {"months": 2, "account": "Asset A"})]:
        out, sources = ask_data.run(name, args, data)
        assert '"nav"' not in out and "http" not in out and sources
        for v in _numbers(json.loads(out)):
            assert round(v, 2) == v, (name, v)


def _numbers(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, float) and k.endswith("_pct"):
                assert round(v, 1) == v
            yield from _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _numbers(v)
    elif isinstance(obj, float):
        yield obj


def test_breakdown_shares_and_previous_period():
    out, sources = ask_data.run("spending_breakdown", {"group_by": "merchant", "months": 3, "category": "Groceries",
                                                       "merchant": "", "card": ""}, fixture())
    r = json.loads(out)
    top = r["rows"][0]
    assert top["group"] == "Kroger" and top["share_pct"] == 100.0 and top["previous_period_total"] > 0
    assert sources[0]["nav"] == {"section": "cards", "tab": "transactions", "l1": "Food & Beverage", "l2": "Groceries"}


def test_find_transactions_caps_rows_and_counts_all():
    out, _ = ask_data.run("find_card_transactions", {"merchant": "kroger", "category": "", "card": "", "start": "",
                                                     "end": "", "min_amount": 0, "max_amount": 0, "limit": 100}, fixture())
    r = json.loads(out)
    assert r["match_count"] == 24 and r["shown"] == 24 and len(r["transactions"]) == 24
    out, _ = ask_data.run("find_card_transactions", {"merchant": "", "category": "", "card": "Card A", "start": "",
                                                     "end": "", "min_amount": 100, "max_amount": 0, "limit": 5}, fixture())
    assert json.loads(out)["shown"] == 5


def test_net_worth_matches_the_dashboard():
    # The Dashboard chart: balance-sheet months lined up with the cash register's, cash counted as an asset,
    # ignored accounts left out, a missing month carried. Cash (3,290) has rows only in October.
    out, sources = ask_data.run("net_worth", {"months": 0, "account": ""}, fixture())
    r = json.loads(out)
    assert [m["month"] for m in r["by_month"]] == ["2026-07", "2026-08", "2026-10"]
    assert r["by_month"][0] == {"month": "2026-07", "assets": 350000.0, "liabilities": -200000.0, "net_worth": 150000.0,
                                "carried_accounts": 0}
    assert r["by_month"][-1] == {"month": "2026-10", "assets": 355290.0, "liabilities": -198000.0, "net_worth": 157290.0,
                                 "carried_accounts": 1}
    assert r["as_of"] == "2026-10" and r["net_worth"] == 157290.0 and r["cash_included"] is True
    assert r["change"] == {"net_worth": 7290.0, "net_worth_pct": 4.9, "assets": 5290.0, "liabilities": 2000.0}
    by_alias = {a["account"]: a for a in r["accounts"]}
    assert by_alias["Asset A"]["value"] == 52000.0 and by_alias["Asset A"]["carried"] is True
    assert by_alias["Asset A"]["change"] == 2000.0 and by_alias["Asset A"]["data_to"] == "2026-10"
    assert by_alias["Asset C"]["ignored"] is True and by_alias["Liability A"]["change"] == 2000.0
    assert by_alias["Account A"] == {"account": "Account A", "type": "cash", "value": 3290.0, "change": None,
                                     "carried": False, "ignored": False, "data_from": "2026-10", "data_to": "2026-10"}
    assert sources == [{"label": "Dashboard: Net worth", "nav": {"section": "dashboard", "tab": "overview"}}]
    # A window of the last two months, and one account's own history.
    r2 = json.loads(ask_data.run("net_worth", {"months": 2, "account": ""}, fixture())[0])
    assert r2["window"] == {"from": "2026-08", "to": "2026-10", "months": 2} and r2["change"]["net_worth"] == 157290.0 - 153000.0
    out, sources = ask_data.run("net_worth", {"months": 0, "account": "liability a"}, fixture())
    r3 = json.loads(out)
    assert r3["account"] == "Liability A" and r3["type"] == "liability" and r3["value"] == -198000.0
    assert [m["value"] for m in r3["by_month"]] == [-200000.0, -199000.0, -198000.0]
    assert sources == [{"label": "Dashboard: Example Mortgage", "nav": {"section": "dashboard", "tab": "liabilities"}}]
    try:
        ask_data.run("net_worth", {"months": 0, "account": "Asset Z"}, fixture())
        assert False, "unknown alias"
    except ask_data.LookupError_ as exc:
        assert "Asset A" in str(exc) and "Account A" in str(exc)
    empty = fixture()
    empty.balance_accounts, empty.balance_periods, empty.balance_matrix, empty.cash, empty.cash_txns = [], [], {}, [], []
    try:
        ask_data.run("net_worth", {"months": 0, "account": ""}, empty)
        assert False, "no balance sheet"
    except ask_data.LookupError_ as exc:
        assert "no account balances" in str(exc)


def test_index_net_worth_block_is_the_lookups_latest_month():
    _, fake, _ = run([msg(text("ok"))], page={"section": "dashboard", "tab": "assets"})
    index = json.loads(fake.calls[0]["system"][1]["text"].split("\n", 1)[1])
    assert index["page"] == {"section": "dashboard", "tab": "assets"}
    nw = index["net_worth"]
    assert nw["as_of"] == "2026-10" and nw["net_worth"] == 157290.0 and nw["change_last_month"] == 157290.0 - 153000.0
    assert nw["data_from"] == "2026-07" and nw["balance_accounts"] == 4 and nw["lookup"] == "net_worth"
    empty = fixture()
    empty.balance_accounts, empty.balance_periods, empty.balance_matrix, empty.cash, empty.cash_txns = [], [], {}, [], []
    assert ask_data.net_worth_index(empty) == {"balance_accounts": 0, "cash_accounts_included": 0, "lookup": "net_worth"}
    assert ask_data.page_context(empty, {"section": "dashboard", "tab": "evil"})["tab"] == "overview"


def test_bad_lookup_input_is_an_error_result_not_a_crash():
    events, fake, _ = run([
        msg(tool("spending_breakdown", {"group_by": "merchant", "months": 6, "category": "", "merchant": "", "card": "Card Z"})),
        msg(text("I could not find that card.")),
    ])
    res = fake.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "Card Z" in res["content"]
    assert final(events)["type"] == "final"


# ── Privacy ──────────────────────────────────────────────────────────────────

def test_nothing_identifying_is_sent():
    events, fake, _ = run([
        msg(tool("find_card_transactions", {"merchant": "kroger", "category": "", "card": "", "start": "", "end": "",
                                            "min_amount": 0, "max_amount": 0, "limit": 25}),
            tool("find_cash_transactions", {"account": "", "payee": "", "start": "", "end": "", "limit": 25}),
            tool("cash_summary", {"account": "Account A", "months": 6}),
            tool("net_worth", {"months": 0, "account": ""})),
        msg(text("Done.")),
    ], page={"section": "cashflow", "account_id": 7})
    sent = json.dumps(fake.calls)
    for secret in ("Travel Rewards Card", "Everyday Cash Back", "Joint Checking", "JANE", "Jane Doe", "#4471", "OAK GROVE",
                   "Brokerage", "Example Bank", "Example Home", "Example Mortgage", "Old Savings"):
        assert secret not in sent, secret
    assert "Card A" in sent and "Account A" in sent and "Asset A" in sent and "Liability A" in sent
    assert ask_data.WITHHELD in sent
    labels = [s["label"] for s in final(events)["sources"]]
    assert "Cash Flow: Joint Checking" in labels and labels[-1] == "Dashboard: Net worth"   # the user sees real names


def test_person_to_person_payees_keep_only_the_service():
    data = fixture()
    assert data.safe("Zelle To Jane Smith") == "Zelle transfer"
    assert ask_data.p2p_label("VENMO *BOB JONES") == "Venmo transfer"
    assert ask_data.p2p_label("ONLINE TRANSFER TO SAVINGS XXXX1234") == "Transfer"
    assert ask_data.p2p_label("PAYPAL *STEAM GAMES") is None and data.safe("Kroger") == "Kroger"
    assert data.safe("Jane Doe Consulting") == ask_data.WITHHELD          # a known name


def test_private_subcategories_are_masked_in_lookups():
    import card_taxonomy as T
    T.set_custom([T.Custom(1, "Food & Beverage", "Emma's allergy food", ai_visible=False)])
    try:
        data = fixture()
        for r in data.rows:
            if r.canonical_merchant == "Kroger":
                r.cat_l2 = r.cat_l3 = "Emma's allergy food"
        _, fake, _ = run([
            msg(tool("spending_breakdown", {"group_by": "subcategory", "months": 0, "category": "", "merchant": "", "card": ""}),
                tool("find_card_transactions", {"merchant": "kroger", "category": "", "card": "", "start": "", "end": "",
                                                "min_amount": 0, "max_amount": 0, "limit": 5})),
            msg(text("Done.")),
        ], data=data)
        sent = json.dumps(fake.calls)
        assert "Emma" not in sent and T.PRIVATE_L2 in sent
    finally:
        T.set_custom([])


def test_real_names_in_history_go_back_to_aliases():
    _, fake, _ = run([msg(text("Yes."))], question="And on my everyday cash back?",
                     history=[{"role": "user", "text": "Kroger spend?"},
                              {"role": "assistant", "text": "Mostly on Travel Rewards Card; Joint Checking paid it."}])
    sent = json.dumps(fake.calls[0]["messages"])
    assert "Travel Rewards Card" not in sent and "Joint Checking" not in sent and "everyday cash" not in sent.lower()
    assert "Mostly on Card A; Account A paid it." in sent and "And on my Card B?" in sent


def test_balance_sheet_names_alias_both_ways():
    data = fixture()
    assert ask_data.realias("Is the Example Mortgage shrinking? And Brokerage at Example Bank?", data) == \
        "Is the Liability A shrinking? And Asset A?"
    m = data.alias_map()
    assert ask_data.unalias("Asset A grew; Assets A and B together; Liability A fell.", m) == \
        "Brokerage at Example Bank grew; Brokerage at Example Bank and Example Home together; Example Mortgage fell."
    events, _, _ = run([msg(text("Liability A is down to $198,000."))])
    assert final(events)["text"] == "Example Mortgage is down to $198,000." and final(events)["unverified"] == []


# ── Budget ───────────────────────────────────────────────────────────────────

def _tool_round():
    return msg(tool("subscriptions", {"include_habitual": False}))


def test_last_round_has_lookups_off():
    events, fake, _ = run([_tool_round() for _ in range(4)] + [msg(text("Netflix is $22.99."))])
    choices = [c["tool_choice"]["type"] for c in fake.calls]
    assert choices == ["auto", "auto", "auto", "auto", "none"]
    assert final(events)["type"] == "final"


def test_lookup_after_lookups_off_is_an_error():
    events, _, _ = run([_tool_round() for _ in range(5)])
    assert final(events)["type"] == "error"


def test_big_token_use_turns_lookups_off_early():
    events, fake, _ = run([msg(tool("subscriptions", {"include_habitual": False}), inp=130_000), msg(text("Done."))])
    assert [c["tool_choice"]["type"] for c in fake.calls] == ["auto", "none"]


def test_extra_lookups_in_a_round_get_an_error():
    events, fake, _ = run([msg(*[tool("subscriptions", {"include_habitual": False}) for _ in range(6)]), msg(text("Done."))])
    results = fake.calls[1]["messages"][-1]["content"]
    assert len(results) == 6 and sum(1 for r in results if r.get("is_error")) == 2
    assert sum(1 for e in events if e["type"] == "status") == 4


def test_oversized_lookup_is_trimmed_or_refused():
    big = {"rows": [{"group": f"m{i}", "total": 1.0} for i in range(5000)]}
    out = json.loads(ask_data.fit(big, limit=20_000))
    assert len(out["rows"]) < 5000 and "shortened" in out["note"]
    try:
        ask_data.fit({"blob": "x" * 50_000}, limit=1000)
        assert False, "should refuse"
    except ask_data.LookupError_ as exc:
        assert "narrower" in str(exc)


def test_truncated_answer_is_marked():
    events, _, _ = run([msg(text("Your biggest win is"), stop="max_tokens")])
    assert final(events)["text"].endswith(ask.CUT_SHORT)


def test_every_round_sends_cache_control_effort_and_fallback():
    _, fake, _ = run([msg(text("ok"))])
    c = fake.calls[0]
    assert c["cache_control"] == {"type": "ephemeral"} and c["output_config"] == {"effort": "medium"}
    assert c["fallbacks"] == "default" and c["max_tokens"] == ask.ROUND_MAX_TOKENS
    assert all(t["input_schema"]["additionalProperties"] is False for t in c["tools"])
    lookups = {t["name"] for t in ask_data.TOOLS}
    assert all(t.get("strict") is (True if t["name"] in lookups else None) for t in c["tools"])


# Anthropic compiles every strict schema in a request into one grammar, with published limits
# (20 strict tools, 24 optional parameters, 16 union-type parameters) and an unpublished size
# limit that 16 strict tools exceeded ("The compiled grammar is too large"). Lookups stay strict;
# proposals are checked in code (ask_proposals.checked_args). Keep well inside the budget.
STRICT_TOOL_LIMIT = 10
STRICT_SCHEMA_BUDGET = 3000        # characters of JSON across every strict input_schema


def test_strict_tools_stay_inside_the_grammar_budget():
    import ask_proposals
    tools = ask_data.TOOLS + ask_proposals.TOOLS
    strict = [t for t in tools if t.get("strict")]
    size = sum(len(json.dumps(t["input_schema"])) for t in strict)
    assert len(strict) <= STRICT_TOOL_LIMIT, (
        f"{len(strict)} strict tools: the API's compiled-grammar limit is shared by all of them. "
        "Make the new tool non-strict and check its input in code, or simplify.")
    assert size <= STRICT_SCHEMA_BUDGET, (
        f"Strict schemas total {size} characters (budget {STRICT_SCHEMA_BUDGET}): make a tool non-strict or simplify.")
    for t in strict:
        assert all(n not in json.dumps(t["input_schema"]) for n in ("anyOf", '"type": ['))      # union types are costly
        assert set(t["input_schema"]["required"]) == set(t["input_schema"]["properties"])     # no optional parameters


def test_non_strict_proposals_check_their_input():
    import ask_proposals as P
    data = fixture()
    base = {"category": "Entertainment", "name": "Team support", "counts_as": "discretionary",
            "detection": "subscription", "rewards": "general", "private": False, "reason": "x"}
    # Enum values are matched case-insensitively and normalised.
    _, card = P.run("propose_new_subcategory", {**base, "counts_as": "Discretionary", "rewards": "GENERAL"}, data, P.State())
    assert card["apply"]["body"]["fixedness"] == "discretionary" and card["apply"]["body"]["rewards"] == "general"
    for bad in ({"rewards": "points"}, {"category": "Made up"}, {"detection": None}, {"counts_as": 3}):
        try:
            P.run("propose_new_subcategory", {**base, **bad}, data, P.State())
            assert False, bad
        except P.ProposalError:
            pass
    rec = {"name": "Netflix Premium", "merchant": "Netflix", "amount_min": "22", "amount_max": 24,
           "cadence": "Monthly", "kind": "subscription", "reason": "x"}
    _, card = P.run("propose_recurring", rec, data, P.State())
    assert card["apply"]["body"]["cadence"] == "monthly" and card["apply"]["body"]["amount_min"] == 22.0
    for bad in ({"kind": "whatever"}, {"cadence": "fortnightly"}, {"amount_min": "lots"}):
        try:
            P.run("propose_recurring", {**rec, **bad}, data, P.State())
            assert False, bad
        except P.ProposalError:
            pass
    rule = {"applies_to": "everything", "merchant": "Kroger", "scope_category": "", "scope_subcategory": "",
            "amount_from": 0, "amount_under": 20, "category": "Food & Beverage", "subcategory": "Groceries",
            "detail": "", "reason": "x"}
    try:
        P.run("propose_amount_rule", rule, data, P.State())
        assert False
    except P.ProposalError as exc:
        assert "applies_to" in str(exc)
    try:   # a missing field is an error to correct, not a crash
        P.run("propose_merchant_category", {"merchant": "Netflix"}, data, P.State())
        assert False
    except P.ProposalError:
        pass


# ── Topic ────────────────────────────────────────────────────────────────────

def test_topic_gate_payload_and_verdict():
    logged = []
    c = client([], relevant=False)
    assert ask.topic_gate(c, "Write me a poem", "Earlier answer " * 100, ["Travel"], lambda *a: logged.append(a)) is False
    kw = c.beta.messages.parse_calls[0]
    payload = json.loads(kw["messages"][0]["content"])
    assert len(payload["previous_answer"]) == 600 and kw["output_config"] == {"effort": "low"} and kw["max_tokens"] == 1024
    assert logged[0][0] == "ask_topic" and logged[0][2] is not None


def test_gate_failure_returns_none_and_backstop_declines():
    c = client([msg(text("OFF_TOPIC"))], relevant=RuntimeError("down"))
    logged = []
    assert ask.topic_gate(c, "Who won the game?", "", [], lambda *a: logged.append(a)) is None
    assert logged[0][3] is not None                    # the failure is logged too
    hist = ask.validate_history([{"role": "user", "text": "Who won the game?"}])
    events = list(ask.answer(c, fixture(), hist, None, lambda *a: None))
    assert final(events)["type"] == "declined"


# ── History ──────────────────────────────────────────────────────────────────

def _bad(raw):
    try:
        ask.validate_history(raw)
    except ask.AskError:
        return True
    return False


def test_history_validation():
    assert _bad([{"role": "user", "text": "a"}, {"role": "user", "text": "b"}])
    assert _bad([{"role": "user", "text": "   "}])
    assert _bad([{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}])
    assert _bad("not a list") and _bad([]) and _bad([{"role": "system", "text": "x"}])
    h = ask.validate_history([{"role": "assistant", "text": "hi"}, {"role": "user", "text": "q" * 5000}])
    assert h == [{"role": "user", "text": "q" * 1000}]
    long = []
    for i in range(6):
        long += [{"role": "user", "text": f"q{i}"}, {"role": "assistant", "text": f"a{i}"}]
    h = ask.validate_history(long + [{"role": "user", "text": "now"}])
    assert len(h) <= 8 and h[0]["role"] == "user" and h[-1]["text"] == "now"


def test_earlier_answers_are_sent_as_plain_text():
    _, fake, _ = run([msg(text("Yes."))], question="And last month?",
                     history=[{"role": "user", "text": "Spend on Kroger?"}, {"role": "assistant", "text": "$120 weekly."}])
    assert fake.calls[0]["messages"][1] == {"role": "assistant", "content": "$120 weekly."}


# ── Proposals ────────────────────────────────────────────────────────────────

def _new_sub(name="Team support", l1="Entertainment", private=False):
    return tool("propose_new_subcategory", {"category": l1, "name": name, "counts_as": "discretionary",
                                            "detection": "subscription", "rewards": "general", "private": private,
                                            "reason": "Fan support, not charity."})


def test_proposals_are_cards_not_changes():
    events, fake, _ = run([
        msg(_new_sub(), tool("propose_merchant_category", {"merchant": "Netflix", "new_name": "", "category": "Entertainment",
                                                           "subcategory": "Team support", "detail": "", "reason": "x"})),
        msg(text("I proposed a Team support subcategory and moving Netflix into it; apply them below.")),
    ], question="Put Netflix somewhere sensible")
    f = final(events)
    assert [p["type"] for p in f["proposals"]] == ["new_subcategory", "merchant_category"]
    sub, merchant = f["proposals"]
    assert sub["apply"] == {"method": "POST", "url": "/api/cards/categories",
                            "body": {"l1": "Entertainment", "l2": "Team support", "fixedness": "discretionary",
                                     "role": "subscription", "rewards": "general", "ai_visible": True}}
    assert merchant["apply"]["url"] == "/api/cards/merchant/edit"
    assert merchant["apply"]["body"]["cat_l2"] == "Team support"          # valid because proposed first
    assert "6 charges" in " ".join(merchant["lines"])
    told = fake.calls[1]["messages"][-1]["content"]
    assert all(not r.get("is_error") and '"applied": false' in r["content"] for r in told)


def test_bad_proposals_come_back_as_errors_to_fix():
    events, fake, _ = run([
        msg(tool("propose_merchant_category", {"merchant": "Nobody Inc", "new_name": "", "category": "Travel",
                                               "subcategory": "Lodging", "detail": "", "reason": "x"}),
            tool("propose_merchant_category", {"merchant": "Netflix", "new_name": "", "category": "Entertainment",
                                               "subcategory": "Team support", "detail": "", "reason": "x"})),
        msg(text("Sorry.")),
    ])
    errs = fake.calls[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in errs) and "No merchant" in errs[0]["content"] and "not a category" in errs[1]["content"]
    assert final(events)["proposals"] == []


def test_amount_rule_and_recurring_proposals():
    data = fixture()
    import card_amount_rules
    data.amount_rules = [card_amount_rules.AmountRule(1, "Kroger", None, 50.0, "Food & Beverage", "Groceries", "Groceries")]
    state = __import__("ask_proposals").State()
    import ask_proposals as P
    rule = {"applies_to": "subcategory", "merchant": "", "scope_category": "Food & Beverage", "scope_subcategory": "Groceries",
            "amount_from": 0, "amount_under": 100, "category": "Food & Beverage", "subcategory": "Groceries",
            "detail": "Convenience store", "reason": "x"}
    told, card = P.run("propose_amount_rule", rule, data, state)
    assert card["apply"]["body"]["scope_l2"] == "Groceries" and card["apply"]["body"]["amount_max"] == 100.0
    assert "11 charges ($770.00)" in " ".join(card["lines"])       # the $70 Kroger charges
    try:
        P.run("propose_amount_rule", {**rule, "applies_to": "merchant", "merchant": "Kroger"}, data, state)
        assert False, "overlap should be refused"
    except P.ProposalError as exc:
        assert "overlaps" in str(exc)
    _, rec = P.run("propose_recurring", {"name": "Netflix Premium", "merchant": "Netflix", "amount_min": 22, "amount_max": 24,
                                         "cadence": "monthly", "kind": "subscription", "reason": "x"}, data, state)
    assert rec["apply"]["body"]["merchant"] == "Netflix" and "Matches 6 charges" in " ".join(rec["lines"])


def test_new_subcategory_names_are_checked():
    import ask_proposals as P
    state = P.State()
    for bad in ("", "a|b", "Sports tickets"):
        try:
            P.run("propose_new_subcategory", {"category": "Entertainment", "name": bad, "counts_as": "discretionary",
                                              "detection": "none", "rewards": "general", "private": False, "reason": ""},
                  fixture(), state)
            assert False, bad
        except P.ProposalError:
            pass


# ── Router: pause, daily count, refunds, logging ─────────────────────────────

def _router(monkey_problem=None, rounds=(), relevant=True, used=None):
    import routers.ask as R
    state = {"count": 0 if used is None else used}
    c = client(list(rounds), relevant)
    logs = []
    saved = {}

    def patch(obj, name, value):
        saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, value)

    def bump(db, n):
        state["count"] = max(0, state["count"] + n)
        return state["count"]

    patch(R.card_ai, "key_problem", lambda db: monkey_problem)
    patch(R.card_ai, "client_factory", lambda: c)
    patch(R, "_bump", bump)
    patch(R, "meter", lambda db: {"month": "2026-10", "this_month": state["count"], "last_month": 0})
    patch(R.ask_data, "load", lambda db: fixture())
    patch(R, "make_logger", lambda db: (lambda *a: logs.append(a)))
    return R, state, c, logs, lambda: [setattr(o, n, v) for (o, n), v in saved.items()]


def _events(R, history):
    return [json.loads(line) for line in R._run(None, history, {}, {"n": 0})]


def test_paused_key_answers_with_reason_and_does_not_count():
    R, state, c, logs, undo = _router(monkey_problem={"message": "The Anthropic API key was rejected."})
    try:
        ev = _events(R, [{"role": "user", "text": "Where can I trim?"}])
        assert ev[0]["type"] == "error" and ev[0]["paused"] and state["count"] == 0
        assert c.beta.messages.calls == [] and c.beta.messages.parse_calls == []
    finally:
        undo()


def test_off_topic_is_declined_before_any_answer_call_and_counts():
    R, state, c, logs, undo = _router(relevant=False)
    try:
        ev = _events(R, [{"role": "user", "text": "Write a poem"}])
        assert ev[-1]["type"] == "declined" and state["count"] == 1 and c.beta.messages.calls == []
    finally:
        undo()


def test_failed_question_is_refunded_and_nothing_blocks():
    R, state, c, logs, undo = _router(rounds=[_tool_round() for _ in range(5)], used=30)
    try:
        ev = _events(R, [{"role": "user", "text": "Where can I trim?"}])
        assert ev[-1]["type"] == "error" and state["count"] == 30        # counted, then refunded
        assert len(c.beta.messages.calls) == 5                           # no limit: 30 already this month
    finally:
        undo()


def test_every_call_is_logged_with_request_and_response():
    R, state, c, logs, undo = _router(rounds=[_tool_round(), msg(text("Netflix is $22.99."))])
    try:
        ev = _events(R, [{"role": "user", "text": "Subscriptions?"}])
        assert ev[-1]["type"] == "final" and ev[-1]["meter"]["this_month"] == 1
        assert [entry[0] for entry in logs] == ["ask_topic", "ask", "ask"]
        assert all(entry[1] and entry[2] is not None for entry in logs)           # request and response
        assert logs[1][4]["question"] == "Subscriptions?" and logs[2][4]["round"] == 2
    finally:
        undo()


def test_make_logger_stores_full_request_and_response():
    import routers.ask as R

    class DB:
        rows = []

        def add(self, r):
            self.rows.append(r)

        def commit(self):
            pass

    db = DB()
    R.make_logger(db)("ask_topic", {"model": "claude-opus-5-5", "output_format": ask.TopicVerdict,
                                    "messages": [{"role": "user", "content": "q"}]},
                      NS(stop_reason="end_turn", parsed_output=ask.TopicVerdict(relevant=True),
                         usage=NS(input_tokens=5, output_tokens=1, cache_read_input_tokens=0)),
                      None, {"question": "q", "round": 1})
    row = db.rows[0]
    req, resp = json.loads(row.request_json), json.loads(row.response_json)
    assert req["output_format"]["output_schema"] == "TopicVerdict" and req["messages"][0]["content"] == "q"
    assert resp["parsed_output"] == {"relevant": True} and row.status == "ok" and row.input_tokens == 5


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)


# ── Proposals have their own cap; the last round is announced; a stuck round is stopped ──

def _sub(i):
    return tool("propose_new_subcategory", {"category": "Entertainment", "name": f"Team {i}", "counts_as": "discretionary",
                                            "detection": "none", "rewards": "general", "private": False, "reason": "x"})


def test_proposals_do_not_use_up_lookups():
    lookups = [tool("subscriptions", {"include_habitual": False}) for _ in range(5)]
    events, fake, _ = run([msg(*[_sub(i) for i in range(12)], *lookups), msg(text("Done."))])
    results = fake.calls[1]["messages"][-1]["content"]
    errors = [r["content"] for r in results if r.get("is_error")]
    assert errors == [f"Not run: at most {ask.MAX_TOOLS_PER_ROUND} lookups per round."]
    assert len(final(events)["proposals"]) == 12


def test_proposals_per_answer_are_capped():
    events, fake, _ = run([msg(*[_sub(i) for i in range(12)]), msg(*[_sub(100 + i) for i in range(10)]), msg(text("Done."))])
    results = fake.calls[2]["messages"][-1]["content"]
    refused = [r for r in results if r.get("is_error")]
    assert len(refused) == 2 and all("20 proposals per answer" in r["content"] for r in refused)
    assert len(final(events)["proposals"]) == ask.MAX_PROPOSALS_PER_QUESTION


def test_last_round_is_announced():
    events, fake, _ = run([_tool_round() for _ in range(4)] + [msg(text("Netflix is $22.99."))])
    last = fake.calls[4]["messages"][-1]["content"]
    assert last[-1] == {"type": "text", "text": ask.FINAL_ROUND_NOTE}
    assert all(b.get("type") == "tool_result" for b in fake.calls[3]["messages"][-1]["content"])   # not before then
    assert final(events)["type"] == "final"


def test_a_round_that_repeats_itself_is_stopped():
    babble = "Calling tools now.\n" + "Calls.\nGo.\n.\nNow.\n" * 40
    found = "I found 8 merchants that are probably listed under more than one name."
    events, fake, logged = run([msg(_sub(1)), msg(text(found + "\nHe invoked twelve propose tools.\n" + babble))],
                               question="Give me the next 10 or so proposals")
    f = final(events)
    assert f["type"] == "final" and f["text"].startswith(found) and f["text"].endswith(ask.STUCK_NOTE)
    assert "Calls." not in f["text"] and len(f["proposals"]) == 1
    assert len(fake.calls) == 2                                          # no further rounds after the stop
    assert logged[-1][2].stop_reason == "stopped_repeating" and logged[-1][3] is not None


def test_normal_answers_are_not_flagged_as_stuck():
    listing = "\n".join(f"- Merchant {i}: {i * 3} charges, ${i * 41}" for i in range(60))
    assert not ask.looks_stuck(listing)
    prose = "\n".join(f"Line {i} of a long but varied answer about spending." for i in range(40))
    assert not ask.looks_stuck(prose)
    assert ask.looks_stuck("Calls.\nGo.\nNow.\n" * 12)
    assert ask.trim_stuck("Real answer with a figure: $42.\n- a bullet\nCalls.\nGo.\nNow.") == "Real answer with a figure: $42.\n- a bullet"
