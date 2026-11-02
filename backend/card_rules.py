"""
Credit-card classification rules.

The base taxonomy, transaction types and merchant rules (MERCHANT_RULES), with
later additions marked "Flow:". Those cover issuer exports seen in practice (some truncate the merchant field at 20 characters,
others glue store numbers onto names).

Everything here is data plus small pure functions so it can be edited safely.
Order matters: the first matching rule wins, so put specific patterns before
generic ones (e.g. UBER EATS before UBER, a sponsor-named arena before its sponsor).

Rule tuple: (regex, canonical_merchant, merchant_family, L1, L2, L3)
Regexes run against the UPPERCASED, processor-prefix-stripped description.
"""
import re

# ---------------------------------------------------------------------------
# Transaction-type detection (non-merchant activity)
# ---------------------------------------------------------------------------
PAYMENT_RE = re.compile(
    r"(AUTOPAY|AUTO ?PAY|AUTOMATIC PAYMENT|ONLINE PAYMENT|MOBILE PAYMENT|PAYMENT\s*-?\s*THANK\s*YOU|"
    r"PAYMENT RECEIVED|THANK YOU FOR YOUR PAYMENT|ELECTRONIC PAYMENT|ACH PAYMENT|PAYMENT - WEB|"
    r"\bEPAYMENT\b|PAYMENT FROM CHK|INTERNET PAYMENT|DIRECT DEBIT PAYMENT)")
PAYMENT_REVERSAL_RE = re.compile(r"(PAYMENT REVERSAL|RETURNED PAYMENT|PAYMENT RETURNED|REVERSE PAYMENT|NSF)")
CARRY_IN_RE = re.compile(
    r"(PREVIOUS BALANCE|BALANCE FORWARD|BEGINNING BALANCE|OPENING BALANCE|STATEMENT BALANCE|"
    r"BALANCE CARRIED|CARRY ?FORWARD|NEW BALANCE)")
BALANCE_TRANSFER_RE = re.compile(r"(BALANCE TRANSFER|BAL TRANSFER|BT FEE)")
ANNUAL_FEE_RE = re.compile(r"(ANNUAL MEMBERSHIP FEE|ANNUAL FEE|MEMBERSHIP FEE|RENEWAL FEE)")
FTF_RE = re.compile(r"(FOREIGN TRANSACTION FEE|FOREIGN TRANS FEE|INTL TRANSACTION FEE|INTERNATIONAL TRANSACTION FEE|FOREIGN CURRENCY CONVERSION FEE)")
OTHER_FEE_RE = re.compile(r"(LATE FEE|LATE PAYMENT FEE|RETURNED PAYMENT FEE|OVERLIMIT FEE|PLAN FEE|FEE FOR PLAN|PAY OVER TIME FEE|CASH ADVANCE FEE)")
INTEREST_RE = re.compile(r"(INTEREST CHARGE|FINANCE CHARGE|INTEREST CHARGED|PURCHASE INTEREST|CASH ADVANCE INTEREST|MINIMUM INTEREST)")
CASH_ADVANCE_RE = re.compile(r"(CASH ADVANCE|ATM WITHDRAWAL|CASH WITHDRAWAL)")
# Card-benefit statement credits (not merchant refunds). Kept separate so they
# can be valued in the annual-fee analysis rather than hidden inside categories.
# Perk kinds any issuer uses ("<kind> CREDIT"), then each large issuer's own wording
# for offers and points redemptions.
_PERK_KINDS = (r"STATEMENT|RIDESHARE|DINING|RESTAURANT|TRAVEL|AIRLINE( FEE| INCIDENTAL)?|HOTEL|STAYS|RESORT|PROPERTY|"
               r"LOUNGE|CLEAR|GLOBAL ENTRY|TSA PRE.?CHECK|NEXUS|STREAMING|ENTERTAINMENT|DIGITAL( ENTERTAINMENT)?|"
               r"WIRELESS|PHONE|GROCERY|DELIVERY|GAS|EV CHARGING|FITNESS|WELLNESS|MEMBERSHIP|"
               r"ANNIVERSARY|ANNUAL|BIRTHDAY|WELCOME|PROMO(TIONAL)?|BONUS|COMPANION|"
               r"UBER|LYFT|DOORDASH|DASHPASS|INSTACART|GRUBHUB|RESY|OPENTABLE|WALMART\+|PELOTON|SAKS|EQUINOX")
BENEFIT_CREDIT_RE = re.compile(
    rf"((\b({_PERK_KINDS}) CREDIT\b)|UBER CASH|"
    r"REWARDS REDEMPTION|CASH BACK REDEMPTION|REDEMPTION CREDIT|POINTS REDEMPTION|MILES REDEMPTION|"
    r"CREDIT ADJUSTMENT|COURTESY CREDIT|RETENTION CREDIT|"
    r"AMEX OFFER|AMEX CREDIT|MEMBERSHIP REWARDS|CHASE OFFER|ULTIMATE REWARDS|PAY YOURSELF BACK|"
    r"CITI MERCHANT OFFER|THANKYOU POINTS|CAPITAL ONE OFFER|PURCHASE ERASER|BANKAMERIDEALS|BOA DEALS|"
    r"US BANK REAL ?REWARDS|WELLS FARGO DEALS|DISCOVER DEALS|CASHBACK BONUS)")
ADJUSTMENT_RE = re.compile(r"(BALANCE ADJUSTMENT|ACCOUNT ADJUSTMENT|MISC ADJUSTMENT)")
INSTALLMENT_RE = re.compile(r"(INSTALLMENT|PLAN IT|MY ?CHASE ?PLAN|PAY OVER TIME|AFFIRM|KLARNA|AFTERPAY|SEZZLE|PLAN PURCHASE)")

# Issuer "type" column values (Chase/Citi style) that indicate a payment.
ISSUER_PAYMENT_TYPES = {"payment", "payments", "autopay"}


def classify_txn_type(desc_u, amount, issuer_type="", issuer_category=""):
    """Return (txn_type, txn_subtype). amount is spend-positive."""
    it = (issuer_type or "").strip().lower()
    ic = (issuer_category or "").strip().lower()
    if CARRY_IN_RE.search(desc_u):
        return "carry_in", "statement balance"
    if PAYMENT_REVERSAL_RE.search(desc_u) and not OTHER_FEE_RE.search(desc_u):
        return "payment_reversal", ""
    if BALANCE_TRANSFER_RE.search(desc_u):
        return "balance_transfer", ""
    if PAYMENT_RE.search(desc_u) or it in ISSUER_PAYMENT_TYPES:
        return "payment", ""
    if FTF_RE.search(desc_u):
        return "card_fee", "foreign transaction fee"
    if ANNUAL_FEE_RE.search(desc_u) and ("fee" in ic or amount > 0 and len(desc_u) < 60):
        return "card_fee", "annual fee"
    if OTHER_FEE_RE.search(desc_u):
        return "card_fee", "other fee"
    if INTEREST_RE.search(desc_u):
        return "interest", ""
    if CASH_ADVANCE_RE.search(desc_u):
        return "cash_advance", ""
    if ADJUSTMENT_RE.search(desc_u):
        return "adjustment", ""
    if amount < 0 and BENEFIT_CREDIT_RE.search(desc_u):
        return "benefit_credit", ""
    if amount < 0:
        return "refund", ""
    return "purchase", ""


# Which types count toward spending totals. Card fees count (and stay
# identifiable); interest and cash advances are reported separately.
SPEND_TYPES = {"purchase", "refund", "benefit_credit", "card_fee"}

# ---------------------------------------------------------------------------
# Description cleanup
# ---------------------------------------------------------------------------
PROCESSOR_PREFIXES = [
    (r"^SQ\s?\*\s*", "Square"), (r"^TST\*\s*", "Toast"), (r"^TOAST\s?\*", "Toast"),
    (r"^SP\s+\*?\s*", "Shopify"), (r"^SHOPIFY\s?\*", "Shopify"),
    (r"^PAYPAL\s?\*\s*", "PayPal"), (r"^PP\*\s*", "PayPal"), (r"^PY\s?\*\s*", "Payment processor"),
    (r"^IN\s?\*\s*", "Payment processor"), (r"^BT\*\s*", "Braintree"), (r"^CKO\*\s*", "Checkout.com"),
    (r"^FSP\*\s*", "Payment processor"), (r"^CLP\*\s*", "Clover"), (r"^GOOGLE \*(?!.*(STORAGE|ONE|CLOUD|YOUTUBE|FI\b))", "Google Pay"),
    (r"^APLPAY\s", "Apple Pay"), (r"^C7\*\s*", "Commerce7"), (r"^WDC\*\s*", "WineDirect"),
]


def strip_processor(desc_u):
    for pat, name in PROCESSOR_PREFIXES:
        m = re.match(pat, desc_u)
        if m:
            return desc_u[m.end():].strip(), name
    return desc_u, ""


_NOISE = re.compile(
    r"(#\s?\d+|\bSTORE\s?\d+|\b\d{3,}\b|\b[A-Z]{2}\s*\d{5}(-\d{4})?\b|\*[A-Z0-9]{5,}|\bWWW\.|\.COM\b|"
    r"\b(INC|LLC|CORP|CO|LTD)\b\.?|[\-/]+$)")
US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
             "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()


GENERIC_RE = re.compile(
    r"RESTAURANT|CLINIC|MEDICAL|HOSPITAL|PHARMAC|DRUG|LIQUOR|WINE|VINEYARD|CELLARS|VINTNER|BREW|TAPROOM|DISTILL|CIDER|MEAD|"
    r"COFFEE|ESPRESSO|ROASTER|BAKERY|PATISSERIE|BUTCHER|MEAT|FISH|CHEESE|MARKET|GROCER|PARKING|PARK|TOLL|PASS|TAXI|CAB|HOTEL|"
    r"TUTOR|SCHOOL|ACADEMY|UNIVERSITY|COLLEGE|TUITION|CAMP|LESSON|LEAGUE|SCOUT|ACTIVITY|DENTAL|DENTIST|ORTHO|ENDO|PERIO|SALON|BARBER|SPA|"
    r"MASSAGE|NAIL|HAIR|GYM|FITNESS|YOGA|PILATES|ATHLETIC|INSURANCE|DONATION|FOUNDATION|CHURCH|MINISTR|PARISH|COUNTY|CITY OF|STATE OF|"
    r"PLUMB|HEATING|HVAC|ROOF|CONSTRUCT|CONTRACT|REMODEL|PAINT|ELECTRIC|PEST|EXTERMIN|CLEAN|MAID|GUTTER|WINDOW|GARAGE|HANDYMAN|"
    r"CARPET|FLOOR|CHIMNEY|SEPTIC|POOL|LAWN|LANDSCAP|TREE|ARBOR|IRRIGATION|SPRINKLER|SNOW|NURSERY|GARDEN|APPLIANCE|FURNITURE|"
    r"MATTRESS|HARDWARE|TIRE|AUTO|COLLISION|CAR WASH|OIL|MOTORS|FUEL|GAS|TRANSIT|AIRLINE|AIRWAYS|AIR LINES|GOLF|BOWL|CINEMA|"
    r"MUSEUM|ZOO|SKI|THEAT|ORCHESTRA|SYMPHONY|AQUARIUM|ESCAPE|BIKE|CYCLE|RUNNING|VET|PET|GROOM|KENNEL|UTILIT|WATER|LUNCH|"
    r"\bMD\b|PSYCH|OPTOM|EYE|VISION|LASER|AESTHETIC|INJECT|HEALTH|PHYSICIAN|DERMA|CHIRO|THERAPY|RADIOLOGY|PEDIATRIC|URGENT|"
    r"RENT|CRUISE|LEGAL|ATTORNEY|ACCOUNT|CPA|NOTARY|BROKER|APPLIANCE|SUPERMARKET|BOOKS|JEWEL|SHIPPING|POSTAL|INN|RESORT|"
    r"SUITES|LODGE|TRANSFER|TRIP|DONAT|CHARIT|TAX|LICENSE|REGISTRATION|VEHICLE|DMV|STATION")

