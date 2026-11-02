from sqlalchemy import Column, Integer, String, Boolean, Date, Numeric, DateTime, Float, Text, UniqueConstraint, Index, ForeignKey
from sqlalchemy.sql import func
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    account_type = Column(String(10), nullable=False)  # 'asset' or 'liability'
    override = Column(Boolean, nullable=False, default=False)
    ignored = Column(Boolean, nullable=False, default=False)
    institution = Column(String, nullable=True)
    last4 = Column(Text, nullable=True)              # last digits of each account number, comma-separated (balance_sheet.numbers)
    archived = Column(Boolean, nullable=False, default=False, server_default="false")  # closed: not carried forward
    alert_pct = Column(Float, nullable=True)         # change alert overrides; null = the global setting
    alert_abs = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Balance(Base):
    __tablename__ = "balances"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, nullable=False)
    period = Column(Date, nullable=False)            # 1st of the month; a snapshot on Oct 31 is the Nov 1 period
    amount = Column(Numeric(18, 2), nullable=False)
    source = Column(String(10), nullable=False, default="manual", server_default="upload")  # 'upload' | 'manual' | 'paste'
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "period", name="uq_balance_account_period"),
        Index("idx_balances_period", "period"),
        Index("idx_balances_account_id", "account_id"),
    )


class RegisterAccount(Base):
    __tablename__ = "register_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    opening_balance = Column(Numeric(18, 2), nullable=False, default=0)
    notes = Column(String, nullable=True)
    institution = Column(String, nullable=True)     # filled from the import format on first import
    cutoff_date = Column(Date, nullable=True)       # ignore transactions before this date
    cutoff_balance = Column(Numeric(18, 2), nullable=True)  # effective balance as of cutoff_date
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    register_account_id = Column(Integer, ForeignKey("register_accounts.id", ondelete="CASCADE"), nullable=False)
    date = Column(Date, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)  # positive=credit, negative=debit
    source = Column(String(10), nullable=False, default="manual")  # 'manual' | 'bank'
    bank_description = Column(String, nullable=True)   # raw string from bank CSV
    matched_to_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    is_reconciled = Column(Boolean, nullable=False, default=False)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_transactions_account_date", "register_account_id", "date"),
        Index("idx_transactions_matched_to", "matched_to_id"),
    )


# ── Credit cards ──────────────────────────────────────────────────────────────
#
# Card accounts are deliberately separate from register_accounts: they are
# upload-only, carry categories, and must never feed the dashboard or net worth
# (card balances reach net worth as liabilities through the balance-sheet
# upload). Amounts use the register's sign convention: charges negative.

