import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import calendar
import re


# ============================================================
# PATHS
# ============================================================

BASE = Path(__file__).resolve().parent.parent
DATASET = BASE / "dataset"


# ============================================================
# LOAD DATA
# ============================================================

def load_data():
    requests = pd.read_csv(DATASET / "requests.csv")
    profiles = pd.read_csv(DATASET / "financial_profiles.csv")
    events = pd.read_csv(DATASET / "financial_events.csv")
    # Normalize once: the dataset is large and every request reuses the same events.
    events = normalize_financial_events(events)
    payment_options = pd.read_csv(DATASET / "request_payment_options.csv")
    messages = pd.read_csv(DATASET / "messages.csv")
    images = pd.read_csv(DATASET / "images.csv")
    exchange_rates = pd.read_csv(DATASET / "exchange_rates.csv")

    return (
        requests,
        profiles,
        events,
        payment_options,
        messages,
        images,
        exchange_rates,
    )


# ============================================================
# BASIC HELPERS
# ============================================================

def to_float(value, default=0.0):
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def parse_date(value):
    if pd.isna(value) or value == "":
        return None

    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


def date_string(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")

    return str(value)


# ============================================================
# CURRENCY CONVERSION
# ============================================================

def convert_to_home_currency(
    amount,
    from_currency,
    home_currency,
    rate_date,
    exchange_rates,
):
    amount = to_float(amount)

    if not from_currency or not home_currency:
        return amount

    if from_currency == home_currency:
        return amount

    target_date = parse_date(rate_date)

    if target_date is None:
        return amount

    rates = exchange_rates.copy()
    rates["rate_date_parsed"] = pd.to_datetime(
        rates["rate_date"], errors="coerce"
    ).dt.date

    # Direct rate
    direct = rates[
        (rates["rate_date_parsed"] == target_date)
        & (rates["from_currency"] == from_currency)
        & (rates["to_currency"] == home_currency)
    ]

    if not direct.empty:
        return amount * to_float(direct.iloc[0]["rate"], 1.0)

    # Reverse rate
    reverse = rates[
        (rates["rate_date_parsed"] == target_date)
        & (rates["from_currency"] == home_currency)
        & (rates["to_currency"] == from_currency)
    ]

    if not reverse.empty:
        rate = to_float(reverse.iloc[0]["rate"], 1.0)

        if rate != 0:
            return amount / rate

    return amount


# ============================================================
# EVENT STATUS PRIORITY
# ============================================================

STATUS_PRIORITY = {
    "failed": 100,
    "cancelled": 100,
    "canceled": 100,
    "settled": 80,
    "scheduled": 70,
    "pending": 60,
    "forecast": 50,
    "": 0,
}


def event_status(value):
    if pd.isna(value):
        return ""

    return str(value).strip().lower()


# ============================================================
# NORMALIZE FINANCIAL EVENTS
# ============================================================

def normalize_financial_events(events):
    df = events.copy()

    if df.empty:
        return df

    for col in [
        "event_id",
        "linked_event_id",
        "event_type",
        "description",
        "category",
        "direction",
        "status",
        "flexibility",
    ]:
        if col not in df.columns:
            df[col] = ""

    df["status_normalized"] = df["status"].apply(event_status)

    df["event_date_parsed"] = df["event_date"].apply(parse_date)
    df["settlement_date_parsed"] = df["settlement_date"].apply(parse_date)

    df["status_priority"] = df["status_normalized"].map(
        STATUS_PRIORITY
    ).fillna(0)

    # Remove failed/cancelled events
    df = df[
        ~df["status_normalized"].isin(
            ["failed", "cancelled", "canceled"]
        )
    ].copy()

    if df.empty:
        return df

    # Build duplicate groups
    def duplicate_key(row):
        event_id = str(row.get("event_id", "")).strip()

        if event_id:
            return ("event_id", event_id)

        linked_id = str(row.get("linked_event_id", "")).strip()

        if linked_id:
            return ("linked_event_id", linked_id)

        return (
            "fingerprint",
            str(row.get("user_id", "")),
            str(row.get("event_type", "")),
            str(row.get("description", "")),
            str(row.get("category", "")),
            str(row.get("direction", "")),
            str(row.get("amount", "")),
            str(row.get("currency", "")),
            str(row.get("event_date", "")),
        )

    df["duplicate_group"] = df.apply(duplicate_key, axis=1)

    # Best record wins
    df = df.sort_values(
        by=[
            "duplicate_group",
            "status_priority",
            "settlement_date_parsed",
            "event_date_parsed",
        ],
        ascending=[True, False, False, False],
    )

    df = df.drop_duplicates(
        subset=["duplicate_group"],
        keep="first",
    )

    return df.reset_index(drop=True)


# ============================================================
# ACTIVE EVENTS
# ============================================================

def get_active_events(events, user_id=None):
    normalized = (
        events.copy()
        if "status_normalized" in events.columns
        else normalize_financial_events(events)
    )

    if normalized.empty:
        return normalized

    if user_id is not None:
        normalized = normalized[
            normalized["user_id"].astype(str)
            == str(user_id)
        ]

    return normalized.copy()


# ============================================================
# FUTURE CONFIRMED EVENTS
# ============================================================

def get_future_confirmed_events(
    events,
    user_id,
    start_date,
    end_date,
):
    normalized = normalize_financial_events(events)

    if normalized.empty:
        return normalized

    normalized = normalized[
        normalized["user_id"].astype(str)
        == str(user_id)
    ]

    normalized = normalized[
        normalized["status_normalized"].isin(
            ["scheduled", "pending"]
        )
    ]

    if normalized.empty:
        return normalized

    normalized["effective_date"] = normalized[
        "settlement_date_parsed"
    ]

    normalized["effective_date"] = normalized[
        "effective_date"
    ].fillna(normalized["event_date_parsed"])

    normalized = normalized[
        (normalized["effective_date"] >= start_date)
        & (normalized["effective_date"] <= end_date)
    ]

    return normalized.copy()


# ============================================================
# CALENDAR HELPERS
# ============================================================

def add_months(date_value, months):
    month = date_value.month - 1 + months

    year = date_value.year + month // 12
    month = month % 12 + 1

    day = min(
        date_value.day,
        calendar.monthrange(year, month)[1],
    )

    return date_value.replace(
        year=year,
        month=month,
        day=day,
    )


# ============================================================
# RECURRENCE DETECTION
# ============================================================

def determine_recurrence(dates):
    dates = sorted(
        d for d in dates if d is not None
    )

    if len(dates) < 2:
        return None

    intervals = []

    for i in range(1, len(dates)):
        diff = (dates[i] - dates[i - 1]).days

        if diff > 0:
            intervals.append(diff)

    if not intervals:
        return None

    intervals_sorted = sorted(intervals)

    median_interval = intervals_sorted[
        len(intervals_sorted) // 2
    ]

    if 27 <= median_interval <= 33:
        return "monthly"

    if 6 <= median_interval <= 8:
        return "weekly"

    if 13 <= median_interval <= 15:
        return "biweekly"

    if 80 <= median_interval <= 100:
        return "quarterly"

    return ("interval", median_interval)


def next_recurrence_date(last_date, recurrence):
    if recurrence is None:
        return None

    if recurrence == "monthly":
        return add_months(last_date, 1)

    if recurrence == "weekly":
        return last_date + timedelta(days=7)

    if recurrence == "biweekly":
        return last_date + timedelta(days=14)

    if recurrence == "quarterly":
        return add_months(last_date, 3)

    if isinstance(recurrence, tuple):
        return last_date + timedelta(
            days=recurrence[1]
        )

    return None


# ============================================================
# GENERATE RECURRING FORECAST EVENTS
# ============================================================

def generate_recurring_events(
    user_events,
    start_date,
    end_date,
):
    if user_events.empty:
        return []

    generated = []

    history = user_events.copy()
    history["_history_date"] = history["event_date"].apply(parse_date)
    history = history[history["_history_date"].notna()]
    history = history[history["_history_date"] < start_date]
    # Recurrence should be inferred from historical settled/realized records,
    # not from future scheduled/pending records (those are forecast separately).
    if "status_normalized" in history.columns:
        history = history[history["status_normalized"].isin(["settled"])]

    groups = history.groupby(
        [
            "event_type",
            "category",
            "description",
            "direction",
        ],
        dropna=False,
    )

    for _, group in groups:

        group = group.copy()

        group["parsed_event_date"] = group[
            "event_date"
        ].apply(parse_date)

        group = group[
            group["parsed_event_date"].notna()
        ]

        if len(group) < 2:
            continue

        group = group.sort_values(
            "parsed_event_date"
        )

        dates = group[
            "parsed_event_date"
        ].tolist()

        recurrence = determine_recurrence(dates)

        if recurrence is None:
            continue

        last_row = group.iloc[-1]

        last_date = last_row[
            "parsed_event_date"
        ]

        amount = to_float(
            last_row.get("amount", 0)
        )

        if amount == 0:
            continue

        currency = str(
            last_row.get("currency", "")
        )

        direction = str(
            last_row.get("direction", "")
        ).lower()

        category = str(
            last_row.get("category", "")
        )

        flexibility = str(
            last_row.get("flexibility", "")
        ).lower()

        event_type = str(
            last_row.get("event_type", "")
        )

        description = str(
            last_row.get("description", "")
        )

        next_date = next_recurrence_date(
            last_date,
            recurrence,
        )

        safety_counter = 0

        while (
            next_date is not None
            and next_date <= end_date
            and safety_counter < 200
        ):

            if next_date >= start_date:

                generated.append(
                    {
                        "event_id": (
                            f"forecast_"
                            f"{event_type}_"
                            f"{category}_"
                            f"{next_date}"
                        ),
                        "user_id": last_row["user_id"],
                        "event_type": event_type,
                        "description": description,
                        "category": category,
                        "direction": direction,
                        "amount": amount,
                        "currency": currency,
                        "event_date": next_date,
                        "settlement_date": next_date,
                        "status": "forecast",
                        "linked_event_id": "",
                        "flexibility": flexibility,
                        "minimum_allowed_amount": (
                            last_row.get(
                                "minimum_allowed_amount",
                                "",
                            )
                        ),
                    }
                )

            next_date = next_recurrence_date(
                next_date,
                recurrence,
            )

            safety_counter += 1

    return generated


# ============================================================
# BALANCE FORECAST
# ============================================================

def forecast_balance(
    starting_balance,
    minimum_balance,
    events,
    recurring_events,
    home_currency,
    exchange_rates,
    start_date,
    end_date,
):
    daily_cashflow = defaultdict(float)

    all_events = []

    if events is not None and not events.empty:
        all_events.extend(
            events.to_dict("records")
        )

    all_events.extend(recurring_events)

    for event in all_events:

        event_date = event.get(
            "settlement_date"
        )

        if event_date is None:
            event_date = event.get(
                "event_date"
            )

        event_date = parse_date(event_date)

        if event_date is None:
            continue

        if event_date < start_date:
            continue

        if event_date > end_date:
            continue

        amount = to_float(
            event.get("amount", 0)
        )

        if amount == 0:
            continue

        currency = event.get(
            "currency",
            home_currency,
        )

        home_amount = convert_to_home_currency(
            amount,
            currency,
            home_currency,
            event_date,
            exchange_rates,
        )

        direction = str(
            event.get("direction", "")
        ).strip().lower()

        if direction in [
            "in",
            "income",
            "credit",
            "inflow",
        ]:
            daily_cashflow[event_date] += home_amount
        else:
            daily_cashflow[event_date] -= home_amount

    balance = starting_balance

    lowest_balance = balance
    lowest_date = start_date

    current_date = start_date

    while current_date <= end_date:

        balance += daily_cashflow.get(
            current_date,
            0.0,
        )

        if balance < lowest_balance:
            lowest_balance = balance
            lowest_date = current_date

        current_date += timedelta(days=1)

    safe = lowest_balance >= minimum_balance

    return {
        "starting_balance": starting_balance,
        "minimum_balance": minimum_balance,
        "lowest_balance": lowest_balance,
        "lowest_date": lowest_date,
        "safe": safe,
        "daily_cashflow": daily_cashflow,
    }


# ============================================================
# AMOUNT SAFE TO PAY TODAY
# ============================================================

def calculate_amount_safe_to_pay(
    requested_amount,
    starting_balance,
    minimum_balance,
    events,
    recurring_events,
    home_currency,
    exchange_rates,
    request_date,
):
    forecast_end = request_date + timedelta(days=90)

    forecast = forecast_balance(
        starting_balance=starting_balance,
        minimum_balance=minimum_balance,
        events=events,
        recurring_events=recurring_events,
        home_currency=home_currency,
        exchange_rates=exchange_rates,
        start_date=request_date,
        end_date=forecast_end,
    )

    available_capacity = max(
        0.0,
        forecast["lowest_balance"]
        - minimum_balance,
    )

    safe_amount = min(
        requested_amount,
        available_capacity,
    )

    return round(
        max(0.0, safe_amount),
        2,
    )


# ============================================================
# EARLIEST FULL PAYMENT DATE
# ============================================================

def find_earliest_full_payment_date(
    requested_amount,
    starting_balance,
    minimum_balance,
    events,
    recurring_events,
    home_currency,
    exchange_rates,
    request_date,
):
    forecast_end = request_date + timedelta(
        days=90
    )

    daily_cashflow = defaultdict(float)

    all_events = []

    if events is not None and not events.empty:
        all_events.extend(
            events.to_dict("records")
        )

    all_events.extend(recurring_events)

    for event in all_events:

        event_date = event.get(
            "settlement_date"
        )

        if event_date is None:
            event_date = event.get(
                "event_date"
            )

        event_date = parse_date(event_date)

        if event_date is None:
            continue

        if event_date < request_date:
            continue

        if event_date > forecast_end:
            continue

        amount = to_float(
            event.get("amount", 0)
        )

        if amount == 0:
            continue

        currency = event.get(
            "currency",
            home_currency,
        )

        home_amount = convert_to_home_currency(
            amount,
            currency,
            home_currency,
            event_date,
            exchange_rates,
        )

        direction = str(
            event.get("direction", "")
        ).strip().lower()

        if direction in [
            "in",
            "income",
            "credit",
            "inflow",
        ]:
            daily_cashflow[event_date] += home_amount
        else:
            daily_cashflow[event_date] -= home_amount

    # Build cumulative balance before request payment
    balances = {}

    balance = starting_balance
    current_date = request_date

    while current_date <= forecast_end:

        balance += daily_cashflow.get(
            current_date,
            0.0,
        )

        balances[current_date] = balance

        current_date += timedelta(days=1)

    candidate = request_date

    while candidate <= forecast_end:

        safe = True

        current_date = request_date

        while current_date <= forecast_end:

            balance_after_request = balances[
                current_date
            ]

            if current_date >= candidate:
                balance_after_request -= requested_amount

            if balance_after_request < minimum_balance:
                safe = False
                break

            current_date += timedelta(days=1)

        if safe:
            return candidate

        candidate += timedelta(days=1)

    return None


# ============================================================
# AFFORDABILITY STATUS
# ============================================================

def determine_affordability_status(
    requested_amount,
    amount_safe_today,
    request_date,
    earliest_full_payment_date,
    desired_completion_date,
):
    requested_amount = to_float(
        requested_amount
    )

    amount_safe_today = to_float(
        amount_safe_today
    )

    if requested_amount <= 0:
        return "not_affordable"

    if amount_safe_today >= requested_amount:
        return "affordable_now"

    if earliest_full_payment_date is None:
        return "not_affordable"

    if earliest_full_payment_date <= request_date:
        return "affordable_now"

    if (
        desired_completion_date is not None
        and earliest_full_payment_date
        <= desired_completion_date
    ):
        return "affordable_with_plan"

    return "affordable_later"


# ============================================================
# STEP 27
# RECOMMENDED PAYMENT METHOD
# ============================================================

def get_user_payment_methods(profile):
    value = profile.get(
        "payment_methods_user_will_consider",
        "",
    )

    if pd.isna(value):
        return []

    text = str(value).strip()

    if not text:
        return []

    # Support common separators
    for separator in ["|", ";", ","]:
        if separator in text:
            methods = text.split(separator)
            return [
                str(x).strip().lower()
                for x in methods
                if str(x).strip()
            ]

    return [text.lower()]


def normalize_payment_method(value):
    if pd.isna(value):
        return ""

    return str(value).strip().lower()


def get_payment_options_for_request(
    payment_options,
    request_id,
):
    if payment_options is None:
        return pd.DataFrame()

    options = payment_options[
        payment_options["request_id"].astype(str)
        == str(request_id)
    ].copy()

    if options.empty:
        return options

    options["normalized_method"] = options[
        "payment_method"
    ].apply(normalize_payment_method)

    options["payment_amount_num"] = options[
        "payment_amount"
    ].apply(to_float)

    options["number_of_payments_num"] = options[
        "number_of_payments"
    ].apply(lambda x: int(to_float(x, 1)))

    options["financing_fee_num"] = options[
        "financing_fee"
    ].apply(to_float)

    options["total_payable_num"] = options[
        "total_payable_amount"
    ].apply(to_float)

    options["first_payment_date_parsed"] = options[
        "first_payment_date"
    ].apply(parse_date)

    return options


def recommended_payment_method(
    request,
    profile,
    payment_options,
    amount_safe_today,
    affordability_status,
    earliest_full_payment_date,
):
    """
    Step 27.

    Determines the recommended payment method using:
    - user-approved payment methods
    - affordability
    - supplied payment options
    - request partial-payment preference
    """

    requested_amount = to_float(
        request["requested_amount"]
    )

    allows_partial = str(
        request.get(
            "allows_partial_payment",
            ""
        )
    ).strip().lower()

    partial_allowed = allows_partial in [
        "true",
        "1",
        "yes",
        "y",
    ]

    user_methods = get_user_payment_methods(
        profile
    )

    request_id = request["request_id"]

    options = get_payment_options_for_request(
        payment_options,
        request_id,
    )

    # --------------------------------------------------------
    # 1. Full payment today
    # --------------------------------------------------------

    if (
        affordability_status == "affordable_now"
        and amount_safe_today >= requested_amount
    ):
        if (
            "full_payment" in user_methods
            or len(user_methods) == 0
        ):
            return "full_payment"

    # --------------------------------------------------------
    # 2. Partial payment
    # --------------------------------------------------------

    if (
        partial_allowed
        and amount_safe_today > 0
        and amount_safe_today < requested_amount
        and "partial_payment" in user_methods
    ):
        return "partial_payment"

    # --------------------------------------------------------
    # 3. Installments
    # --------------------------------------------------------

    # A conversational purchase may not have a matching row in the
    # challenge payment-options table. In that case `options` can be
    # an empty DataFrame with no normalized helper column. Do not index
    # the helper column until we know it exists.
    if "installments" in user_methods and not options.empty and "normalized_method" in options.columns:
        installment_options = options[
            options["normalized_method"]
            == "installments"
        ].copy()

        if not installment_options.empty:

            # Keep only options whose first payment
            # is not before the request date.
            request_date = parse_date(
                request["request_date"]
            )

            if request_date is not None:
                installment_options = (
                    installment_options[
                        installment_options[
                            "first_payment_date_parsed"
                        ].isna()
                        |
                        (
                            installment_options[
                                "first_payment_date_parsed"
                            ] >= request_date
                        )
                    ]
                )

            if not installment_options.empty:

                # Prefer:
                # 1. lowest financing fee
                # 2. lowest total payable
                # 3. fewer payments
                # 4. payment option ID
                installment_options = (
                    installment_options.sort_values(
                        by=[
                            "financing_fee_num",
                            "total_payable_num",
                            "number_of_payments_num",
                            "payment_option_id",
                        ],
                        ascending=[
                            True,
                            True,
                            True,
                            True,
                        ],
                    )
                )

                return "installments"

    # --------------------------------------------------------
    # 4. Wait
    # --------------------------------------------------------

    if (
        affordability_status
        in [
            "affordable_later",
            "affordable_with_plan",
        ]
        and earliest_full_payment_date is not None
        and earliest_full_payment_date
        > parse_date(request["request_date"])
        and "full_payment" in user_methods
    ):
        return "wait"

    # --------------------------------------------------------
    # 5. Not recommended
    # --------------------------------------------------------

    return "not_recommended"



# ============================================================
# STEP 28
# PAYMENT PLAN
# ============================================================

def build_payment_plan(
    request,
    profile,
    payment_options,
    recommended_method,
    amount_safe_today,
    earliest_full_payment_date,
):
    requested_amount = to_float(request["requested_amount"])
    request_date = parse_date(request["request_date"])

    if requested_amount <= 0:
        return "none"

    if recommended_method == "full_payment":
        return (
            f"{request_date.strftime('%Y-%m-%d')}:"
            f"{requested_amount:.2f}"
        )

    if recommended_method == "wait":
        if earliest_full_payment_date is None:
            return "none"

        return (
            f"{earliest_full_payment_date.strftime('%Y-%m-%d')}:"
            f"{requested_amount:.2f}"
        )

    if recommended_method == "partial_payment":
        safe_today = round(
            min(
                max(0.0, amount_safe_today),
                requested_amount,
            ),
            2,
        )
        remainder = round(
            requested_amount - safe_today,
            2,
        )

        if (
            safe_today <= 0
            or remainder <= 0
            or earliest_full_payment_date is None
        ):
            return "none"

        return (
            f"{request_date.strftime('%Y-%m-%d')}:"
            f"{safe_today:.2f}|"
            f"{earliest_full_payment_date.strftime('%Y-%m-%d')}:"
            f"{remainder:.2f}"
        )

    if recommended_method == "installments":
        options = get_payment_options_for_request(
            payment_options,
            request["request_id"],
        )

        if options.empty:
            return "none"

        options = options[
            options["normalized_method"] == "installments"
        ].copy()

        if options.empty:
            return "none"

        options = options.sort_values(
            by=[
                "financing_fee_num",
                "total_payable_num",
                "number_of_payments_num",
                "payment_option_id",
            ],
            ascending=[True, True, True, True],
        )

        option = options.iloc[0]

        number_of_payments = int(
            option["number_of_payments_num"]
        )
        payment_amount = round(
            to_float(option["payment_amount_num"]),
            2,
        )
        first_date = option["first_payment_date_parsed"]
        frequency_days = int(
            to_float(
                option.get("payment_frequency_days", 0),
                0,
            )
        )

        if (
            number_of_payments <= 0
            or payment_amount <= 0
            or first_date is None
        ):
            return "none"

        parts = []

        for i in range(number_of_payments):
            payment_date = (
                first_date
                + timedelta(days=i * frequency_days)
            )

            parts.append(
                f"{payment_date.strftime('%Y-%m-%d')}:"
                f"{payment_amount:.2f}"
            )

        return "|".join(parts)

    return "none"


# ============================================================
# STEP 29
# SPENDING CHANGES NEEDED
# ============================================================

def parse_profile_categories(value):
    if pd.isna(value):
        return set()

    raw = str(value).strip().lower()

    if not raw:
        return set()

    # Profiles may use common separators.
    parts = re.split(r"[|,;]", raw)

    return {
        p.strip()
        for p in parts
        if p.strip()
    }


def direction_is_expense(value):
    return str(value).strip().lower() in {
        "out", "expense", "debit", "outflow", "payment"
    }


def category_allowed(
    category,
    profile_field,
):
    category = str(category).strip().lower()
    allowed = parse_profile_categories(profile_field)

    return category in allowed


def find_changeable_recurring_expenses(
    user_events,
    profile,
):
    candidates = []

    if user_events is None or user_events.empty:
        return candidates

    # Group by recurring identity.
    groups = user_events.groupby(
        [
            "event_type",
            "category",
            "description",
            "direction",
        ],
        dropna=False,
    )

    for _, group in groups:
        if len(group) < 2:
            continue

        group = group.copy()
        group["_date"] = group["event_date"].apply(parse_date)
        group = group[group["_date"].notna()]

        if len(group) < 2:
            continue

        # Only expenses can be reduced/stopped.
        last = group.sort_values("_date").iloc[-1]

        if not direction_is_expense(last["direction"]):
            continue

        flexibility = str(
            last.get("flexibility", "")
        ).strip().lower()

        category = str(
            last.get("category", "")
        ).strip().lower()

        amount = to_float(last.get("amount", 0))

        if amount <= 0:
            continue

        can_stop = (
            flexibility in {
                "stoppable",
                "reducible_or_stoppable",
            }
            and category_allowed(
                category,
                profile.get(
                    "expense_categories_user_is_willing_to_stop",
                    "",
                ),
            )
        )

        can_reduce = (
            flexibility in {
                "reducible",
                "reducible_or_stoppable",
            }
            and category_allowed(
                category,
                profile.get(
                    "expense_categories_user_is_willing_to_reduce",
                    "",
                ),
            )
        )

        if not can_stop and not can_reduce:
            continue

        minimum_amount = to_float(
            last.get(
                "minimum_allowed_amount",
                0,
            )
        )

        if can_stop:
            # Stop and reduce are mutually exclusive in the output.
            # Prefer stop when the user explicitly allows stopping this category.
            candidates.append({
                "event_id": str(last["event_id"]),
                "category": category,
                "description": str(last["description"]),
                "amount": amount,
                "new_amount": 0.0,
                "command": (
                    f"stop:{last['event_id']}"
                ),
                "savings": amount,
            })
        elif can_reduce:
            # Respect the event's minimum allowed amount.
            if minimum_amount > 0:
                new_amount = min(
                    amount,
                    minimum_amount,
                )
            else:
                # When no explicit minimum is supplied,
                # use a conservative 50% reduction.
                new_amount = round(
                    amount * 0.50,
                    2,
                )

            if new_amount < amount:
                candidates.append({
                    "event_id": str(last["event_id"]),
                    "category": category,
                    "description": str(last["description"]),
                    "amount": amount,
                    "new_amount": new_amount,
                    "command": (
                        f"reduce_to:{last['event_id']}:"
                        f"{new_amount:.2f}"
                    ),
                    "savings": amount - new_amount,
                })

    # Largest useful savings first.
    candidates.sort(
        key=lambda x: x["savings"],
        reverse=True,
    )

    return candidates


def calculate_spending_changes_needed(
    requested_amount,
    amount_safe_today,
    user_events,
    profile,
):
    """
    Returns at most three optional recurring expense changes.

    Important:
    - only recurring expenses are considered;
    - only categories the user explicitly allows are considered;
    - stop/reduce are represented by the required output syntax;
    - no changes are returned when the request is already affordable.
    """

    if (
        amount_safe_today >= requested_amount
        or requested_amount <= 0
    ):
        return []

    candidates = find_changeable_recurring_expenses(
        user_events,
        profile,
    )

    if not candidates:
        return []

    selected = []
    used_event_ids = set()

    # Select at most one action per recurring expense.
    for candidate in candidates:
        event_id = candidate["event_id"]

        if event_id in used_event_ids:
            continue

        selected.append(candidate["command"])
        used_event_ids.add(event_id)

        if len(selected) >= 3:
            break

    return selected

# ============================================================
# STEP 30
# DECISION EXPLANATION
# ============================================================

def build_decision_explanation(request, profile, forecast, amount_safe_today, affordability_status_value, recommended_method, payment_plan, earliest_full_payment_date, spending_changes_output):
    requested_amount = to_float(request["requested_amount"])
    minimum_balance = to_float(profile["minimum_balance_to_keep"])
    lowest_balance = to_float(forecast.get("lowest_balance", 0))
    lowest_date = forecast.get("lowest_date")

    parts = [
        f"The requested amount is {requested_amount:.2f}.",
        f"The maximum amount that can safely be paid today is {to_float(amount_safe_today):.2f}.",
        f"Over the 90-day forecast, the lowest projected balance is {lowest_balance:.2f}"
        + (f" on {lowest_date.strftime('%Y-%m-%d')}" if hasattr(lowest_date, "strftime") else "")
        + f", while the required minimum balance is {minimum_balance:.2f}.",
    ]

    if affordability_status_value == "affordable_now":
        parts.append("The full requested amount is safe to pay on the request date without breaching the minimum balance.")
    elif affordability_status_value == "affordable_with_plan":
        if earliest_full_payment_date is not None:
            parts.append(f"The full amount becomes safe by {earliest_full_payment_date:%Y-%m-%d}, within the desired completion period.")
        else:
            parts.append("The request requires a payment plan to remain within the financial safety constraint.")
    elif affordability_status_value == "affordable_later":
        if earliest_full_payment_date is not None:
            parts.append(f"The full amount is not safe on the request date but is projected to become safe on {earliest_full_payment_date:%Y-%m-%d}.")
        else:
            parts.append("The full amount is not projected to become safe within the available forecast.")
    else:
        parts.append("The full requested amount cannot be safely funded under the current 90-day forecast.")

    method_text = {
        "full_payment": "pay in full",
        "partial_payment": "use a partial payment",
        "installments": "use installments",
        "wait": "wait until the full amount becomes safe",
        "not_recommended": "avoid recommending a payment",
    }.get(recommended_method, recommended_method)
    parts.append(f"The recommended approach is to {method_text}.")

    if payment_plan and payment_plan != "none":
        parts.append(f"The payment plan is {payment_plan}.")
    if spending_changes_output and spending_changes_output != "none":
        parts.append(f"Optional spending changes are: {spending_changes_output}.")
    else:
        parts.append("No optional spending changes are required.")

    desired = parse_date(request.get("desired_completion_date"))
    if desired is not None:
        parts.append(f"The desired completion date is {desired:%Y-%m-%d}.")

    return " ".join(parts)


# ============================================================
# MAIN
# ============================================================

def process_request(request, profiles, financial_events, payment_options, exchange_rates):
    request_id = str(request["request_id"])
    user_id = str(request["user_id"])

    request_date = parse_date(request["request_date"])
    desired_completion_date = parse_date(request["desired_completion_date"])
    requested_amount = to_float(request["requested_amount"])

    profile_rows = profiles[profiles["user_id"].astype(str) == user_id]
    if profile_rows.empty:
        raise ValueError(f"No profile found for user {user_id}")
    profile = profile_rows.iloc[0]

    starting_balance = to_float(profile["current_available_balance"])
    minimum_balance = to_float(profile["minimum_balance_to_keep"])
    home_currency = str(profile["home_currency"]).strip()

    user_events = get_active_events(financial_events, user_id)

    forecast_end = request_date + timedelta(days=90)
    recurring_events = generate_recurring_events(user_events, request_date, forecast_end)

    forecast = forecast_balance(
        starting_balance=starting_balance,
        minimum_balance=minimum_balance,
        events=user_events,
        recurring_events=recurring_events,
        home_currency=home_currency,
        exchange_rates=exchange_rates,
        start_date=request_date,
        end_date=forecast_end,
    )

    amount_safe_today = calculate_amount_safe_to_pay(
        requested_amount=requested_amount,
        starting_balance=starting_balance,
        minimum_balance=minimum_balance,
        events=user_events,
        recurring_events=recurring_events,
        home_currency=home_currency,
        exchange_rates=exchange_rates,
        request_date=request_date,
    )

    earliest_full_payment_date = find_earliest_full_payment_date(
        requested_amount=requested_amount,
        starting_balance=starting_balance,
        minimum_balance=minimum_balance,
        events=user_events,
        recurring_events=recurring_events,
        home_currency=home_currency,
        exchange_rates=exchange_rates,
        request_date=request_date,
    )

    affordability_status = determine_affordability_status(
        requested_amount=requested_amount,
        amount_safe_today=amount_safe_today,
        request_date=request_date,
        earliest_full_payment_date=earliest_full_payment_date,
        desired_completion_date=desired_completion_date,
    )

    request_options = get_payment_options_for_request(payment_options, request_id)
    recommended_method = recommended_payment_method(
        request=request,
        profile=profile,
        payment_options=request_options if not request_options.empty else payment_options,
        amount_safe_today=amount_safe_today,
        affordability_status=affordability_status,
        earliest_full_payment_date=earliest_full_payment_date,
    )

    # If the request cannot be fully completed within its desired deadline,
    # do not invent a payment plan. The challenge requires not_recommended
    # when no valid method exists.
    if recommended_method in ("full_payment", "partial_payment", "installments", "wait"):
        payment_plan = build_payment_plan(
            request=request,
            profile=profile,
            payment_options=payment_options,
            recommended_method=recommended_method,
            amount_safe_today=amount_safe_today,
            earliest_full_payment_date=earliest_full_payment_date,
        )
    else:
        payment_plan = "none"

    spending_changes = calculate_spending_changes_needed(
        requested_amount=requested_amount,
        amount_safe_today=amount_safe_today,
        user_events=user_events,
        profile=profile,
    )
    spending_changes_output = "|".join(spending_changes) if spending_changes else "none"

    decision_explanation = build_decision_explanation(
        request=request,
        profile=profile,
        forecast=forecast,
        amount_safe_today=amount_safe_today,
        affordability_status_value=affordability_status,
        recommended_method=recommended_method,
        payment_plan=payment_plan,
        earliest_full_payment_date=earliest_full_payment_date,
        spending_changes_output=spending_changes_output,
    )

    return {
        "request_id": request_id,
        "amount_safe_to_pay": round(amount_safe_today, 2),
        "affordability_status": affordability_status,
        "recommended_payment_method": recommended_method,
        "payment_plan": payment_plan,
        "earliest_date_for_full_payment": (
            earliest_full_payment_date.strftime("%Y-%m-%d")
            if earliest_full_payment_date is not None else ""
        ),
        "spending_changes_needed": spending_changes_output,
        "decision_explanation": decision_explanation,
    }


def main():
    (
        requests,
        profiles,
        financial_events,
        payment_options,
        messages,
        images,
        exchange_rates,
    ) = load_data()

    # The prediction file is requests.csv only. Every request gets exactly one row.
    results = []
    failures = []

    total = len(requests)
    print("=" * 60)
    print("BUY OR WAIT? - FINAL BATCH RUN")
    print(f"Requests found: {total}")
    print("=" * 60)

    for index, (_, request) in enumerate(requests.iterrows(), start=1):
        request_id = request["request_id"]
        try:
            result = process_request(
                request=request,
                profiles=profiles,
                financial_events=financial_events,
                payment_options=payment_options,
                exchange_rates=exchange_rates,
            )
            results.append(result)
            print(
                f"[{index:03d}/{total:03d}] {request_id}: "
                f"{result['affordability_status']} | "
                f"{result['recommended_payment_method']} | "
                f"safe={result['amount_safe_to_pay']:.2f}"
            )
        except Exception as exc:
            failures.append((request_id, str(exc)))
            # Keep one valid output row per request even if an isolated row fails.
            results.append({
                "request_id": str(request_id),
                "amount_safe_to_pay": 0.0,
                "affordability_status": "not_affordable",
                "recommended_payment_method": "not_recommended",
                "payment_plan": "none",
                "earliest_date_for_full_payment": "",
                "spending_changes_needed": "none",
                "decision_explanation": (
                    f"Unable to establish a safe payment recommendation for {request_id}; "
                    "the conservative fallback is not_recommended."
                ),
            })
            print(f"[{index:03d}/{total:03d}] {request_id}: ERROR -> {exc}")

    output_columns = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    ]

    output = pd.DataFrame(results, columns=output_columns)
    output = output.drop_duplicates(subset=["request_id"], keep="last")

    # Restore the exact request order from requests.csv.
    order = {str(rid): i for i, rid in enumerate(requests["request_id"].astype(str))}
    output["_order"] = output["request_id"].astype(str).map(order)
    output = output.sort_values("_order").drop(columns=["_order"])

    output_path = BASE / "output.csv"
    output.to_csv(output_path, index=False)

    print("\n" + "=" * 60)
    print("RUN COMPLETE")
    print(f"Output rows : {len(output)}")
    print(f"Output file : {output_path}")
    print(f"Failures    : {len(failures)}")

    if failures:
        print("\nFailures:")
        for request_id, error in failures[:20]:
            print(f"  {request_id}: {error}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")

    print("\nStatus counts:")
    print(output["affordability_status"].value_counts().to_string())
    print("\nMethod counts:")
    print(output["recommended_payment_method"].value_counts().to_string())


if __name__ == "__main__":
    main()