# Issuer categories specific enough to beat a generic keyword; vague ones
# ("General retail", "Other travel") never override.
AUTHORITATIVE_ISSUER_L2 = {"Dining", "Groceries", "Lodging", "Airfare", "Fuel", "Pharmacy", "Medical", "Dental",
                           "Rental car", "Ground transportation"}
# Keywords that describe the merchant itself and should survive any issuer label.
STRONG_RE = re.compile(r"WINERY|VINEYARD|CELLARS|VINTNER|WINE ?CLUB|WINEDIRECT|COMMERCE7|BREW|DISTILL|CIDERY|TAPROOM|HOTEL|RESORT|"
                       r"AIRLINE|AIRWAYS|DENTAL|DENTIST|ORTHODONT|TUTOR|TUITION|PARKING|TOLL|INSURANCE|CHURCH|DONATION")

_SHORT_UPPER = {"BP", "REI", "CVS", "AMC", "IHG", "KLM", "AWS", "HBO", "MTA", "BART", "TFL", "UPS", "USPS", "DHL", "QT", "RH", "CB2", "F45",
                "YMCA", "PBS", "NPR", "IRS", "BSA", "HEB", "ESPN", "AT&T", "TSA", "USAA", "DMV", "B&H", "K&L", "MGM", "ACE"}


def nice_case(s):
    out = []
    for w in re.split(r"(\s+)", s):
        if w.strip() and (w.upper() in _SHORT_UPPER):
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return "".join(out).strip()


def _fallback_canonical(desc_u):
    """Conservative display name for merchants no rule recognizes."""
    s = _NOISE.sub(" ", desc_u)
    toks = [t for t in re.split(r"\s+", s) if t]
    # drop a trailing state code and the city word before it (a common issuer tail)
    if len(toks) >= 3 and toks[-1] in US_STATES:
        toks = toks[:-2]
    toks = toks[:4] if toks else [desc_u[:30]]
    name = " ".join(toks).strip(" -*.,")
    return nice_case(name) if name else nice_case(desc_u)


# ---------------------------------------------------------------------------
# Merchant rules (specific first). (regex, canonical, family, L1, L2, L3)
# ---------------------------------------------------------------------------
# Flow: the base rules' "Subscriptions / Telecom" mixed what was bought with how it
# is billed (billing is the Subscriptions tab's job, whatever the category). It
# is split into Bills & Utilities (phone, internet, TV and Home's utilities) and
# Digital & Media (streaming, software, cloud, news); paid memberships moved to
# Shopping (Prime, Walmart+, Uber One, DashPass) and Services (AAA, club dues).
T, F, S, H, TR, HP, E, BILLS, DIG, ED, FIN, PETS, GIVE, GOV, SVC = (
    "Travel", "Food & Beverage", "Shopping", "Home", "Transportation", "Health & Personal",
    "Entertainment", "Bills & Utilities", "Digital & Media", "Education / Family", "Financial / Investment",
    "Pets", "Giving", "Government & Taxes", "Services")

# Flow: US airlines and the large hotel groups each get their own detail (Airfare > United,
# Lodging > Hyatt ...), generated from these lists so every brand is treated alike; other
# airlines and hotels share "Other airlines" / "Other hotels". (airfare regex, canonical, family)
US_AIRLINES = [
    (r"DELTA AIR|DELTA AIRLINES|^DELTA\s+\d|^DELTA\s+ATLANTA|DELTA\.COM", "Delta Air Lines", "Delta"),
    (r"UNITED\s*AIR|UNITED\s+\d{10}|UNITED\.COM", "United Airlines", "United"),
    (r"AMERICAN AIR|AMERICAN\s+\d{10}|AA\.COM", "American Airlines", "American"),
    (r"SOUTHWEST\s*(AIR|\d)|SOUTHWES\b", "Southwest Airlines", "Southwest"),
    (r"ALASKA AIR", "Alaska Airlines", "Alaska"),
    (r"JETBLUE", "JetBlue", "JetBlue"),
    (r"HAWAIIAN AIR", "Hawaiian Airlines", "Hawaiian"),
    (r"SPIRIT AIR", "Spirit Airlines", "Spirit"),
    (r"FRONTIER AIR", "Frontier Airlines", "Frontier"),
    (r"ALLEGIANT", "Allegiant Air", "Allegiant"),
    (r"SUN COUNTRY", "Sun Country Airlines", "Sun Country"),
    (r"BREEZE AIR", "Breeze Airways", "Breeze"),
    (r"\bAVELO\b", "Avelo Airlines", "Avelo"),
]
# Airport lounges, airline vacation packages and inflight purchases, before the airfare rules.
AIRLINE_EXTRAS = [
    (r"DELTA SKY ?CLUB", "Delta Sky Club", "Delta", "Other travel", "Airport lounge"),
    (r"UNITED CLUB", "United Club", "United", "Other travel", "Airport lounge"),
    (r"ADMIRALS ?CLUB", "Admirals Club", "American", "Other travel", "Airport lounge"),
    (r"ALASKA (AIRLINES )?LOUNGE", "Alaska Lounge", "Alaska", "Other travel", "Airport lounge"),
    (r"CENTURION LOUNGE", "Centurion Lounge", "American Express", "Other travel", "Airport lounge"),
    (r"CAPITAL ONE LOUNGE", "Capital One Lounge", "Capital One", "Other travel", "Airport lounge"),
    (r"SAPPHIRE LOUNGE", "Chase Sapphire Lounge", "Chase", "Other travel", "Airport lounge"),
    (r"PRIORITY PASS", "Priority Pass", "Priority Pass", "Other travel", "Airport lounge"),
    (r"PLAZA PREMIUM", "Plaza Premium Lounge", "Plaza Premium", "Other travel", "Airport lounge"),
    (r"DELTA VACATIONS", "Delta Vacations", "Delta", "Booking agency / OTA", "Airline vacation package"),
    (r"UNITED VACATIONS", "United Vacations", "United", "Booking agency / OTA", "Airline vacation package"),
    (r"(AMERICAN AIRLINES|\bAA) VACATIONS", "American Airlines Vacations", "American", "Booking agency / OTA",
     "Airline vacation package"),
    (r"SOUTHWEST VACATIONS", "Southwest Vacations", "Southwest", "Booking agency / OTA", "Airline vacation package"),
    (r"ALASKA (AIRLINES )?VACATIONS", "Alaska Airlines Vacations", "Alaska", "Booking agency / OTA",
     "Airline vacation package"),
    (r"JETBLUE (VACATIONS|PACKAGES)", "JetBlue Vacations", "JetBlue", "Booking agency / OTA", "Airline vacation package"),
    (r"DELTA (WI-?FI|INFLIGHT|IN-FLIGHT)|GOGO.*DELTA", "Delta Inflight", "Delta", "Other travel", "Inflight purchase"),
    (r"UNITED (WI-?FI|INFLIGHT|IN-FLIGHT)|UA INFLT", "United Inflight", "United", "Other travel", "Inflight purchase"),
    (r"AMERICAN (AIRLINES )?(WI-?FI|INFLIGHT|IN-FLIGHT)|AA INFLIGHT", "American Inflight", "American", "Other travel",
     "Inflight purchase"),
    (r"SOUTHWEST (WI-?FI|INFLIGHT|IN-FLIGHT)", "Southwest Inflight", "Southwest", "Other travel", "Inflight purchase"),
    (r"ALASKA (AIRLINES )?(WI-?FI|INFLIGHT|IN-FLIGHT)", "Alaska Inflight", "Alaska", "Other travel", "Inflight purchase"),
    (r"JETBLUE (WI-?FI|INFLIGHT|IN-FLIGHT)|FLY-?FI", "JetBlue Inflight", "JetBlue", "Other travel", "Inflight purchase"),
]
# (regex, canonical = family = detail). Sub-brands count as their group: a Hampton Inn stay is Hilton.
HOTEL_GROUPS = [
    (r"MARRIOTT|COURTYARD|RESIDENCE INN|SPRINGHILL|FAIRFIELD INN|TOWNEPLACE|RITZ[- ]?CARLTON|ST\.? REGIS|"
     r"\bJW\b|WESTIN|SHERATON|\bW HOTEL|LE MERIDIEN|RENAISSANCE|AUTOGRAPH COLL|TRIBUTE PORT|LUXURY COLLECTION|"
     r"FOUR POINTS|\bALOFT\b|\bMOXY\b|\bAC HOTEL|ELEMENT BY|DESIGN HOTELS|GAYLORD|DELTA HOTELS|EDITION HOTEL|BONVOY",
     "Marriott"),
    (r"HILTON|HAMPTON INN|DOUBLETREE|EMBASSY SUITES|HOMEWOOD|HOME2|CONRAD|WALDORF|CANOPY BY|CURIO|TAPESTRY", "Hilton"),
    (r"HYATT|ANDAZ|THOMPSON HOTEL|ALILA", "Hyatt"),
    (r"INTERCONTINENTAL|KIMPTON|CROWNE PLAZA|HOLIDAY INN|HOTEL INDIGO|\bIHG\b|STAYBRIDGE|CANDLEWOOD", "IHG"),
    (r"WYNDHAM|LA QUINTA|DAYS INN|SUPER 8|RAMADA", "Wyndham"),
    (r"COMFORT (INN|SUITES)|QUALITY INN|SLEEP INN|\bCLARION\b|ECONO ?LODGE|CAMBRIA HOTEL|RODEWAY|"
     r"MAINSTAY SUITES|CHOICE HOTELS", "Choice Hotels"),
    (r"BEST WESTERN", "Best Western"),
    (r"SOFITEL|NOVOTEL|\bPULLMAN\b|\bACCOR\b|IBIS HOTEL|SWISSOTEL", "Accor"),
]

