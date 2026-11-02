"""
Tests for transaction typing, category precedence, and the AI request shape.

Covers the rules' known traps, the precedence between the
user's merchant rules, local rules and cached AI answers, and the exact payload
the categorization call sends. No database or network: the AI client is a fake.
Runnable directly (``python3 test_card_classifier.py``) or via pytest.
"""

from __future__ import annotations

import json
import os
import types

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects

import card_ai  # noqa: E402
import card_taxonomy  # noqa: E402
from card_classifier import CacheEntry, MerchantIndex, classify_transaction, coerce_category  # noqa: E402
from card_privacy import sanitize  # noqa: E402


def classify(desc, amount=-50.0, issuer_category="", issuer_type="", cache=None):
    s = sanitize(desc)
    return classify_transaction(desc, amount, issuer_type, issuer_category, "Test Card",
                                s.merchant_key, s.label, cache or {})


def test_transaction_types():
    assert classify("AUTOPAY PAYMENT - THANK YOU", 500.0).txn_type == "payment"
    assert classify("ANNUAL MEMBERSHIP FEE", -95.0).txn_type == "card_fee"
    assert classify("FOREIGN TRANSACTION FEE", -3.10).txn_subtype == "foreign transaction fee"
    assert classify("INTEREST CHARGE ON PURCHASES", -12.0).txn_type == "interest"
    for perk in ("HOTEL STAYS CREDIT", "TRAVEL CREDIT", "CHASE OFFER CREDIT", "UBER CASH", "THANKYOU POINTS REDEMPTION"):
        assert classify(perk, 100.0).txn_type == "benefit_credit", perk
    assert classify("REI.COM", 40.0).txn_type == "refund"
    p = classify("AUTOPAY PAYMENT - THANK YOU", 500.0)
    assert not p.is_spend and p.cat_l1 == "Excluded (non-spend)"


def test_merchant_service_fee_is_a_purchase():
    t = classify("CITY PARKING SERVICE FEE", -2.50)
    assert t.txn_type == "purchase"


def test_known_rule_traps():
    assert classify("EXPEDIA 7263 UNITED").cat_l2 == "Booking agency / OTA"          # OTA, not airfare
    assert classify("C7* SILVER OAK CELLARS").cat_l3 == "Direct winery / wine club"
    assert classify("TST* THE HUNGRY BUTCHER", issuer_category="Restaurant-Restaurant").cat_l2 == "Dining"
    assert classify("UBER ONE").cat_l2 == "Memberships"
    assert classify("UBER EATS").cat_l3 == "Delivery"
    assert classify("UBER").cat_l2 == "Ground transportation"


def test_fixed_width_and_truncated_descriptions():
    assert classify("AplPay APPLE.COM/BILINTERNET CHARGE     CA").cat_l2 == "Cloud / technology"
    assert classify("SPRINGFIELD ARENA BOXSPRINGFIELD         IL").cat_l2 == "Sports tickets"
    assert classify("ATT MOB REC").cat_l2 == "Mobile phone"
    assert classify("CIRCLEK#1000043 OAK GROVE IL").cat_l2 == "Fuel"
    assert classify("EXTRA SPACE 1865    SHELBYVILLE         IL").cat_l2 == "Storage"
    gym = classify("SOME CLUB DUES", issuer_category="Entertainment-Associations")
    assert gym.cat_l2 == "Memberships" and gym.needs_ai                               # issuer-only: still asks the AI


def test_subscriptions_telecom_is_split():
    """What was bought decides the L1; how it is billed is the Subscriptions tab's job."""
    for desc, l1, l2 in [("VERIZON WIRELESS", "Bills & Utilities", "Mobile phone"),
                         ("COMCAST CABLE", "Bills & Utilities", "Internet"),
                         ("DUKE ENERGY", "Bills & Utilities", "Utilities"),
                         ("NETFLIX.COM", "Digital & Media", "Streaming"),
                         ("ADOBE CREATIVE CLOUD", "Digital & Media", "Software"),
                         ("NYTIMES DIGITAL", "Digital & Media", "News & media"),
                         ("AMAZON PRIME", "Shopping", "Memberships"),
                         ("UBER ONE", "Shopping", "Memberships")]:
        t = classify(desc)
        assert (t.cat_l1, t.cat_l2) == (l1, l2), (desc, t.cat_l1, t.cat_l2)
    assert classify("SOME CLUB DUES", issuer_category="Entertainment-Associations").cat_l1 == "Services"
    assert card_taxonomy.LEGACY_SUB_L1 not in card_taxonomy.TREE