class CardAccount(Base):
    __tablename__ = "card_accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    issuer = Column(String, nullable=True)
    last4 = Column(Text, nullable=True)              # last digits of each card on the account, comma-separated
    cardholder_names = Column(Text, nullable=True)   # newline-separated; redacted before any AI call
    notes = Column(String, nullable=True)
    archived = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CardImport(Base):
    __tablename__ = "card_imports"

    id = Column(Integer, primary_key=True)
    card_account_id = Column(Integer, ForeignKey("card_accounts.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String, nullable=True)
    issuer_format = Column(String, nullable=False)
    sign_convention = Column(String, nullable=False)
    sign_reason = Column(String, nullable=True)
    rows_parsed = Column(Integer, nullable=False, default=0)
    rows_imported = Column(Integer, nullable=False, default=0)
    rows_skipped = Column(Integer, nullable=False, default=0)
    pending_dropped = Column(Integer, nullable=False, default=0)
    profile_id = Column(Integer, ForeignKey("import_profiles.id", ondelete="SET NULL"), nullable=True)  # the format used
    date_min = Column(Date, nullable=True)       # statement period when the file states one,
    date_max = Column(Date, nullable=True)       # else first/last transaction date
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CardTransaction(Base):
    __tablename__ = "card_transactions"

    id = Column(Integer, primary_key=True)
    card_account_id = Column(Integer, ForeignKey("card_accounts.id", ondelete="CASCADE"), nullable=False)
    import_id = Column(Integer, ForeignKey("card_imports.id", ondelete="SET NULL"), nullable=True)
    date = Column(Date, nullable=False)
    post_date = Column(Date, nullable=True)
    raw_description = Column(String, nullable=False)
    merchant_key = Column(String, nullable=False, default="")    # sanitized; classification cache key
    merchant_label = Column(String, nullable=False, default="")  # display form of merchant_key
    processor = Column(String, nullable=True)
    canonical_merchant = Column(String, nullable=False)
    merchant_family = Column(String, nullable=False)
    amount = Column(Numeric(18, 2), nullable=False)               # charges negative, credits positive
    txn_type = Column(String(20), nullable=False)
    txn_subtype = Column(String, nullable=True)
    is_spend = Column(Boolean, nullable=False)
    cat_l1 = Column(String, nullable=False)
    cat_l2 = Column(String, nullable=False)
    cat_l3 = Column(String, nullable=False)
    # rule | issuer | keyword | unclassified | ai | merchant | user (see card_classifier)
    category_source = Column(String(12), nullable=False)
    name_set_by_user = Column(Boolean, nullable=False, default=False, server_default="false")  # renamed on its own
    pre_rule_category = Column(String, nullable=True)  # "l1|l2|l3|source" before an amount rule set the category
    reviewed_at = Column(DateTime(timezone=True), nullable=True)  # checked off on the Review tab
    reviewed_reasons = Column(String, nullable=True)      # reason codes it had then, comma-separated
    issuer_category = Column(String, nullable=True)
    issuer_type = Column(String, nullable=True)
    cardholder = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    foreign_flag = Column(Boolean, nullable=False, default=False)
    installment_flag = Column(Boolean, nullable=False, default=False)
    subscription_hint = Column(Boolean, nullable=False, default=False)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_card_txn_account_date", "card_account_id", "date"),
        Index("idx_card_txn_merchant_key", "merchant_key"),
        Index("idx_card_txn_category", "cat_l1", "cat_l2"),
        Index("idx_card_txn_import", "import_id"),
    )


class MerchantCategory(Base):
    """Classification cache: one row per sanitized merchant key the AI or the user has set."""
    __tablename__ = "merchant_categories"

    merchant_key = Column(String, primary_key=True)
    canonical_merchant = Column(String, nullable=False)
    merchant_family = Column(String, nullable=False)
    cat_l1 = Column(String, nullable=False)
    cat_l2 = Column(String, nullable=False)
    cat_l3 = Column(String, nullable=False)
    source = Column(String(8), nullable=False)        # 'ai' | 'user'
    subscription_hint = Column(Boolean, nullable=True)
    confidence = Column(Float, nullable=True)
    model = Column(String, nullable=True)
    prompt_version = Column(String, nullable=True)
    sanitized_label = Column(String, nullable=True)   # exactly what was sent, when source='ai'
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CardRecurringVerdict(Base):
    """
    The user's call on a detected recurring merchant on one card: 'confirmed' pins
    it, 'dismissed' hides it, null leaves it to detection. card_account_id 0 means
    every card (verdicts saved before they were per card); a card's own row wins.
    ended_through is the last charge the user's "Ended" covers: a later charge
    makes the series active again.
    """
    __tablename__ = "card_recurring_verdicts"

    canonical_merchant = Column(String, primary_key=True)
    card_account_id = Column(Integer, primary_key=True, nullable=False, default=0, server_default="0")
    status = Column(String(10), nullable=True)
    ended_through = Column(Date, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AiRequestLog(Base):
    """Every Anthropic API call: the full request as sent and the response as received."""
    __tablename__ = "ai_request_log"

    id = Column(Integer, primary_key=True)
    purpose = Column(String(12), nullable=False)      # 'categorize' | 'insights' | 'ask' | 'ask_topic'
    model = Column(String, nullable=False)
    prompt_version = Column(String, nullable=False)
    payload_json = Column(Text, nullable=False)       # the data Flow chose to send (user message / summary)
    request_json = Column(Text, nullable=True)        # the whole request body: system, tools, messages, settings
    response_json = Column(Text, nullable=True)       # the whole response: content blocks, stop reason, usage
    item_count = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False)       # 'ok' | 'error' | 'refusal'
    error = Column(Text, nullable=True)
    error_kind = Column(String(12), nullable=True)    # 'auth' | 'billing' | 'rate_limit' | 'network' | 'model' | 'other'
    key_fingerprint = Column(String(16), nullable=True)  # short hash of the key used; never the key itself
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AskUsage(Base):
    """Questions asked through Ask, per local calendar day; the panel shows the monthly total."""
    __tablename__ = "ask_usage"

    day = Column(Date, primary_key=True)
    count = Column(Integer, nullable=False, default=0)


class CardManualRecurring(Base):
    """A recurring charge the user defined (card_recurring.ManualSeries): it claims matching charges."""
    __tablename__ = "card_manual_recurring"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    merchant = Column(String, nullable=False)                  # canonical merchant name, case-insensitive
    card_account_id = Column(Integer, ForeignKey("card_accounts.id", ondelete="CASCADE"), nullable=True)
    amount_min = Column(Float, nullable=True)
    amount_max = Column(Float, nullable=True)
    cadence = Column(String(12), nullable=False)
    kind = Column(String(20), nullable=False)
    ended_through = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CardAmountRule(Base):
    """A category for charges within an amount range, for one merchant or for every merchant in
    a category (card_amount_rules)."""
    __tablename__ = "card_amount_rules"

    id = Column(Integer, primary_key=True)
    merchant = Column(String, nullable=True)                   # merchant scope: canonical name, case-insensitive
    scope_l1 = Column(String, nullable=True)                   # category scope: every merchant in this L1 ...
    scope_l2 = Column(String, nullable=True)                   # ... and L2 (null: the whole L1)
    amount_min = Column(Float, nullable=True)                  # inclusive; null = no lower bound
    amount_max = Column(Float, nullable=True)                  # exclusive; null = no upper bound
    cat_l1 = Column(String, nullable=False)
    cat_l2 = Column(String, nullable=False)
    cat_l3 = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CustomCategory(Base):
    """Your own subcategory (l3 '') or detail under an existing category (card_taxonomy.Custom)."""
    __tablename__ = "custom_categories"

    id = Column(Integer, primary_key=True)
    l1 = Column(String, nullable=False)
    l2 = Column(String, nullable=False)
    l3 = Column(String, nullable=False, default="", server_default="")
    fixedness = Column(String(16), nullable=False, default="discretionary")
    role = Column(String(16), nullable=False, default="none")
    rewards = Column(String(24), nullable=False, default="general")
    ai_visible = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CategoryRename(Base):
    """A built-in subcategory (l3 '') or detail renamed by the user, keyed by its original names (card_taxonomy)."""
    __tablename__ = "category_renames"

    id = Column(Integer, primary_key=True)
    l1 = Column(String, nullable=False)
    l2 = Column(String, nullable=False)
    l3 = Column(String, nullable=False, default="", server_default="")
    new_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("l1", "l2", "l3", name="uq_category_rename"),)