MERCHANT_RULES = [
    *[(rx, c, fam, T, l2, l3) for rx, c, fam, l2, l3 in AIRLINE_EXTRAS],
    *[(rx, c, fam, T, "Airfare", fam) for rx, c, fam in US_AIRLINES],
    # --- other airlines ---
    (r"AIR CANADA", "Air Canada", "Air Canada", T, "Airfare", "Other airlines"),
    (r"\bKLM\b", "KLM", "Air France-KLM", T, "Airfare", "Other airlines"),
    (r"AIR FRANCE", "Air France", "Air France-KLM", T, "Airfare", "Other airlines"),
    (r"VIRGIN ATL", "Virgin Atlantic", "Virgin Atlantic", T, "Airfare", "Other airlines"),
    (r"AEROMEXICO", "Aeromexico", "Aeromexico", T, "Airfare", "Other airlines"),
    (r"LUFTHANSA", "Lufthansa", "Lufthansa", T, "Airfare", "Other airlines"),
    (r"BRITISH A", "British Airways", "British Airways", T, "Airfare", "Other airlines"),
    (r"ICELANDAIR", "Icelandair", "Icelandair", T, "Airfare", "Other airlines"),
    (r"KOREAN AIR", "Korean Air", "Korean Air", T, "Airfare", "Other airlines"),
    (r"EMIRATES", "Emirates", "Emirates", T, "Airfare", "Other airlines"),
    (r"\bAIRLINES?\b|\bAIRWAYS\b|\bAIR LINES\b", None, None, T, "Airfare", "Other airlines"),
    # --- hotels ---
    *[(rx, name, name, T, "Lodging", name) for rx, name in HOTEL_GROUPS],
    (r"AIRBNB", "Airbnb", "Airbnb", T, "Lodging", "Vacation rental"),
    (r"\bVRBO\b|HOMEAWAY", "Vrbo", "Expedia Group", T, "Lodging", "Vacation rental"),
    (r"EXPEDIA", "Expedia", "Expedia Group", T, "Booking agency / OTA", "Expedia"),
    (r"HOTELS\.COM|HOTELSCOM", "Hotels.com", "Expedia Group", T, "Booking agency / OTA", "Hotels.com"),
    (r"BOOKING\.COM|BOOKING COM", "Booking.com", "Booking Holdings", T, "Booking agency / OTA", "Booking.com"),
    (r"PRICELINE|KAYAK|AGODA", "Priceline/Kayak", "Booking Holdings", T, "Booking agency / OTA", "Other OTA"),
    (r"ORBITZ|TRAVELOCITY|HOPPER|TRIP\.COM|VIATOR|GETYOURGUIDE|KLOOK", None, None, T, "Booking agency / OTA", "Other OTA"),
    # --- ground / rental / parking ---
    (r"UBER\s*\*?\s*ONE|UBER ONE", "Uber One", "Uber", S, "Memberships", "Uber One"),
    (r"UBER\s*\*?\s*EATS|UBEREATS", "Uber Eats", "Uber", F, "Dining", "Delivery"),
    (r"\bUBER\b|UBER\s*\*", "Uber", "Uber", T, "Ground transportation", "Rideshare"),
    (r"\bLYFT\b", "Lyft", "Lyft", T, "Ground transportation", "Rideshare"),
    (r"TAXI|\bCAB\b|YELLOW CAB|CURB MOBILITY|FREENOW|BOLT\.EU|GRAB\b", None, None, T, "Ground transportation", "Taxi"),
    (r"AMTRAK|EUROSTAR|TRENITALIA|ITALO|SNCF|DB BAHN|DEUTSCHE BAHN|RENFE|SBB|VIA RAIL|JR EAST|JR CENTRAL|BRIGHTLINE",
     None, None, T, "Ground transportation", "Rail"),
    (r"METRO TRANSIT|\bMTA\b|\bBART\b|CLIPPER|\bTFL\b|TRANSIT|OYSTER CARD|VENTRA|MARTA", None, None, T, "Ground transportation", "Public transit"),
    (r"HERTZ", "Hertz", "Hertz", T, "Rental car", "Hertz"),
    (r"\bAVIS\b", "Avis", "Avis Budget", T, "Rental car", "Avis"),
    (r"BUDGET RENT|BUDGET CAR", "Budget", "Avis Budget", T, "Rental car", "Budget"),
    (r"NATIONAL CAR|NATIONAL RENT", "National", "Enterprise Holdings", T, "Rental car", "National"),
    (r"ENTERPRISE RENT|ENTERPRISE CAR|\bERAC\b", "Enterprise", "Enterprise Holdings", T, "Rental car", "Enterprise"),
    (r"ALAMO DRAFT", "Alamo Drafthouse", "Alamo Drafthouse", E, "Recreation", "Movies"),
    (r"ALAMO RENT|ALAMO CAR|^ALAMO\b", "Alamo", "Enterprise Holdings", T, "Rental car", "Alamo"),
    (r"\bSIXT\b|THRIFTY|DOLLAR RENT|\bTURO\b|EUROPCAR", None, None, T, "Rental car", "Other rental"),
    (r"CARNIVAL|ROYAL CARIBBEAN|NORWEGIAN CRUISE|CELEBRITY CRUISE|PRINCESS CRUISE|DISNEY CRUISE|VIKING (CRUISE|RIVER|OCEAN)|HOLLAND AMERICA",
     None, None, T, "Cruise", "Cruise line"),
    (r"PARK N FLY|THE PARKING SPOT|PARKING SPOT|AIRPORT PARK|AIRPORT.*PARKING|PARKING.*AIRPORT",
     None, None, T, "Parking / tolls", "Airport parking"),
    (r"\bCLEAR\b.*(MEMBER|SECURE)|CLEARME|TSA PRE|GLOBAL ENTRY|NEXUS", None, None, T, "Other travel", "Trusted traveler"),
    (r"GOGO|VIASAT|INFLIGHT WI|PANASONIC AVIONICS", None, None, T, "Other travel", "Inflight wifi"),
    (r"ALLIANZ TRAVEL|TRAVEL GUARD|TRAVEL INSURANCE|WORLD NOMADS", None, None, FIN, "Insurance", "Travel insurance"),
    (r"PARKMOBILE|PARK ?WHIZ|SPOTHERO|\bSP\+|SP PLUS|LAZ PARK|ABM PARK|PASSPORT PARK|PAYBYPHONE|PARKING|IMPARK|PREMIUM PARKING",
     None, None, TR, "Parking / tolls", "Parking"),
    (r"TOLL|E-?ZPASS|EXPRESS LANE|E-?PASS|SUNPASS|FASTRAK|I-?PASS|PIKEPASS|TXTAG|PLATEPASS", None, None, TR, "Parking / tolls", "Tolls"),
    # --- entertainment venues (before conflicting retail/utility names) ---
    # Flow: generic venue words. Venues named after a sponsor are in SPONSOR_VENUES (checked
    # first); teams are left to the AI, which knows them all.
    (r"\bSTADIUM\b|\bARENA\b|BALLPARK|\bCOLISEUM\b|AMPHITHEAT", None, None, E, "Sports tickets", "Venue"),
    (r"\bMLB\b|\bNFL\b|\bNBA\b|\bWNBA\b|\bNHL\b|\bMLS\b|\bNWSL\b", None, None, E, "Sports tickets", "Team / league"),
    (r"TICKETMASTER|LIVE NATION|LIVENATION|\bAXS\b|STUBHUB|SEATGEEK|VIVID SEATS|EVENTBRITE|TICKETWEB|SEE TICKETS",
     None, None, E, "Concerts / events", "Ticketing"),
    # --- food: delivery / coffee / fast casual ---
    (r"DOORDASH.*DASHPASS|DASHPASS", "DoorDash DashPass", "DoorDash", S, "Memberships", "DashPass"),
    (r"DOORDASH|^DD \*|GRUBHUB|SEAMLESS|POSTMATES|CAVIAR|GOPUFF", None, None, F, "Dining", "Delivery"),
    (r"INSTACART|SHIPT|AMAZON FRESH|AMZN FRESH", None, None, F, "Groceries", "Grocery delivery"),
    (r"STARBUCKS", "Starbucks", "Starbucks", F, "Dining", "Coffee / cafe"),
    (r"PEET'?S|DUTCH BROS|TIM HORTONS|BLUE BOTTLE|DUNKIN|COFFEE|ESPRESSO|ROASTER", None, None, F, "Dining", "Coffee / cafe"),
    (r"CHIPOTLE|PANERA|CHICK-?FIL|MCDONALD|CULVER|JIMMY JOHN|POTBELLY|NOODLES & CO|QDOBA|SUBWAY|TACO BELL|WENDY|BURGER KING|"
     r"FIVE GUYS|SHAKE SHACK|SWEETGREEN|\bCAVA\b|PANDA EXPRESS|JERSEY MIKE|FIREHOUSE SUB|DAIRY QUEEN|ARBY|RAISING CANE|"
     r"WINGSTOP|DOMINO|PIZZA HUT|PAPA JOHN|LITTLE CAESAR|PORTILLO|SMASHBURGER|FREDDY'?S|WHITE CASTLE|SONIC DRIVE|"
     r"CHICK FIL|IN-N-OUT|WHATABURGER|EINSTEIN|BRUEGGER|CRUMBL|JAMBA|SMOOTHIE KING|TROPICAL SMOOTHIE",
     None, None, F, "Dining", "Fast casual / quick service"),
    # --- groceries ---
    (r"WHOLE ?FOODS|WHOLEFDS", "Whole Foods", "Amazon", F, "Groceries", "Specialty grocery"),
    (r"TRADER JOE", "Trader Joe's", "Trader Joe's", F, "Groceries", "Specialty grocery"),
    (r"FRESH THYME|MOM'?S ORGANIC|SPROUTS|NATURAL GROCERS|EATALY|BUTCHER|MEAT MARKET|FISH MARKET|FROMAGE|CHEESE SHOP|BAKERY|PATISSERIE",
     None, None, F, "Groceries", "Specialty grocery"),
    (r"HY-?VEE|KROGER|SAFEWAY|ALBERTSONS|PUBLIX|\bHEB\b|H-E-B|WEGMANS|MEIJER|\bALDI\b|FOOD LION|GIANT EAGLE|"
     r"JEWEL|MARIANO|STOP & SHOP|SHOPRITE|HARRIS TEETER|WINCO|PIGGLY|GROCER|SUPERMARKET",
     None, None, F, "Groceries", "Traditional grocery"),
    # --- alcohol / wine ---
    (r"WINEDIRECT|COMMERCE7|VINOSHIPPER|VIN65|ORDERPORT|WINE ?CLUB|WINERY|VINEYARD|VINEYARDS|\bCELLARS\b|VINTNERS|WINE ESTATE|ESTATE WINER",
     None, None, F, "Alcohol / Wine", "Direct winery / wine club"),
    (r"TOTAL WINE|LIQUOR|WINE & SPIRITS|WINE AND SPIRITS|BEVMO|SPEC'?S|BINNY|WINE\.COM|DRIZLY|VIVINO|K&L WINE|"
     r"LAST BOTTLE|WINE ACCESS|BOTTLE SHOP|WINE MERCHANT|WINE SHOP",
     None, None, F, "Alcohol / Wine", "Wine / liquor retailer"),
    (r"BREWING|BREWERY|BREWHOUSE|TAPROOM|DISTILL|CIDERY|MEADERY", None, None, F, "Alcohol / Wine", "Brewery / distillery / winery"),
    # --- Amazon family (specific before generic) ---
    (r"AMAZON WEB SERVICES|\bAWS\b", "Amazon Web Services", "Amazon", DIG, "Cloud / technology", "AWS"),
    (r"PRIME VIDEO|AMAZON VIDEO", "Prime Video", "Amazon", DIG, "Streaming", "Video"),
    (r"AUDIBLE", "Audible", "Amazon", DIG, "Streaming", "Audio / books"),
    (r"KINDLE", "Kindle", "Amazon", S, "Specialty retail", "Books / digital"),
    (r"AMAZON PRIME|PRIME MEMBER|AMZN PRIME|PRIME\*", "Amazon Prime", "Amazon", S, "Memberships", "Amazon Prime"),
    (r"AMZN|AMAZON", "Amazon", "Amazon", S, "General retail / online", "Amazon"),
    # --- warehouse / big box ---
    (r"COSTCO GAS|COSTCO FUEL", "Costco Gas", "Costco", TR, "Fuel", "Warehouse fuel"),
    (r"COSTCO", "Costco", "Costco", S, "Warehouse club", "Costco"),
    (r"SAM'?S CLUB|SAMSCLUB", "Sam's Club", "Walmart", S, "Warehouse club", "Sam's Club"),
    (r"WALMART\+|WALMART PLUS", "Walmart+", "Walmart", S, "Memberships", "Walmart+"),
    (r"WAL-?MART|WM SUPERCENTER", "Walmart", "Walmart", S, "General retail / online", "Mass merchant"),
    (r"^TARGET\b|TARGET\.COM|TARGET\s+\d|TARGET T-", "Target", "Target", S, "General retail / online", "Mass merchant"),
    # --- department / apparel / electronics / sporting / home goods ---
    # Flow: \bMACY'?S?\b, not MACY, which matched inside "PHARMACY" and filed CVS/Walgreens as department stores.
    (r"NORDSTROM|\bMACY'?S?\b|KOHL'?S|JCPENNEY|DILLARD|\bSAKS\b|NEIMAN|BLOOMINGDALE|TJ ?MAXX|MARSHALLS|HOMEGOODS|ROSS STORES",
     None, None, S, "Department store", "Department / off-price"),
    (r"LULULEMON|\bNIKE\b|\bGAP\b|OLD NAVY|BANANA REP|J\.? ?CREW|UNIQLO|\bZARA\b|H&M|ATHLETA|VUORI|RHONE|BONOBOS|"
     r"ABERCROMBIE|AMERICAN EAGLE|EXPRESS\b|BROOKS BROTHERS|JOS A BANK|MEN'?S WEARHOUSE|NEW BALANCE|ALLBIRDS|ON RUNNING|HOKA",
     None, None, S, "Apparel", "Apparel / footwear"),
    (r"APPLE\.COM/BIL|ITUNES|APPLE SERVICES", "Apple Services", "Apple", DIG, "Cloud / technology", "Apple services (iCloud/App Store)"),
    (r"APPLE STORE|APPLE\.COM/US|APPLE ONLINE", "Apple Store", "Apple", S, "Electronics / technology", "Apple hardware"),
    (r"BEST ?BUY|B&H PHOTO|MICRO ?CENTER|NEWEGG|\bDELL\b|LENOVO|SAMSUNG|SONOS|ADORAMA|CRUTCHFIELD",
     None, None, S, "Electronics / technology", "Electronics retailer"),
    # Fitness and GPS device makers, each its own detail.
    (r"GARMIN", "Garmin", "Garmin", S, "Electronics / technology", "Garmin"),
    (r"FITBIT", "Fitbit", "Google", S, "Electronics / technology", "Fitbit"),
    (r"\bCOROS\b", "Coros", "Coros", S, "Electronics / technology", "Coros"),
    (r"POLAR ELECTRO|POLAR FLOW|POLAR\.COM", "Polar", "Polar", S, "Electronics / technology", "Polar"),
    (r"WAHOO FITNESS|WAHOOFITNESS", "Wahoo", "Wahoo", S, "Electronics / technology", "Wahoo"),
    (r"SUUNTO", "Suunto", "Suunto", S, "Electronics / technology", "Suunto"),
    (r"\bREI\b|DICK'?S SPORTING|CABELA|BASS PRO|BACKCOUNTRY|\bEVO\b|PATAGONIA|NORTH FACE|ARC'?TERYX|"
     r"RUNNING ROOM|FLEET FEET|BIKE|CYCLE|COMPETITIVE CYCLIST|ROAD RUNNER SPORTS|GOLF GALAXY|PGA TOUR SUPER",
     None, None, S, "Sporting goods / outdoors", "Sporting goods"),
    (r"ROOM ?& ?BOARD|ARHAUS|\bRH\b|RESTORATION HARDWARE|LA-Z-BOY|ETHAN ALLEN|ASHLEY FURN|FURNITURE|MATTRESS|SLEEP NUMBER",
     None, None, H, "Furniture", "Furniture"),
    (r"WAYFAIR|CRATE ?& ?BARREL|CRATE AND BARREL|POTTERY BARN|WILLIAMS[- ]?SONOMA|WEST ELM|BED BATH|\bIKEA\b|\bCB2\b|SUR LA TABLE|CONTAINER STORE",
     None, None, S, "Home goods", "Home goods"),
    (r"APPLIANCE|AJ MADISON|AJMADISON", None, None, H, "Appliances", "Appliances"),
    # --- home ---
    (r"HOME DEPOT|MENARDS|LOWE'?S|ACE HARDWARE|TRUE VALUE|SHERWIN|BENJAMIN MOORE|HARDWARE", None, None, H, "Home maintenance", "Home improvement store"),
    (r"TREE (CARE|SERVICE|EXPERT)|BARTLETT TREE|ARBOR|LAWN|LANDSCAP|IRRIGATION|SPRINKLER|SNOW REMOVAL|NURSERY|GARDEN CENTER",
     None, None, H, "Landscaping / tree care", "Landscaping / tree care"),
    (r"PLUMBING|HEATING|\bHVAC\b|ROOFING|CONSTRUCTION|CONTRACTING|REMODEL|PAINTING|ELECTRICAL|PEST CONTROL|EXTERMINAT|"
     r"CLEANING|MAID|MOLLY MAID|MERRY MAIDS|GUTTER|WINDOW|GARAGE DOOR|HANDYMAN|CARPET|FLOORING|CHIMNEY|SEPTIC|POOL SERV",
     None, None, H, "Contractors", "Home services / contractors"),
    (r"DUKE ENERGY|PG&E|CON ?ED|DOMINION ENERGY|WATER UTIL|UTILITY|UTILITIES|CITY OF .*WATER|WASTE MANAGEMENT|REPUBLIC SERVICES",
     None, None, BILLS, "Utilities", "Utilities"),
    # --- transportation ---
    (r"TESLA SUPERCHARGER|SUPERCHARGER|CHARGEPOINT|ELECTRIFY AMERICA|\bEVGO\b|BLINK CHARG|RIVIAN ADVENTURE NETWORK|IONNA", None, None, TR, "Fuel", "EV charging"),
    (r"\bSHELL\b|\bBP\b|EXXON|MOBIL\b|CHEVRON|CIRCLE ?K\b|SPEEDWAY|MARATHON PETRO|SUNOCO|CITGO|VALERO|PHILLIPS 66|CONOCO|"
     r"SINCLAIR|\bARCO\b|PILOT|FLYING J|LOVE'?S TRAVEL|CASEY'?S|QUIKTRIP|\bQT\b|WAWA|SHEETZ|BUC-?EE|"
     r"FUEL|GAS STATION",
     None, None, TR, "Fuel", "Gas station"),
    # Carmakers that sell direct (no dealership in between), each its own detail.
    (r"TESLA", "Tesla", "Tesla", TR, "Auto purchase / repair / maintenance", "Tesla"),
    (r"RIVIAN", "Rivian", "Rivian", TR, "Auto purchase / repair / maintenance", "Rivian"),
    (r"LUCID (MOTORS|GROUP|USA)", "Lucid", "Lucid", TR, "Auto purchase / repair / maintenance", "Lucid"),
    (r"POLESTAR", "Polestar", "Polestar", TR, "Auto purchase / repair / maintenance", "Polestar"),
    (r"JIFFY LUBE|VALVOLINE|DISCOUNT TIRE|\bTIRE\b|TIRES|FIRESTONE|MIDAS|MEINEKE|AUTO REPAIR|AUTOMOTIVE|COLLISION|CAR WASH|AUTOZONE|"
     r"O'?REILLY AUTO|NAPA AUTO|ADVANCE AUTO|DEALERSHIP|MOTORS\b|AUTO BODY|OIL CHANGE",
     None, None, TR, "Auto purchase / repair / maintenance", "Auto service"),
    (r"\bDMV\b|\bBMV\b|\bMVD\b|\bRMV\b|MOTOR VEHICLE|VEHICLE REG|LICENSE BUREAU|DRIVER (AND|&) VEHICLE|"
     r"DEPUTY REG|TAG AGENCY|TAG OFFICE|SECRETARY OF STATE|\bSOS\b.*(VEHICLE|DRIVER|TITLE)", None, None, TR, "Auto purchase / repair / maintenance", "Registration / licensing"),
    # --- health & personal ---
    (r"\bCVS\b|WALGREENS|RITE AID|PHARMACY|DRUG STORE|CAPSULE PHARM|ALTO PHARM", None, None, HP, "Pharmacy", "Pharmacy"),
    (r"DENTAL|DENTIST|ORTHODONT|ENDODONT|PERIODONT", None, None, HP, "Dental", "Dental"),
    (r"MEDSPA|MED SPA|AESTHETIC|BOTOX|LASER CLINIC|DERMAL|INJECTABLE|COOLSCULPT", None, None, HP, "Aesthetics", "Aesthetics"),
    (r"\bHIMS\b|\bHERS\b|\bRO\b.*HEALTH|NURX|ONE MEDICAL|FORWARD HEALTH|PARSLEY HEALTH|FUNCTION HEALTH|NOOM|LEVELS HEALTH|OURA",
     None, None, HP, "Subscription healthcare", "Subscription healthcare"),
    (r"CLINIC|MEDICAL|HOSPITAL|MINUTECLINIC|URGENT CARE|LABCORP|QUEST DIAG|PHYSICIAN|"
     r"DERMATOLOG|CHIROPRACT|PHYSICAL THERAPY|OPTOM|EYE CARE|LENSCRAFTERS|WARBY|RADIOLOGY|PEDIATRIC|\bMD\b|HEALTHCARE|PSYCH",
     None, None, HP, "Medical", "Medical"),
    (r"LIFE ?TIME|ORANGE ?THEORY|PELOTON|EQUINOX|ANYTIME FITNESS|PLANET FITNESS|\bF45\b|CROSSFIT|\bYMCA\b|YOGA|PILATES|CORE ?POWER|"
     r"STRAVA|ZWIFT|TRAINERROAD|WHOOP|\bGYM\b|FITNESS|ATHLETIC CLUB|RACE ?ENTRY|RUNSIGNUP|ACTIVE\.COM|ATHLINKS|IRONMAN",
     None, None, HP, "Fitness", "Fitness"),
    (r"SALON|BARBER|\bSPA\b|MASSAGE|GREAT CLIPS|SPORT CLIPS|NAIL|ULTA|SEPHORA|HAIR", None, None, HP, "Personal care", "Personal care"),
    # --- bills / digital & media ---
    (r"VERIZON|\bAT&?T\b|ATT\*|^ATT MOB|T-?MOBILE|MINT MOBILE|VISIBLE|GOOGLE ?FI|US MOBILE|CRICKET", None, None, BILLS, "Mobile phone", "Mobile carrier"),
    (r"COMCAST|XFINITY|SPECTRUM|CENTURYLINK|LUMEN|QUANTUM FIBER|STARLINK|FRONTIER COMM|GOOGLE FIBER|MEDIACOM|SPARKLIGHT",
     None, None, BILLS, "Internet", "Internet provider"),
    (r"DIRECTV|DISH NETWORK|YOUTUBE ?TV|SLING|FUBO", None, None, BILLS, "TV", "Live TV"),
    (r"NETFLIX|HULU|DISNEY ?PLUS|DISNEYPLUS|HBO|MAX\.COM|HBOMAX|PEACOCK|PARAMOUNT|SPOTIFY|APPLE ?MUSIC|YOUTUBE ?PREMIUM|"
     r"YOUTUBEPREMIUM|SIRIUS|PANDORA|TIDAL|ESPN\+|ESPN PLUS|CRUNCHYROLL|APPLE ?TV|STARZ|SHOWTIME|MLB\.TV|NBA LEAGUE PASS",
     None, None, DIG, "Streaming", "Streaming service"),
    (r"ADOBE|MICROSOFT|MSFT|OFFICE 365|DROPBOX|1PASSWORD|LASTPASS|BITWARDEN|OPENAI|CHATGPT|ANTHROPIC|CLAUDE\.AI|GITHUB|NOTION|"
     r"EVERNOTE|ZOOM\.US|ZOOM VIDEO|CANVA|GRAMMARLY|NORDVPN|EXPRESSVPN|JETBRAINS|CURSOR|MIDJOURNEY|PERPLEXITY",
     None, None, DIG, "Software", "Software subscription"),
    (r"GOOGLE ?CLOUD|GOOGLE ?STORAGE|GOOGLE ONE|ICLOUD|DIGITALOCEAN|LINODE|CLOUDFLARE|BACKBLAZE|GODADDY|NAMECHEAP|SQUARESPACE|WIX",
     None, None, DIG, "Cloud / technology", "Cloud / hosting"),
    (r"NYTIMES|NY TIMES|WSJ|WALL ST|WASHINGTON POST|ECONOMIST|THE ATHLETIC|SUBSTACK|MEDIUM\.COM|PATREON",
     None, None, DIG, "News & media", "News / media"),
    # --- entertainment / recreation ---
    (r"GOLF|BOWL|CINEMA|\bAMC\b|REGAL|MARCUS THEAT|MUSEUM|\bZOO\b|SKI\b|THEME PARK|SIX FLAGS|CEDAR FAIR|CEDAR POINT|KINGS ISLAND|KNOTT'?S|DOLLYWOOD|BUSCH GARDENS|SEAWORLD|DISNEYLAND|WALT DISNEY WORLD|"
     r"UNIVERSAL STUDIOS|AQUARIUM|ESCAPE ROOM|TOPGOLF|THEATER|THEATRE|ORCHESTRA|SYMPHONY|PHILHARMONIC|OPERA|BALLET",
     None, None, E, "Recreation", "Recreation / attractions"),
    (r"STEAM ?GAMES|STEAMPOWERED|PLAYSTATION|\bXBOX\b|NINTENDO|EPIC GAMES|BLIZZARD|RIOT GAMES", None, None, E, "Recreation", "Video games"),
    # --- education / family ---
    (r"TUTOR|KUMON|MATHNASIUM|SYLVAN|VARSITY TUTORS|OUTSCHOOL|HUNTINGTON LEARN", None, None, ED, "Tutoring", "Tutoring"),
    (r"MYSCHOOLBUCKS|SCHOOLPAY|PAYSCHOOLS|K12PAYMENT|INFINITE CAMPUS|SCHOOL DIST|ISD \d|USD \d|LUNCH", None, None, ED, "School-related", "School fees / lunch"),
    (r"TUITION|ACADEMY|MONTESSORI|UNIVERSITY|COLLEGE|SCHOOL", None, None, ED, "Tuition", "Tuition / school"),
    (r"SCOUT|\bBSA\b|CAMP\b|CAMPS|LESSONS|DANCE STUDIO|SWIM SCHOOL|LEAGUE|TEAM SNAP|TEAMSNAP|SPORTSENGINE|ACTIVITY", None, None, ED, "Activities / camps", "Activities / camps"),
    # --- financial / services / other ---
    (r"STATE FARM|GEICO|PROGRESSIVE|ALLSTATE|USAA|LIBERTY MUTUAL|FARMERS INS|NATIONWIDE|TRAVELERS INS|INSURANCE", None, None, FIN, "Insurance", "Insurance"),
    (r"TURBOTAX|INTUIT|H&R BLOCK|\bCPA\b|ACCOUNTING|LAW OFFICE|ATTORNEY|LEGAL|NOTARY", None, None, FIN, "Professional services", "Tax / legal / accounting"),
    (r"FIDELITY|VANGUARD|SCHWAB|COINBASE|ROBINHOOD|BROKERAGE", None, None, FIN, "Investment", "Investment"),
    (r"VENMO|ZELLE|CASH ?APP|PAYPAL(?!\s?\*)", None, None, "Transfers / P2P", "Person-to-person", "P2P via card"),
    (r"DONATION|FOUNDATION|CHURCH|GOFUNDME|CHARITY|RED CROSS|UNITED WAY|\bPBS\b|\bNPR\b|MINISTR|PARISH|SYNAGOGUE|TEMPLE ", None, None, GIVE, "Charitable giving", "Donation"),
    (r"\bIRS\b|US TREASURY|DEPT OF REVENUE|DEPARTMENT OF REVENUE|PASSPORT|COUNTY|CITY OF|STATE OF", None, None, GOV, "Government fees", "Government / taxes"),
    (r"PETCO|PETSMART|CHEWY|VETERIN|ANIMAL HOSP|BANFIELD|PET CLINIC|\bVET\b|PET SUPPL|GROOMING|DOGGY|KENNEL|ROVER\.COM|WAG\b",
     None, None, PETS, "Pet care", "Pet care"),
    (r"\bUSPS\b|UPS STORE|THE UPS|FEDEX|DHL", None, None, SVC, "Shipping / postal", "Shipping"),
    # Flow: self-storage
    (r"EXTRA SPACE|PUBLIC STORAGE|CUBESMART|LIFE STORAGE|U-?HAUL STORAGE|STORAGE UNIT|SELF STORAGE",
     None, None, SVC, "Storage", "Self storage"),
    (r"ETSY|EBAY|WALGREEN PHOTO|SHUTTERFLY|MINTED|ZAZZLE|MICHAELS|HOBBY LOBBY|JO-?ANN|BARNES|BOOKS|STAPLES|OFFICE DEPOT|OFFICEMAX|PARTY CITY|JEWEL(ER|RY)",
     None, None, S, "Specialty retail", "Specialty retail"),
]