def test_legacy_categories_map_into_the_tree():
    split = card_taxonomy.split_legacy_category
    legacy = card_taxonomy.LEGACY_SUB_L1
    cases = {("Mobile phone", "Telecom"): "Bills & Utilities", ("TV", "Live TV"): "Bills & Utilities",
             ("Streaming", "Video"): "Digital & Media", ("Cloud / technology", "AWS"): "Digital & Media",
             ("Memberships", "Amazon Prime"): "Shopping", ("Memberships", "DashPass"): "Shopping",
             ("Memberships", "Memberships / dues"): "Services"}
    for (l2, l3), l1 in cases.items():
        assert split(legacy, l2, l3) == l1
        assert card_taxonomy.is_valid(l1, l2), (l1, l2)
    assert split("Home", "Utilities", "Utilities") == "Bills & Utilities"
    assert split("Home", "Contractors", "Home services / contractors") is None
    assert split("Travel", "Airfare", "United") is None


def test_weak_guesses_are_queued_for_ai():
    t = classify("SNPO                CHARLOTTE           NC")
    assert t.category_source == "unclassified" and t.needs_ai
    t = classify("PIPER & PINE - SPRINSPRINGFIELD         IL", issuer_category="Restaurant-Restaurant")
    assert t.category_source == "issuer" and t.needs_ai
    t = classify("SOUTHWEST AIRLINES")
    assert t.category_source == "rule" and not t.needs_ai


def test_precedence_user_then_rule_then_ai():
    key = sanitize("SOUTHWEST AIRLINES").merchant_key
    user = CacheEntry("Southwest", "Southwest", "Travel", "Lodging", "Other hotels", "user")
    ai = CacheEntry("Southwest", "Southwest", "Shopping", "Specialty retail", "Specialty retail", "ai")
    assert classify("SOUTHWEST AIRLINES", cache={key: user}).cat_l2 == "Lodging"         # user beats rule
    assert classify("SOUTHWEST AIRLINES", cache={key: user}).category_source == "merchant"
    assert classify("SOUTHWEST AIRLINES", cache={key: ai}).cat_l2 == "Airfare"          # rule beats cached AI
    snpo = sanitize("SNPO").merchant_key
    t = classify("SNPO", cache={snpo: ai})
    assert t.category_source == "ai" and not t.needs_ai                               # cached AI beats a weak guess


def test_separator_variants_hit_the_same_rule():
    """CIRCLE_K and CIRCLE-K used to miss the Circle K rule and go to the AI."""
    for d in ("CIRCLE K #1000043 OAK GROVE IL", "CIRCLEK#1000043 OAK GROVE IL",
              "CIRCLE_K 1000043 OAK GROVE IL", "Circle K Store 41", "CIRCLE-K 0042 SALEM OR"):
        t = classify(d)
        assert (t.cat_l2, t.category_source, t.needs_ai) == ("Fuel", "rule", False), d


COMMON_BRANDS = [
    ("RALPHS #0123 LOS ANGELES CA", "Groceries", "Ralphs"),
    ("SAVE-A-LOT 4421", "Groceries", "Save A Lot"),
    ("7-ELEVEN 34012", "Fuel", "7-Eleven"),
    ("KUM & GO #455", "Fuel", "Kum & Go"),
    ("POPEYES 12345", "Dining", "Popeyes"),
    ("OLIVE GARDEN 0091", "Dining", "Olive Garden"),
    ("CHILI'S GRILL & BAR 88", "Dining", "Chili's"),
    ("BIGGBY COFFEE #123", "Dining", "Biggby Coffee"),
    ("DOLLAR GENERAL #1234", "General retail / online", "Dollar General"),
    ("DOLLARTREE 5521", "General retail / online", "Dollar Tree"),
    ("BJS WHOLESALE #12", "Warehouse club", "BJ's Wholesale"),
    ("HARBOR FREIGHT TOOLS 33", "Home maintenance", "Harbor Freight"),
    ("NATIONAL GRID NY", "Utilities", "National Grid"),
    ("COX COMM ATLANTA", "Internet", "Cox"),
    ("XFINITY MOBILE", "Mobile phone", "Xfinity Mobile"),
    ("HELLOFRESH 866-216", "Groceries", "HelloFresh"),
    ("DUOLINGO PLUS", "Software", "Duolingo"),
    ("ALLEGIANT AIR 0123", "Airfare", "Allegiant Air"),
    ("COMFORT INN SALEM OR", "Lodging", "Choice Hotels"),
    ("ZIPCAR TRIP", "Rental car", "Zipcar"),
    ("LA FITNESS 1234", "Fitness", "LA Fitness"),
    ("VCA ANIMAL HOSPITAL", "Pet care", "VCA Animal Hospitals"),
    ("CARMAX AUTO", "Auto purchase / repair / maintenance", "CarMax"),
    ("CINEMARK 0453", "Recreation", "Cinemark"),
    ("COURSERA.ORG", "Tuition", "Coursera"),
]