class CardInsightReport(Base):
    __tablename__ = "card_insight_reports"

    id = Column(Integer, primary_key=True)
    request_log_id = Column(Integer, ForeignKey("ai_request_log.id", ondelete="SET NULL"), nullable=True)
    account_ids = Column(String, nullable=False)
    date_from = Column(Date, nullable=True)
    date_to = Column(Date, nullable=True)
    common_period = Column(Boolean, nullable=False, default=False)
    model = Column(String, nullable=False)
    report_json = Column(Text, nullable=False)
    card_aliases_json = Column(Text, nullable=True)   # {"Card A": card id}: names restored when shown
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# flow_settings key holding the session generation (auth.set_session_epoch): bumped by
# "Sign out everywhere", kept as it is when a backup is restored.
SESSION_EPOCH_KEY = "session_epoch"
REVOKED_SESSIONS_KEY = "revoked_sessions"    # JSON {session id: expiry}: cookies ended by "Sign out"


class AppSetting(Base):
    """Small key/value settings (balance change alert thresholds, the session generation, the AI
    settings: settings_store.SETTING_ENV)."""
    __tablename__ = "flow_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


class AppSecret(Base):
    """Secrets Flow keeps itself (settings_store): the dashboard password as a scrypt hash, the
    Anthropic API key encrypted under SECRET_KEY. Never part of a backup (backup_service skips it)."""
    __tablename__ = "flow_secrets"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(String(32), nullable=True)


class PayeeRole(Base):
    """What a cash-register payee's money is for (cash_flow.ROLES), set by the user once per payee."""
    __tablename__ = "payee_roles"

    key = Column(String, primary_key=True)          # cash_flow.payee key (compact cleaned name)
    label = Column(String, nullable=False)
    role = Column(String(16), nullable=False)
    category = Column(String, nullable=True)        # spending: a top-level card category
    target = Column(String, nullable=True)          # debt / saving: the account it goes to
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ImportProfile(Base):
    """An import format: how to read one file layout (import_profile.Spec, stored as JSON)."""
    __tablename__ = "import_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    kind = Column(String(10), nullable=False)        # cash | card | balance
    institution = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    spec_json = Column(Text, nullable=False)
    fingerprint = Column(String(16), nullable=False)  # Spec.fingerprint: header (or headerless layout) hash
    version = Column(Integer, nullable=False, default=1)
    needs_preview = Column(Boolean, nullable=False, default=False, server_default="false")  # imported from JSON, not yet tried
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_import_profiles_fingerprint", "fingerprint"),)


class ImportProfileLink(Base):
    """Which accounts use a format. account_id points at register_accounts, card_accounts or accounts by kind."""
    __tablename__ = "import_profile_links"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("import_profiles.id", ondelete="CASCADE"), nullable=False)
    account_kind = Column(String(10), nullable=False)   # cash | card | balance
    account_id = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "account_kind", "account_id", name="uq_import_profile_link"),
        Index("idx_import_profile_links_account", "account_kind", "account_id"),
    )


class BalanceMarker(Base):
    """Where a balance-sheet account's balance sits on a pasted summary (balance_sheet.Marker), taught by example."""
    __tablename__ = "balance_markers"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    label_text = Column(String, nullable=False)
    last4 = Column(String(4), nullable=True)
    amount_rule = Column(String(16), nullable=False, default="first_after")   # first_after | on_line_with
    amount_label = Column(String, nullable=True)
    source = Column(String(8), nullable=False, default="user")                # user | ai
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("idx_balance_markers_account", "account_id"),)