# ---------------------------------------------------------------------------
# Flow: common US merchants the base rules do not name. Checked before
# MERCHANT_RULES. Every entry is a specific brand with an explicit canonical
# name, so a hit is a confident "merchant rule" and is never sent to the AI.
# Patterns allow optional spaces ("DOLLAR ?GENERAL") because issuers glue words;
# separators (_ - . /) are already normalized to spaces by classify_merchant.
# ---------------------------------------------------------------------------
_GROC, _SPEC, _FUEL = ("Groceries", "Traditional grocery"), ("Groceries", "Specialty grocery"), ("Fuel", "Gas station")
_FAST, _FULL, _CAFE = ("Dining", "Fast casual / quick service"), ("Dining", "Full service"), ("Dining", "Coffee / cafe")
_UTIL, _NET, _MOB = ("Utilities", "Utilities"), ("Internet", "Internet provider"), ("Mobile phone", "Mobile carrier")
_SOFT, _STREAM = ("Software", "Software subscription"), ("Streaming", "Streaming service")
_AIR, _HOTEL = ("Airfare", "Other airlines"), ("Lodging", "Other hotels")


def _brands(l1: str, l2l3: tuple, *entries: tuple) -> list[tuple]:
    """(regex, canonical[, family]) entries sharing one category."""
    return [(e[0], e[1], e[2] if len(e) > 2 else e[1], l1, l2l3[0], l2l3[1]) for e in entries]