def test_common_brands_are_recognised_locally():
    for d, l2, name in COMMON_BRANDS:
        t = classify(d)
        assert (t.cat_l2, t.canonical_merchant, t.category_source, t.needs_ai) == (l2, name, "rule", False), (d, t)


def test_existing_brands_unchanged_by_the_common_block():
    """The common block runs first; brands the base rules already named keep their category."""
    for d, l2 in (("KROGER #123", "Groceries"), ("STARBUCKS 1234", "Dining"), ("UBER EATS", "Dining"),
                  ("LOWE'S #1234", "Home maintenance"), ("AMC THEATRES 22", "Recreation"),
                  ("COMCAST CABLE", "Internet"), ("SAM'S CLUB #8", "Warehouse club"), ("CVS/PHARMACY #1", "Pharmacy")):
        assert classify(d).cat_l2 == l2, d
    assert classify("WALGREENS PHARMACY 0042").cat_l2 == "Pharmacy"    # "PHARMACY" contains "MACY"
    assert classify("MACYS .COM").cat_l2 == "Department store"
    assert classify("LOWES FOODS #12").cat_l2 == "Groceries"          # not the home improvement store
    assert classify("AMC+ SUBSCRIPTION").cat_l2 == "Streaming"         # not the cinema
    assert classify("SAMS CLUB FUEL 4455").cat_l2 == "Fuel"


def test_every_rule_category_is_in_the_taxonomy():
    import card_rules
    import card_taxonomy
    for _rx, canon, _fam, l1, l2, _l3 in card_rules.ALL_MERCHANT_RULES:
        assert l1 in card_taxonomy.L1_ORDER and card_taxonomy.is_valid(l1, l2), (canon, l1, l2)
    assert len(card_rules.COMMON_MERCHANT_RULES) >= 200


def _entry(source, l2="Specialty retail"):
    return CacheEntry("X", "X", "Shopping", l2, l2, source)


def test_merchant_index_exact_and_loose():
    idx = MerchantIndex({"SPRINGFIELDGRIZZLIES": _entry("ai"), "CIRCLEK": _entry("user"), "AMAZON": _entry("ai")})
    assert idx.match("circle_k") == "CIRCLEK"                         # compact form of any spelling
    assert idx.match("SPRINGFIELDGRIZZL") == "SPRINGFIELDGRIZZLIES"   # one issuer truncates
    assert idx.match("SPRINGFIELD") is None                             # too little shared
    assert idx.match("AMAZONPRIME") is None                           # below the 8-character floor
    assert idx.match("") is None


def test_merchant_index_prefers_user_then_longest():
    idx = MerchantIndex({"SNPOSTOREONLINE": _entry("ai", "Software"), "SNPOSTORE": _entry("user")})
    assert idx.get("SNPOSTOREONL").source == "user"
    idx = MerchantIndex({"KELLYGREENIRR": _entry("ai", "A"), "KELLYGREENIRRIG": _entry("ai", "B")})
    assert idx.get("KELLYGREENIRRIGATION").cat_l2 == "B"


def test_loose_cache_hit_avoids_ai_at_import():
    idx = MerchantIndex({"SNPOSTORES": CacheEntry("SNPO", "SNPO", "Shopping", "Specialty retail", "Specialty retail", "ai")})
    t = classify("SNPO STORES ONLINE", cache=idx)
    assert (t.category_source, t.needs_ai, t.cat_l2) == ("ai", False, "Specialty retail")


def test_split_pending_reuses_instead_of_sending():
    idx = MerchantIndex({"SPRINGFIELDGRIZZLIES": _entry("ai"), "OLDPROMPT": _entry("ai")})
    rows = [("SPRINGFIELDGRIZZL", "Springfield Grizzl", "", "", "unclassified"),
            ("BRANDNEWSHOP", "Brand New Shop", "Merchandise", "", "issuer"),
            ("BRANDNEWSHOP", "Brand New Shop", "Merchandise", "Square", "issuer"),
            ("OLDPROMPT", "Old Prompt", "", "", "ai")]
    to_send, reusable = card_ai.split_pending(rows, idx, stale_keys={"OLDPROMPT"})
    assert [i["merchant_key"] for i in to_send] == ["BRANDNEWSHOP", "OLDPROMPT"]
    assert to_send[0] == {"merchant_key": "BRANDNEWSHOP", "label": "Brand New Shop", "processor": "Square",
                          "issuer_category": "Merchandise"}
    assert reusable == {"SPRINGFIELDGRIZZL": "SPRINGFIELDGRIZZLIES"}


