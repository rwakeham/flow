"""
The Dashboard's "Needs attention" panel (pure): one ordered list of what is waiting for the user,
each item with a count, a sentence and where to go (``nav``, in the shapes ask.js's askNavigate
understands). Nothing here is stored: every signal is recomputed from what the routers already know
(routers/attention.py gathers the inputs), so the counts match the tab each item opens.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

BACKUP_OLD_DAYS = 7          # "No backup in N days" from here on

# nav shapes the panel may use (test_attention checks every item against these)
NAV_SECTIONS = {"dashboard": {"tab", "payees", "accounts"}, "cashflow": {"account", "reconcile"},
                "cards": {"tab", "card", "q", "l1", "l2"}}
NAV_ACTIONS = {"settings", "balances", "import", "formats"}
SETTINGS_SECTIONS = {"ai", "backups", "updates", "changes", "account"}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _days_since(iso: str | None, today: date) -> int | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return (today - when.date()).days


def build(inp) -> list[dict]:
    """``inp`` has: today (date); balance_accounts [{id, name, ignored, archived, behind}];
    cash_accounts [{id, name, behind, unmatched, pending}]; unsorted_payees, review_open, subs_overdue,
    subs_price_change, subs_duplicate, formats_unpreviewed (ints); ai_problem ({message} or None);
    update ({version, url} or None); last_backup_at (iso text or None); backup_daily (bool).
    Returns items {kind, count, text, nav}, the ones to act on first."""
    g = (lambda k, d=None: getattr(inp, k, d)) if not isinstance(inp, dict) else (lambda k, d=None: inp.get(k, d))
    today: date = g("today") or date.today()
    items: list[dict] = []

    def add(kind, count, text, nav):
        items.append({"kind": kind, "count": int(count), "text": text, "nav": nav})

    problem = g("ai_problem")
    if problem:
        add("ai_paused", 1, "AI paused: " + str(problem.get("message") or "the key was refused"),
            {"action": "settings", "section": "ai"})

    cash = list(g("cash_accounts") or [])
    for a in cash:
        if a.get("unmatched"):
            add("unmatched", a["unmatched"], f"{_n(a['unmatched'], 'bank row', 'bank rows')} to reconcile in {a['name']}",
                {"section": "cashflow", "account": a["id"], "reconcile": True})
    for a in cash:
        if a.get("pending"):
            add("pending", a["pending"], f"{_n(a['pending'], 'match', 'matches')} to confirm in {a['name']}",
                {"section": "cashflow", "account": a["id"], "reconcile": True})

    behind = [a for a in (g("balance_accounts") or []) if a.get("behind") and not a.get("ignored") and not a.get("archived")]
    if behind:
        add("balances_behind", len(behind),
            f"{_n(len(behind), 'balance account has', 'balance accounts have')} no {today.strftime('%B')} balance yet",
            {"action": "balances"})
    for a in cash:
        if a.get("behind"):
            add("cash_behind", 1, f"Nothing newer than 40 days in {a['name']}: import its latest statement",
                {"section": "cashflow", "account": a["id"]})

    n = g("unsorted_payees") or 0
    if n:
        add("payees", n, f"{_n(n, 'payee needs', 'payees need')} a role in Cash Flow Analysis (this year)",
            {"section": "dashboard", "tab": "cashflow", "payees": True})
    n = g("review_open") or 0
    if n:
        add("review", n, f"{_n(n, 'card charge', 'card charges')} to review (this year)", {"section": "cards", "tab": "review"})
    subs = {"section": "cards", "tab": "subscriptions"}
    n = g("subs_overdue") or 0
    if n:
        add("subs_overdue", n, f"{_n(n, 'subscription is', 'subscriptions are')} overdue: possibly stopped", subs)
    n = g("subs_price_change") or 0
    if n:
        add("subs_price", n, f"{_n(n, 'subscription', 'subscriptions')} changed price", subs)
    n = g("subs_duplicate") or 0
    if n:
        add("subs_dup", n, f"{_n(n, 'subscription is', 'subscriptions are')} charged on more than one card", subs)
    n = g("formats_unpreviewed") or 0
    if n:
        add("formats", n, f"{_n(n, 'import format has', 'import formats have')} not been tried on a file yet",
            {"action": "formats"})

    if not g("backup_daily", True):
        add("backup_off", 1, "The daily backup is off", {"action": "settings", "section": "backups"})
    age = _days_since(g("last_backup_at"), today)
    if age is None:
        add("backup_old", 1, "No backup has been taken yet", {"action": "settings", "section": "backups"})
    elif age > BACKUP_OLD_DAYS:
        add("backup_old", 1, f"No backup in {age} days", {"action": "settings", "section": "backups"})

    upd = g("update")
    if upd and upd.get("version"):
        add("update", 1, f"Flow {upd['version']} is available", {"action": "settings", "section": "updates"})
    return items