COMMON_MERCHANT_RULES = [
    # --- groceries ---
    *_brands(F, _GROC,
             (r"\bRALPHS\b", "Ralphs", "Kroger"), (r"FRED ?MEYER", "Fred Meyer", "Kroger"),
             (r"KING ?SOOPERS", "King Soopers", "Kroger"), (r"\bDILLONS\b", "Dillons", "Kroger"),
             (r"FRY'?S (FOOD|MARKETPLACE)|FRYS FOOD", "Fry's Food", "Kroger"),
             (r"SMITH'?S (FOOD|MARKETPLACE)|SMITHS FOOD", "Smith's Food", "Kroger"),
             (r"\bQFC\b|QUALITY FOOD CENTER", "QFC", "Kroger"), (r"FOOD ?4 ?LESS", "Food 4 Less", "Kroger"),
             (r"\bVONS\b", "Vons", "Albertsons"), (r"\bPAVILIONS\b", "Pavilions", "Albertsons"),
             (r"\bACME (MARKETS?|MKT)", "Acme Markets", "Albertsons"), (r"TOM THUMB", "Tom Thumb", "Albertsons"),
             (r"\bRANDALLS\b", "Randalls", "Albertsons"), (r"SHAW'?S (SUPERMARKET|\d)", "Shaw's", "Albertsons"),
             (r"HANNAFORD", "Hannaford"), (r"MARKET ?BASKET|DEMOULAS", "Market Basket"),
             (r"GIANT (FOOD|MARTIN)|GIANT ?\d", "Giant"), (r"\bWEIS (MARKETS?|\d)", "Weis Markets"),
             (r"WINN ?DIXIE", "Winn-Dixie"), (r"SAVE ?A ?LOT", "Save A Lot"),
             (r"GROCERY ?OUTLET", "Grocery Outlet"), (r"STATER ?BRO", "Stater Bros"),
             (r"PRICE ?CHOPPER", "Price Chopper"), (r"TOPS (MARKETS?|FRIENDLY)", "Tops Markets"),
             (r"LOWES FOODS", "Lowes Foods"), (r"INGLES (MARKETS?|\d)", "Ingles"), (r"\bBI ?LO\b", "BI-LO"),
             (r"SCHNUCK", "Schnucks"), (r"DIERBERG", "Dierbergs"), (r"FAREWAY", "Fareway"),
             (r"SMART ?& ?FINAL", "Smart & Final"), (r"RALEY'?S", "Raley's"), (r"FOOD CITY", "Food City"),
             (r"FAMILY FARE", "Family Fare"), (r"FIESTA MART", "Fiesta Mart"), (r"CARDENAS MARKET", "Cardenas"),
             (r"SEDANO'?S", "Sedano's"), (r"HARMONS", "Harmons"), (r"PICK ?'?N ?SAVE", "Pick 'n Save", "Kroger"),
             (r"\bMETRO MARKET\b", "Metro Market", "Kroger")),
    *_brands(F, _SPEC,
             (r"(THE )?FRESH MARKET\b", "The Fresh Market"), (r"EARTH ?FARE", "Earth Fare"),
             (r"BRISTOL FARMS", "Bristol Farms"), (r"GELSON'?S", "Gelson's"), (r"NUGGET MARKETS?", "Nugget Markets"),
             (r"\bH ?MART\b", "H Mart"), (r"99 RANCH", "99 Ranch Market")),
    *_brands(F, ("Groceries", "Grocery delivery"),
             (r"HELLO ?FRESH", "HelloFresh"), (r"FACTOR ?75|FACTOR MEALS|FACTOR_?\*", "Factor", "HelloFresh"),
             (r"BLUE ?APRON", "Blue Apron"), (r"HOME ?CHEF", "Home Chef"), (r"GREEN ?CHEF", "Green Chef", "HelloFresh"),
             (r"THRIVE ?MARKET", "Thrive Market"), (r"MISFITS MARKET", "Misfits Market")),
    # --- fuel and convenience ---
    *_brands(TR, _FUEL,
             (r"\b7 ?ELEVEN\b|SEVEN ?ELEVEN", "7-Eleven"), (r"RACE ?TRAC|\bRACEWAY\b", "RaceTrac"),
             (r"MURPHY ?(USA|EXPRESS)", "Murphy USA"), (r"MAVERIK", "Maverik"), (r"\bGET ?GO\b", "GetGo"),
             (r"CUMBERLAND FARMS", "Cumberland Farms"), (r"THORNTONS", "Thorntons"), (r"TEXACO", "Texaco"),
             (r"\bHESS\b", "Hess"), (r"KUM ?& ?GO", "Kum & Go"), (r"ROYAL FARMS", "Royal Farms"),
             (r"STEWART'?S SHOP", "Stewart's Shops"), (r"\bCENEX\b", "Cenex"), (r"GULF OIL", "Gulf"),
             (r"\bSPINX\b", "Spinx"), (r"\bONCUE\b", "OnCue"), (r"SUPER ?AMERICA", "SuperAmerica"),
             (r"ALLSUP'?S", "Allsup's"), (r"TA TRAVEL CENTER|PETRO STOPPING", "TravelCenters of America"),
             (r"SAM'?S ?(CLUB )?FUEL|SAMS ?CLUB ?GAS", "Sam's Club Fuel", "Walmart"),
             (r"BJ'?S (FUEL|GAS)", "BJ's Fuel", "BJ's Wholesale"), (r"KWIK ?(TRIP|STAR)", "Kwik Trip"),
             (r"HOLIDAY STATION|HOLIDAY \d", "Holiday")),
    # --- restaurants ---
    *_brands(F, _FAST,
             (r"\bKFC\b|KENTUCKY FRIED", "KFC"), (r"POPEYES", "Popeyes"), (r"JACK IN THE BOX", "Jack in the Box"),
             (r"CARL'?S ?JR", "Carl's Jr."), (r"HARDEE'?S", "Hardee's"), (r"ZAXBY", "Zaxby's"),
             (r"BOJANGLES", "Bojangles"), (r"DEL TACO", "Del Taco"), (r"EL POLLO LOCO", "El Pollo Loco"),
             (r"CHURCH'?S (CHICKEN|TEXAS)", "Church's Chicken"), (r"CHECKERS DRIVE|\bRALLY'?S\b", "Checkers"),
             (r"STEAK ?N ?SHAKE", "Steak 'n Shake"), (r"KRISPY ?KREME", "Krispy Kreme"), (r"CINNABON", "Cinnabon"),
             (r"AUNTIE ANNE", "Auntie Anne's"), (r"BASKIN", "Baskin-Robbins"), (r"COLD ?STONE", "Cold Stone"),
             (r"MOE'?S SOUTHWEST", "Moe's Southwest Grill"), (r"BAJA FRESH", "Baja Fresh"),
             (r"MCALISTER", "McAlister's Deli"), (r"JASON'?S DELI", "Jason's Deli"), (r"SCHLOTZSKY", "Schlotzsky's"),
             (r"TACO JOHN", "Taco John's"), (r"TACO CABANA", "Taco Cabana"), (r"HABIT BURGER", "The Habit"),
             (r"\bMOD ?PIZZA", "MOD Pizza"), (r"BLAZE PIZZA", "Blaze Pizza"), (r"MARCO'?S PIZZA", "Marco's Pizza"),
             (r"PAPA ?MURPHY", "Papa Murphy's"), (r"HUNGRY HOWIE", "Hungry Howie's"), (r"PEI WEI", "Pei Wei"),
             (r"CAPTAIN D'?S", "Captain D's"), (r"LONG JOHN SILVER", "Long John Silver's"),
             (r"NOTHING BUNDT", "Nothing Bundt Cakes"), (r"INSOMNIA COOKIES", "Insomnia Cookies"),
             (r"PIZZA RANCH", "Pizza Ranch"), (r"SBARRO", "Sbarro"), (r"WETZEL", "Wetzel's Pretzels"),
             (r"PANDA ?EXPRESS", "Panda Express"), (r"CHICK ?FIL ?A", "Chick-fil-A")),
    *_brands(F, _FULL,
             (r"APPLEBEE", "Applebee's"), (r"\bCHILI'?S\b", "Chili's"), (r"OLIVE ?GARDEN", "Olive Garden"),
             (r"RED LOBSTER", "Red Lobster"), (r"OUTBACK", "Outback Steakhouse"),
             (r"TEXAS ROADHOUSE", "Texas Roadhouse"), (r"LONGHORN STEAK", "LongHorn Steakhouse"),
             (r"CRACKER ?BARREL", "Cracker Barrel"), (r"BUFFALO WILD WINGS|\bBWW\b", "Buffalo Wild Wings"),
             (r"RED ROBIN", "Red Robin"), (r"CHEESECAKE FACTORY", "The Cheesecake Factory"),
             (r"\bP ?F ?CHANG", "P.F. Chang's"), (r"TGI ?FRIDAY", "TGI Fridays"), (r"RUBY TUESDAY", "Ruby Tuesday"),
             (r"BJ'?S (RESTAURANT|BREWHOUSE)", "BJ's Restaurant"), (r"HOOTERS", "Hooters"),
             (r"WAFFLE HOUSE", "Waffle House"), (r"BOB EVANS", "Bob Evans"), (r"PERKINS", "Perkins"),
             (r"FIRST WATCH", "First Watch"), (r"CARRABBA", "Carrabba's"), (r"MAGGIANO", "Maggiano's"),
             (r"YARD HOUSE", "Yard House"), (r"DENNY'?S", "Denny's"), (r"\bIHOP\b", "IHOP"),
             (r"TWIN PEAKS", "Twin Peaks"), (r"\bCHUY'?S\b", "Chuy's"), (r"BONEFISH", "Bonefish Grill"),
             (r"RUTH'?S CHRIS", "Ruth's Chris"), (r"FOGO DE CHAO", "Fogo de Chão"), (r"BENIHANA", "Benihana"),
             (r"OLD CHICAGO", "Old Chicago"), (r"BAR LOUIE", "Bar Louie"), (r"ON THE BORDER", "On The Border"),
             (r"CALIFORNIA PIZZA KITCHEN|\bCPK\b", "California Pizza Kitchen"), (r"HOULIHAN", "Houlihan's"),
             (r"CHEDDAR'?S", "Cheddar's"), (r"O'?CHARLEY", "O'Charley's"), (r"GOLDEN CORRAL", "Golden Corral"),
             (r"SALTGRASS", "Saltgrass"), (r"KONA GRILL", "Kona Grill"), (r"SEASONS 52", "Seasons 52")),
    *_brands(F, _CAFE,
             (r"BIGGBY", "Biggby Coffee"), (r"SCOOTER'?S COFFEE", "Scooter's Coffee"), (r"PJ'?S COFFEE", "PJ's Coffee"),
             (r"GREGORYS COFFEE", "Gregorys Coffee"), (r"BLACK RIFLE", "Black Rifle Coffee"),
             (r"CARIBOU", "Caribou Coffee")),
    # --- retail ---
    *_brands(S, ("General retail / online", "Discount store"),
             (r"DOLLAR ?GENERAL", "Dollar General"), (r"DOLLAR ?TREE", "Dollar Tree"),
             (r"FAMILY ?DOLLAR", "Family Dollar"), (r"FIVE ?BELOW", "Five Below"), (r"BIG ?LOTS", "Big Lots"),
             (r"OLLIE'?S BARGAIN", "Ollie's"), (r"\bTEMU\b", "Temu"), (r"\bSHEIN\b", "Shein"),
             (r"ALIEXPRESS", "AliExpress"), (r"OVERSTOCK", "Overstock"), (r"\bQVC\b", "QVC"), (r"\bHSN\b", "HSN")),
    *_brands(S, ("Warehouse club", "BJ's"), (r"BJ'?S (WHOLESALE|WHSE|CLUB)|BJS WHOLESALE", "BJ's Wholesale")),
    *_brands(S, ("Department store", "Department / off-price"),
             (r"BURLINGTON (STORES?|COAT|\d)", "Burlington"), (r"\bBELK\b", "Belk"), (r"BOSCOV", "Boscov's"),
             (r"VON MAUR", "Von Maur"), (r"HOMESENSE", "HomeSense", "TJX")),
    *_brands(S, ("Apparel", "Apparel / footwear"),
             (r"VICTORIA'?S SECRET", "Victoria's Secret"), (r"FOOT ?LOCKER", "Foot Locker"), (r"\bDSW\b", "DSW"),
             (r"FAMOUS FOOTWEAR", "Famous Footwear"), (r"SKECHERS", "Skechers"), (r"\bZAPPOS\b", "Zappos", "Amazon"),
             (r"CARTER'?S\b", "Carter's"), (r"CHILDREN'?S PLACE", "The Children's Place"), (r"JOURNEYS (SHOES|KIDZ|#|\d)", "Journeys"),
             (r"ZUMIEZ", "Zumiez"), (r"HOT TOPIC", "Hot Topic")),
    *_brands(S, ("Specialty retail", "Specialty retail"),
             (r"BATH ?& ?BODY", "Bath & Body Works"), (r"CLAIRE'?S\b", "Claire's"), (r"SPENCER GIFTS", "Spencer's")),
    *_brands(S, ("Electronics / technology", "Electronics retailer"), (r"GAME ?STOP", "GameStop")),
    *_brands(S, ("Sporting goods / outdoors", "Sporting goods"),
             (r"ACADEMY SPORTS", "Academy Sports"), (r"BIG 5 SPORTING", "Big 5 Sporting Goods"),
             (r"SCHEELS", "Scheels"), (r"FLEET FARM", "Fleet Farm")),
    *_brands(S, ("Home goods", "Home goods"), (r"\bAT HOME\b", "At Home"), (r"KIRKLAND'?S", "Kirkland's")),
    *_brands(H, ("Home maintenance", "Home improvement store"),
             (r"TRACTOR SUPPLY", "Tractor Supply"), (r"HARBOR FREIGHT", "Harbor Freight"),
             (r"NORTHERN TOOL", "Northern Tool"), (r"FLOOR ?& ?DECOR", "Floor & Decor"), (r"84 LUMBER", "84 Lumber")),
    *_brands(H, ("Home maintenance", "Home security"),
             (r"\bRING (YEARLY|MONTHLY|BASIC|PROTECT)|\bRING COM\b", "Ring", "Amazon"),
             (r"SIMPLI ?SAFE", "SimpliSafe"), (r"\bADT\b", "ADT"), (r"VIVINT", "Vivint")),
    # --- utilities and telecom ---
    *_brands(BILLS, _UTIL,
             (r"NATIONAL ?GRID", "National Grid"), (r"EVERSOURCE", "Eversource"),
             (r"SOUTHERN CALIFORNIA EDISON|SO CAL EDISON", "Southern California Edison"),
             (r"SDG ?& ?E|SAN DIEGO GAS", "SDG&E"), (r"PSE ?& ?G\b|\bPSEG\b", "PSE&G"),
             (r"\bFPL\b|FLORIDA POWER", "Florida Power & Light"), (r"GEORGIA POWER", "Georgia Power"),
             (r"ALABAMA POWER", "Alabama Power"), (r"ENTERGY", "Entergy"), (r"AMEREN", "Ameren"),
             (r"CONSUMERS ENERGY", "Consumers Energy"), (r"\bDTE\b", "DTE Energy"),
             (r"\bCOMED\b|COMMONWEALTH EDISON", "ComEd"), (r"\bPECO\b", "PECO"), (r"PPL ELECTRIC", "PPL Electric"),
             (r"AMERICAN ELECTRIC POWER|APPALACHIAN POWER|\bAEP\b", "AEP"), (r"FIRST ?ENERGY", "FirstEnergy"),
             (r"ATMOS ENERGY", "Atmos Energy"), (r"\bNICOR\b", "Nicor Gas"), (r"PIEDMONT NATURAL", "Piedmont Natural Gas"),
             (r"\bSRP\b|SALT RIVER PROJECT", "SRP"), (r"ARIZONA PUBLIC SERV|\bAPS ELECTRIC", "APS"),
             (r"NV ENERGY", "NV Energy"), (r"ROCKY MOUNTAIN POWER|PACIFICORP", "Rocky Mountain Power"),
             (r"\bAVISTA\b", "Avista"), (r"\bEVERGY\b", "Evergy"), (r"TXU ENERGY", "TXU Energy"),
             (r"RELIANT ENERGY", "Reliant Energy"), (r"CPS ENERGY", "CPS Energy"), (r"AMERICAN WATER", "American Water"),
             (r"\bXCEL\b", "Xcel Energy"), (r"CENTERPOINT", "CenterPoint Energy")),
    *_brands(BILLS, _NET,
             (r"\bCOX (COMM|CABLE|COM\b)", "Cox"), (r"OPTIMUM|ALTICE", "Optimum"), (r"WINDSTREAM|KINETIC BY", "Windstream"),
             (r"\bZIPLY\b", "Ziply Fiber"), (r"METRONET", "Metronet"), (r"\bRCN\b", "RCN"), (r"ASTOUND", "Astound"),
             (r"WIDEOPENWEST|WOW INTERNET", "WOW!"), (r"BREEZELINE", "Breezeline"), (r"HUGHESNET", "HughesNet")),
    *_brands(BILLS, _MOB,
             (r"BOOST MOBILE", "Boost Mobile"), (r"CONSUMER CELLULAR", "Consumer Cellular"),
             (r"STRAIGHT ?TALK", "Straight Talk"), (r"TRAC ?FONE", "Tracfone"), (r"TOTAL (WIRELESS|BY VERIZON)", "Total Wireless"),
             (r"U ?S CELLULAR", "UScellular"), (r"XFINITY MOBILE", "Xfinity Mobile", "Comcast"),
             (r"SPECTRUM MOBILE", "Spectrum Mobile", "Charter")),
    # --- digital & media ---
    *_brands(DIG, _STREAM,
             (r"DISCOVERY ?(PLUS|\+)", "Discovery+"), (r"\bPHILO\b", "Philo"), (r"BRITBOX", "BritBox"),
             (r"ACORN ?TV", "Acorn TV"), (r"\bMUBI\b", "MUBI"), (r"CRITERION CHANNEL", "Criterion Channel"),
             (r"AMC ?(PLUS|\+)", "AMC+"), (r"\bTWITCH\b", "Twitch", "Amazon")),
    *_brands(DIG, _SOFT,
             (r"DUOLINGO", "Duolingo"), (r"HEADSPACE", "Headspace"), (r"\bCALM (COM|APP)\b", "Calm"),
             (r"LINKEDIN", "LinkedIn", "Microsoft"), (r"\bSLACK\b", "Slack"),
             (r"GOOGLE ?WORKSPACE|G ?SUITE", "Google Workspace", "Google"),
             (r"NORTON|GEN DIGITAL", "Norton"), (r"MCAFEE", "McAfee"), (r"PROTON ?(MAIL|AG|VPN)", "Proton"),
             (r"AUTODESK", "Autodesk"), (r"DOCUSIGN", "DocuSign"), (r"QUICKBOOKS", "QuickBooks", "Intuit"),
             (r"\bDISCORD\b", "Discord"), (r"\bLOOM\b", "Loom"), (r"YNAB|YOU NEED A BUDGET", "YNAB"),
             (r"ROCKET MONEY|TRUEBILL", "Rocket Money"), (r"CREDIT KARMA|EXPERIAN|EQUIFAX|TRANSUNION", "Credit monitoring")),
    *_brands(SVC, ("Memberships", "Memberships / dues"),
             (r"AAA (MEMBERSHIP|MEMBER|AUTO|CLUB)|\bAAA COM\b", "AAA")),
    *_brands(ED, ("Tuition", "Online courses"),
             (r"MASTERCLASS", "MasterClass"), (r"COURSERA", "Coursera"), (r"UDEMY", "Udemy"),
             (r"SKILLSHARE", "Skillshare"), (r"\bCHEGG\b", "Chegg")),
    # --- travel ---
    *_brands(T, _AIR,
             (r"WESTJET", "WestJet"), (r"QATAR AIR", "Qatar Airways"),
             (r"TURKISH AIR", "Turkish Airlines"), (r"AER LINGUS", "Aer Lingus"), (r"QANTAS", "Qantas"),
             (r"\bIBERIA\b", "Iberia"), (r"SWISS (INTL|INTERNATIONAL|AIR)", "SWISS"), (r"FINNAIR", "Finnair"),
             (r"ETIHAD", "Etihad"), (r"SINGAPORE AIR", "Singapore Airlines"), (r"CATHAY PACIFIC", "Cathay Pacific"),
             (r"ALL NIPPON|\bANA AIR", "ANA"), (r"JAPAN AIRLINES", "Japan Airlines"), (r"VOLARIS", "Volaris"),
             (r"PORTER AIR", "Porter Airlines"), (r"COPA AIR", "Copa Airlines"), (r"AVIANCA", "Avianca"),
             (r"\bLATAM\b", "LATAM"), (r"TAP (AIR|PORTUGAL)", "TAP Air Portugal"), (r"NORSE ATLANTIC", "Norse Atlantic")),
    *_brands(T, ("Lodging", "Accor"), (r"FAIRMONT", "Fairmont", "Accor")),
    *_brands(T, _HOTEL,
             (r"RED ROOF", "Red Roof"), (r"MOTEL ?6", "Motel 6"), (r"EXTENDED STAY", "Extended Stay America"),
             (r"OMNI (HOTEL|RESORT)", "Omni Hotels"), (r"LOEWS (HOTEL|RESORT)", "Loews Hotels"),
             (r"FOUR SEASONS", "Four Seasons"),
             (r"DRURY (INN|HOTEL|PLAZA)", "Drury Hotels"), (r"GREAT WOLF", "Great Wolf Lodge"),
             (r"SONESTA", "Sonesta"), (r"GRADUATE HOTEL", "Graduate Hotels")),
    *_brands(T, ("Booking agency / OTA", "Other OTA"), (r"HOTEL ?TONIGHT", "HotelTonight", "Airbnb")),
    *_brands(T, ("Rental car", "Car sharing"), (r"ZIPCAR", "Zipcar", "Avis Budget"), (r"GETAROUND", "Getaround")),
    *_brands(T, ("Ground transportation", "Micromobility"),
             (r"\bLIME\b ?\*|LIME ?BIKE|LIME RIDE", "Lime"), (r"\bBIRD (APP|RIDE)", "Bird"),
             (r"CITI ?BIKE|\bDIVVY\b|BLUE ?BIKES|NICE ?RIDE|CAPITAL BIKESHARE|BAY WHEELS|METRO BIKE|\bINDEGO\b|"
              r"BIKETOWN|BCYCLE", "Bike share")),
    *_brands(T, ("Ground transportation", "Bus"),
             (r"GREYHOUND", "Greyhound"), (r"FLIXBUS", "FlixBus"), (r"MEGABUS", "Megabus")),
    *_brands(T, ("Ground transportation", "Rideshare"), (r"\bWAYMO\b", "Waymo")),
    # --- health, fitness, pets ---
    *_brands(HP, ("Pharmacy", "Pharmacy"),
             (r"GOODRX", "GoodRx"), (r"DUANE READE", "Duane Reade", "Walgreens"), (r"\bGNC\b", "GNC"),
             (r"VITAMIN SHOPPE", "The Vitamin Shoppe")),
    *_brands(HP, ("Medical", "Medical"),
             (r"TELADOC|\bAMWELL\b|\bMDLIVE\b", "Telehealth"), (r"1 ?800 ?CONTACTS", "1-800 Contacts"),
             (r"AMERICA'?S BEST CONTACTS", "America's Best"), (r"VISIONWORKS", "Visionworks")),
    *_brands(HP, ("Fitness", "Fitness"),
             (r"24 ?HOUR ?FITNESS", "24 Hour Fitness"), (r"\bLA ?FITNESS|FITNESS INTERNATIONAL", "LA Fitness"),
             (r"CRUNCH FITNESS", "Crunch Fitness"), (r"GOLD'?S GYM", "Gold's Gym"), (r"SOULCYCLE", "SoulCycle"),
             (r"BARRY'?S", "Barry's"), (r"CLASS ?PASS", "ClassPass"), (r"PURE BARRE", "Pure Barre"),
             (r"CLUB PILATES", "Club Pilates"), (r"SNAP FITNESS", "Snap Fitness"), (r"EOS FITNESS", "EoS Fitness"),
             (r"BLINK FITNESS", "Blink Fitness")),
    *_brands(PETS, ("Pet care", "Pet care"),
             (r"BARK ?BOX", "BarkBox"), (r"VCA (ANIMAL|HOSP)", "VCA Animal Hospitals"), (r"BLUEPEARL", "BluePearl"),
             (r"TRUPANION|HEALTHY PAWS|NATIONWIDE PET", "Pet insurance")),
    # --- insurance, auto, entertainment, other ---
    *_brands(FIN, ("Insurance", "Insurance"),
             (r"LEMONADE (INS|INC)|LEMONADE COM", "Lemonade"), (r"ROOT INSURANCE", "Root Insurance"),
             (r"METLIFE", "MetLife"), (r"PRUDENTIAL", "Prudential"), (r"\bAFLAC\b", "Aflac"), (r"ERIE INS", "Erie Insurance"),
             (r"AMERICAN FAMILY INS|\bAMFAM\b", "American Family"), (r"HAGERTY", "Hagerty"),
             (r"\bKEMPER\b", "Kemper"), (r"THE HARTFORD|HARTFORD INS", "The Hartford"), (r"NEW YORK LIFE", "New York Life"),
             (r"NORTHWESTERN MUTUAL", "Northwestern Mutual")),
    *_brands(TR, ("Auto purchase / repair / maintenance", "Auto service"),
             (r"\bCARMAX\b", "CarMax"), (r"CARVANA", "Carvana"), (r"PEP BOYS", "Pep Boys"),
             (r"TAKE ?5 (OIL|CAR)", "Take 5 Oil Change"), (r"CHRISTIAN BROTHERS AUTO", "Christian Brothers Automotive"),
             (r"\bMAACO\b", "Maaco"), (r"GREASE MONKEY", "Grease Monkey"), (r"MONRO (MUFFLER|AUTO)", "Monro"),
             (r"MAVIS (TIRE|DISCOUNT)", "Mavis Tires"), (r"BELLE TIRE", "Belle Tire"), (r"LES SCHWAB", "Les Schwab"),
             (r"SAFELITE", "Safelite"), (r"MISTER CAR WASH", "Mister Car Wash"), (r"CARFAX", "Carfax")),
    *_brands(E, ("Recreation", "Recreation / attractions"),
             (r"CINEMARK", "Cinemark"), (r"HARKINS", "Harkins Theatres"), (r"DAVE ?& ?BUSTER", "Dave & Buster's"),
             (r"FANDANGO", "Fandango"), (r"BOWLERO|AMF BOWL", "Bowlero"), (r"CHUCK ?E ?CHEESE", "Chuck E. Cheese"),
             (r"ROUND ?1 BOWL", "Round1"), (r"MAIN EVENT", "Main Event"), (r"SKY ZONE", "Sky Zone"),
             (r"URBAN AIR", "Urban Air"), (r"PUTTSHACK", "Puttshack")),
    *_brands(E, ("Recreation", "Video games"), (r"ROBLOX", "Roblox"), (r"ELECTRONIC ARTS|\bEA \*", "Electronic Arts")),
    *_brands(SVC, ("Shipping / postal", "Shipping"), (r"PIRATE ?SHIP", "Pirate Ship"), (r"STAMPS COM", "Stamps.com")),
    *_brands(GIVE, ("Charitable giving", "Donation"),
             (r"WIKIMEDIA", "Wikimedia Foundation"), (r"ST JUDE", "St. Jude"), (r"SALVATION ARMY", "The Salvation Army"),
             (r"HABITAT FOR HUMANITY", "Habitat for Humanity"), (r"\bUNICEF\b", "UNICEF"),
             (r"DOCTORS WITHOUT BORDERS|\bMSF USA", "Doctors Without Borders")),
]