def test_coerce_category_clamps_to_taxonomy():
    assert coerce_category("Travel", "Lodging", "") == ("Travel", "Lodging", "Lodging")
    assert coerce_category("Travel", "Made Up", "x") == ("Uncategorized", "Needs review", "Needs review")
    assert coerce_category("Excluded (non-spend)", "Payment", "x")[0] == "Uncategorized"


def test_categorize_payload_carries_only_labels():
    items = [{"merchant_key": "PIPER & PINE", "label": "Piper & Pine", "issuer_category": "Restaurant-Restaurant",
              "processor": ""},
             {"merchant_key": "FRIENDS OF MAPLE", "label": "Friends Of Maple", "issuer_category": "",
              "processor": "Square"}]
    payload = card_ai.build_categorize_payload(items)
    assert payload == {"merchants": [
        {"id": 0, "label": "Piper & Pine", "issuer_category": "Restaurant-Restaurant"},
        {"id": 1, "label": "Friends Of Maple", "processor": "Square"},
    ]}


class _FakeMessages:
    def __init__(self, stop_reason="end_turn", parsed=None):
        self.calls = []
        self.stop_reason = stop_reason
        self.parsed = parsed

    def parse(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed, model=kw["model"],
                                     stop_details=types.SimpleNamespace(explanation="policy"),
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))


def _client(msgs):
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=msgs))


def test_call_shape_uses_structured_output_caching_and_fallback():
    parsed = card_ai.MerchantBatch(merchants=[])
    msgs = _FakeMessages(parsed=parsed)
    out, _usage, model = card_ai._call(_client(msgs), "SYSTEM", {"merchants": []}, card_ai.MerchantBatch,
                                       effort="low", max_tokens=16000)
    kw = msgs.calls[0]
    assert out is parsed and model == card_ai.model_name()
    assert kw["output_format"] is card_ai.MerchantBatch
    assert kw["output_config"] == {"effort": "low"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in kw and "temperature" not in kw
    assert json.loads(kw["messages"][0]["content"]) == {"merchants": []}


def test_refusal_and_truncation_raise():
    for msgs in (_FakeMessages(stop_reason="refusal"), _FakeMessages(stop_reason="max_tokens")):
        try:
            card_ai._call(_client(msgs), "S", {}, card_ai.MerchantBatch, effort="low", max_tokens=10)
        except card_ai.AiError:
            continue
        raise AssertionError("expected AiError")


def test_disabled_without_key():
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        assert not card_ai.enabled()
        assert "No Anthropic API key" in card_ai.disabled_reason()
        assert not card_ai.area_enabled("cards") and "No Anthropic API key" in card_ai.area_off_reason("cards")
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_area_switches_gate_each_feature_under_the_global_one():
    import settings_store as st
    old = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "FLOW_AI_ENABLED", "FLOW_AI_CASH")}
    try:
        st.testing_state({st.AI_CASH: "0"}, {st.API_KEY: "sk-ant-test-key"})
        assert card_ai.enabled() and card_ai.areas_enabled() == {"cards": True, "cash": False, "balances": True}
        assert card_ai.area_off_reason("cards") is None
        assert card_ai.area_off_reason("cash") == "AI for cash accounts is switched off (Settings → AI in the top bar)."
        os.environ["FLOW_AI_ENABLED"] = "0"                       # the global switch comes first
        assert not any(card_ai.areas_enabled().values())
        assert card_ai.area_off_reason("balances") == "AI calls are switched off."
    finally:
        st.testing_state()
        for k, v in old.items():
            if v is not None:
                os.environ[k] = v


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


def test_venues_are_generic_not_regional():
    """Venue words anywhere; a sponsor's name in a venue doesn't make it that sponsor's purchase."""
    for desc in ("ACME STADIUM SPRINGFIELD IL", "RIVERSIDE ARENA", "OAKWOOD BALLPARK CONCESSIONS",
                 "CHASE CENTER EVENTS", "AMERICAN AIRLINES CENTER", "T-MOBILE PARK", "STATE FARM STADIUM",
                 "NHL SHOP TICKETS"):
        assert classify(desc).cat_l2 == "Sports tickets", desc
    assert classify("AMERICAN AIRLINES 0012345678901").cat_l2 == "Airfare"
    assert classify("STATE FARM INSURANCE").cat_l2 == "Insurance"
    assert classify("TARGET T-0862 SPRINGFIELD IL").cat_l2 != "Sports tickets"
    assert classify("FIELD & STREAM 0412").cat_l2 != "Sports tickets"