# Flow: arenas and ballparks named after a sponsor, checked before everything else so a
# ticket isn't filed under the sponsor (a retailer, airline, bank, phone or energy company).
SPONSOR_VENUES = [
    (rx, None, None, E, "Sports tickets", "Venue") for rx in (
        r"\bTARGET (CENTER|FIELD)\b", r"XCEL ENERGY CENTER", r"UNITED CENTER", r"AMERICAN AIRLINES (CENTER|ARENA)",
        r"\bDELTA CENTER\b", r"T-?MOBILE (PARK|ARENA|CENTER)", r"AT ?& ?T (STADIUM|CENTER|FIELD|PARK)",
        r"CHASE (CENTER|FIELD)", r"CAPITAL ONE (ARENA|CENTER)", r"STATE FARM (STADIUM|ARENA)", r"CRYPTO ?\.?COM ARENA",
        r"SOFI STADIUM", r"LEVI'?S STADIUM", r"\bBALL ARENA\b", r"LUMEN FIELD", r"MERCEDES[- ]?BENZ (STADIUM|SUPERDOME)",
        r"HARD ROCK STADIUM", r"BANK OF AMERICA STADIUM", r"SPECTRUM CENTER", r"\bTD GARDEN\b", r"BARCLAYS CENTER",
        r"MADISON SQUARE GARDEN", r"WRIGLEY FIELD", r"FENWAY PARK", r"CITI FIELD", r"CITIZENS BANK PARK", r"\bPNC PARK\b",
        r"BUSCH STADIUM", r"COORS FIELD", r"ALLEGIANT STADIUM", r"\bKIA (CENTER|FORUM)\b", r"FROST BANK CENTER",
        r"SMOOTHIE KING CENTER", r"FISERV FORUM", r"PAYCOM CENTER", r"FOOTPRINT CENTER", r"GOLDEN 1 CENTER",
        r"TOYOTA (CENTER|STADIUM)", r"GEHA FIELD", r"EMPOWER FIELD", r"NRG STADIUM", r"\bU ?S ?BANK STADIUM\b")
]

ALL_MERCHANT_RULES = SPONSOR_VENUES + COMMON_MERCHANT_RULES + MERCHANT_RULES
MERCHANT_RULES_C = [(re.compile(p), c, fam, l1, l2, l3) for p, c, fam, l1, l2, l3 in ALL_MERCHANT_RULES]

# Weak keyword rules applied only after issuer-category mapping fails.
WEAK_KEYWORDS = [
    (r"RESTAURANT|GRILL|BISTRO|STEAK|TAVERN|KITCHEN|TRATTORIA|OSTERIA|SUSHI|RAMEN|PIZZ|BRASSERIE|CHOPHOUSE|OYSTER|TAQUERIA|"
     r"CANTINA|\bPUB\b|\bBAR\b|EATERY|DINER|\bBBQ\b|SMOKEHOUSE|CAFE|CAFFE|TRATTORIA|RISTORANTE|BRASSERIE|CUCINA|FOOD HALL",
     F, "Dining", "Full service"),
    (r"HOTEL|RESORT|\bINN\b|SUITES|\bLODGE\b|HOSTEL|B&B|BED AND BREAKFAST", T, "Lodging", "Other hotels"),
    (r"\bMARKET\b|\bFOODS\b", F, "Groceries", "Traditional grocery"),
]
WEAK_KEYWORDS_C = [(re.compile(p), l1, l2, l3) for p, l1, l2, l3 in WEAK_KEYWORDS]

# Issuer-category substring map (lowercased). Covers Amex "Group-Subgroup",
# Chase, Capital One, Discover and Citi style labels.
ISSUER_CATEGORY_MAP = [
    # Flow: "Group-Subgroup" labels the base rules do not map ("Entertainment-Associations" is
    # gym and club dues; it must precede "entertain").
    ("association", SVC, "Memberships", "Memberships / dues"),
    ("convenience", F, "Groceries", "Convenience store"),     # also a target for amount rules (Circle K inside)
    ("sports event", E, "Sports tickets", "Venue"),
    ("printing", SVC, "Printing / publishing", "Printing / publishing"),
    ("other transportation", TR, "Other transportation", "Other transportation"),
    ("airline", T, "Airfare", "Other airlines"),
    ("lodging", T, "Lodging", "Other hotels"), ("hotel", T, "Lodging", "Other hotels"),
    ("travel agenc", T, "Booking agency / OTA", "Other OTA"),
    ("vehicle rental", T, "Rental car", "Other rental"), ("car rental", T, "Rental car", "Other rental"),
    ("taxi", T, "Ground transportation", "Taxi"), ("rail", T, "Ground transportation", "Rail"),
    ("parking", TR, "Parking / tolls", "Parking"), ("toll", TR, "Parking / tolls", "Tolls"),
    ("fuel", TR, "Fuel", "Gas station"), ("gasoline", TR, "Fuel", "Gas station"), ("gas station", TR, "Fuel", "Gas station"),
    ("auto", TR, "Auto purchase / repair / maintenance", "Auto service"), ("automotive", TR, "Auto purchase / repair / maintenance", "Auto service"),
    ("fast food", F, "Dining", "Fast casual / quick service"),
    ("bar & caf", F, "Dining", "Bar / cafe"), ("restaurant", F, "Dining", "Full service"), ("food & drink", F, "Dining", "Full service"),
    ("dining", F, "Dining", "Full service"),
    ("groceries", F, "Groceries", "Traditional grocery"), ("grocery", F, "Groceries", "Traditional grocery"), ("supermarket", F, "Groceries", "Traditional grocery"),
    ("liquor", F, "Alcohol / Wine", "Wine / liquor retailer"), ("wine", F, "Alcohol / Wine", "Other alcohol"),
    ("pharmac", HP, "Pharmacy", "Pharmacy"), ("drug", HP, "Pharmacy", "Pharmacy"),
    ("dental", HP, "Dental", "Dental"), ("medical", HP, "Medical", "Medical"), ("health", HP, "Medical", "Medical"),
    ("department", S, "Department store", "Department / off-price"), ("clothing", S, "Apparel", "Apparel / footwear"),
    ("electronic", S, "Electronics / technology", "Electronics retailer"), ("computer", S, "Electronics / technology", "Electronics retailer"),
    ("hardware", H, "Home maintenance", "Home improvement store"), ("home improvement", H, "Home maintenance", "Home improvement store"),
    ("furnish", S, "Home goods", "Home goods"), ("furniture", H, "Furniture", "Furniture"), ("home", H, "Home maintenance", "Home"),
    ("sporting", S, "Sporting goods / outdoors", "Sporting goods"), ("wholesale", S, "Warehouse club", "Other warehouse"),
    ("internet purchase", S, "General retail / online", "General online"), ("book", S, "Specialty retail", "Books"),
    ("jewel", S, "Specialty retail", "Jewelry"), ("arts", S, "Specialty retail", "Arts / crafts"), ("shopping", S, "General retail / online", "General retail"),
    ("merchandise", S, "General retail / online", "General retail"),
    ("cable", BILLS, "Internet", "Internet provider"), ("internet comm", BILLS, "Internet", "Internet provider"),
    ("communication", BILLS, "Mobile phone", "Telecom"), ("telecom", BILLS, "Mobile phone", "Telecom"), ("phone", BILLS, "Mobile phone", "Telecom"),
    ("utilit", BILLS, "Utilities", "Utilities"), ("bills", BILLS, "Utilities", "Bills"),
    ("insurance", FIN, "Insurance", "Insurance"), ("professional", FIN, "Professional services", "Professional services"),
    ("education", ED, "Tuition", "Education"), ("charit", GIVE, "Charitable giving", "Donation"), ("gifts & don", GIVE, "Charitable giving", "Donation"),
    ("government", GOV, "Government fees", "Government / taxes"),
    ("entertain", E, "Recreation", "Recreation / attractions"), ("attraction", E, "Recreation", "Recreation / attractions"),
    ("theat", E, "Concerts / events", "Theater / events"), ("recreation", E, "Recreation", "Recreation / attractions"),
    ("personal", HP, "Personal care", "Personal care"), ("travel", T, "Other travel", "Other travel"),
]

FEE_CLASS = {
    "annual fee": (FIN, "Fees", "Card annual fee"),
    "foreign transaction fee": (FIN, "Fees", "Foreign transaction fee"),
    "other fee": (FIN, "Fees", "Other card fee"),
}


def issuer_map(issuer_category):
    ic = (issuer_category or "").lower()
    if ic:
        for key, l1, l2, l3 in ISSUER_CATEGORY_MAP:
            if key in ic:
                return l1, l2, l3
    return None


def classify_merchant(desc_u, issuer_category="", fallback_name=None):
    """Return dict: canonical, family, l1, l2, l3, class_source, processor.

    Precedence: named-brand rule > issuer category > generic-keyword rule > weak keyword.
    A generic keyword (e.g. BUTCHER, CELLARS, CLINIC) loses to an issuer category
    that disagrees at L2, because names like "The Hungry Butcher" are restaurants.
    """
    clean, processor = strip_processor(desc_u)
    # Flow: separator-normalized copy, so "CIRCLE_K" and "CIRCLE-K" match the "CIRCLE ?K" rule.
    norm = re.sub(r"\s+", " ", re.sub(r"[_\-./*]+", " ", clean)).strip()
    im = issuer_map(issuer_category)

    def fallback_canonical(text):  # Flow: prefer the caller's sanitized label
        return fallback_name or _fallback_canonical(text)

    for rx, canon, fam, l1, l2, l3 in MERCHANT_RULES_C:
        m = rx.search(clean) or rx.search(desc_u) or rx.search(norm)
        if not m:
            continue
        hit = m.group(0).strip(" *.-")
        generic = canon is None and bool(GENERIC_RE.search(hit))
        if (generic and im and (im[0], im[1]) != (l1, l2) and im[1] in AUTHORITATIVE_ISSUER_L2
                and not STRONG_RE.search(hit)):
            c = fallback_canonical(clean)
            return dict(canonical=c, family=c, l1=im[0], l2=im[1], l3=im[2],
                        class_source="issuer category (over keyword)", processor=processor)
        if canon:
            c = canon
        elif generic or len(hit) < 3:
            c = fallback_canonical(clean)
        else:
            c = nice_case(hit)
        return dict(canonical=c, family=fam or c, l1=l1, l2=l2, l3=l3,
                    class_source="keyword rule" if generic else "merchant rule", processor=processor)
    if im:
        c = fallback_canonical(clean)
        return dict(canonical=c, family=c, l1=im[0], l2=im[1], l3=im[2], class_source="issuer category", processor=processor)
    for rx, l1, l2, l3 in WEAK_KEYWORDS_C:
        if rx.search(clean) or rx.search(norm):
            c = fallback_canonical(clean)
            return dict(canonical=c, family=c, l1=l1, l2=l2, l3=l3, class_source="keyword", processor=processor)
    c = fallback_canonical(clean)
    return dict(canonical=c, family=c, l1="Uncategorized", l2="Needs review", l3="Needs review",
                class_source="unclassified", processor=processor)


# ---------------------------------------------------------------------------
# Reward keys: the earning bucket a purchase most likely falls into (your own
# subcategories carry theirs). REWARD_PARENT gives each key's broader bucket, ending
# at 'default'.
# ---------------------------------------------------------------------------
REWARD_PARENT = {
    "airfare_other": "airfare", "airfare": "travel",
    "hotel_other": "hotel", "vacation_rental": "hotel_other", "hotel": "travel",
    "travel_ota": "travel", "car_rental": "travel", "transit": "travel", "parking_tolls": "transit",
    "cruise": "travel", "travel_other": "travel", "travel": "default",
    "dining_delivery": "dining", "dining": "default",
    "grocery_delivery": "grocery", "grocery": "default",
    "ev_charging": "gas", "gas": "default",
    "streaming": "default", "phone_plan": "default", "internet_cable": "default", "alcohol": "default",
    "warehouse_club": "default", "amazon": "default", "drugstore": "default", "home_improvement": "default",
    "utilities": "default", "general": "default",
}


def reward_key(l1, l2, l3, family):
    import card_taxonomy                        # your subcategories carry their own rewards bucket
    custom = card_taxonomy.custom_subcategory(l1, l2)
    if custom is not None:
        return custom.rewards
    l1, l2, l3 = card_taxonomy.base(l1, l2, l3)    # renamed built-ins keep their bucket
    if l1 == "Travel":
        if l2 == "Airfare":
            return "airfare_other"
        if l2 == "Lodging":
            return "vacation_rental" if l3 == "Vacation rental" else "hotel_other"
        return {"Booking agency / OTA": "travel_ota", "Rental car": "car_rental", "Ground transportation": "transit",
                "Parking / tolls": "parking_tolls", "Cruise": "cruise"}.get(l2, "travel_other")
    if l1 == "Transportation":
        if l2 == "Fuel":
            return "ev_charging" if l3 == "EV charging" else "gas"
        if l2 == "Parking / tolls":
            return "parking_tolls"
        return "general"
    if l1 == "Food & Beverage":
        if l2 == "Dining":
            return "dining_delivery" if l3 == "Delivery" else "dining"
        if l2 == "Groceries":
            return "grocery_delivery" if l3 == "Grocery delivery" else "grocery"
        if l2 == "Alcohol / Wine":
            return "alcohol"
    if l1 == "Bills & Utilities":
        return {"Utilities": "utilities", "Mobile phone": "phone_plan", "Internet": "internet_cable", "TV": "internet_cable"}.get(l2, "general")
    if l1 == "Digital & Media":
        return "streaming" if l2 == "Streaming" else "general"
    if l1 == "Shopping":
        if l2 == "Memberships":
            return "general"
        if l2 == "Warehouse club":
            return "warehouse_club"
        if family == "Amazon":
            return "amazon"
    if l1 == "Health & Personal" and l2 == "Pharmacy":
        return "drugstore"
    if l1 == "Home":
        if l2 == "Home maintenance" and l3 == "Home improvement store":
            return "home_improvement"
    return "general"
