import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DEFAULT_USER = os.getenv("FINPILOT_DEFAULT_USER", "user_26")
VERSION = "0.8.0"

app = FastAPI(title="FinPilot", version=VERSION)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

DATA = engine.load_data()
REQUESTS, PROFILES, EVENTS, PAYMENT_OPTIONS, MESSAGES, IMAGES, RATES = DATA

# Prototype personal-data store: financial onboarding is saved in Supabase Auth
# user metadata, so no extra SQL migration is required for v0.7.
CUSTOM_PROFILES = {}
CUSTOM_EVENTS = {}
CUSTOM_DATA_BY_AUTH_ID = {}



def _supabase_headers(access_token: str):
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    anon = os.getenv("SUPABASE_ANON_KEY", "")
    if not url or not anon or not access_token:
        return None
    return {
        "apikey": anon,
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def authenticate_access_token(access_token: str):
    """Validate a Supabase access token and return the Auth user object."""
    headers = _supabase_headers(access_token)
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not headers or not base:
        return None
    req = URLRequest(f"{base}/auth/v1/user", headers=headers, method="GET")
    try:
        with urlopen(req, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None


def auth_profile(access_token: str):
    """Resolve a Supabase session to the user's private FinPilot identity/data."""
    auth_user = authenticate_access_token(access_token)
    if not auth_user or not auth_user.get("id"):
        return None
    headers = _supabase_headers(access_token)
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    uid = str(auth_user["id"])
    metadata = auth_user.get("user_metadata") or {}
    finpilot = metadata.get("finpilot") if isinstance(metadata, dict) else None
    if isinstance(finpilot, dict) and finpilot.get("balance") is not None:
        _cache_custom_user(uid, auth_user, finpilot)
        return {
            "auth_user": auth_user,
            "auth_user_id": uid,
            "demo_user_id": uid,
            "display_name": metadata.get("full_name") or metadata.get("name") or auth_user.get("email", "FinPilot user").split("@")[0],
            "data_mode": "personal_metadata",
        }
    # Backward-compatible lookup for profiles created by v0.6 SQL.
    if headers and base:
        query = f"{base}/rest/v1/fp_profiles?id=eq.{uid}&select=id,display_name,demo_user_id,home_currency"
        req = URLRequest(query, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=8) as response:
                rows = json.loads(response.read().decode("utf-8"))
            if rows and rows[0].get("demo_user_id"):
                return {
                    "auth_user": auth_user,
                    "auth_user_id": uid,
                    "demo_user_id": str(rows[0]["demo_user_id"]),
                    "display_name": rows[0].get("display_name"),
                    "data_mode": "demo_dataset",
                }
        except (HTTPError, URLError, TimeoutError, ValueError):
            pass
    return {
        "auth_user": auth_user,
        "auth_user_id": uid,
        "demo_user_id": DEFAULT_USER,
        "display_name": metadata.get("full_name") or metadata.get("name") or "FinPilot user",
        "data_mode": "needs_onboarding",
    }


def _cache_custom_user(uid: str, auth_user: dict, finpilot: dict):
    """Convert onboarding JSON into the dataframe shape expected by the engine."""
    currency = str(finpilot.get("currency") or "INR").upper()
    balance = float(finpilot.get("balance") or 0)
    minimum = float(finpilot.get("minimum_balance") or 0)
    income = float(finpilot.get("monthly_income") or 0)
    essential = float(finpilot.get("monthly_essential_expenses") or 0)
    flexible = float(finpilot.get("monthly_flexible_expenses") or 0)
    today = date.today()
    profile = {
        "user_id": uid, "home_currency": currency,
        "current_available_balance": balance,
        "minimum_balance_to_keep": minimum,
        "financial_priorities": "emergency_savings|retirement_investment",
        "payment_methods_user_will_consider": "full_payment|partial_payment|installments",
        "history_end_date": today.isoformat(),
    }
    CUSTOM_PROFILES[uid] = profile
    rows = []
    def add_history(event_id, name, amount, category, direction, flexibility):
        if amount <= 0: return
        for days in (60, 30):
            d = today - timedelta(days=days)
            rows.append({
                "user_id": uid, "event_id": f"{event_id}_{days}", "event_type": "income" if direction == "in" else "expense",
                "description": name, "category": category, "direction": direction, "status": "settled",
                "flexibility": flexibility, "amount": amount, "currency": currency,
                "event_date": d.isoformat(), "settlement_date": d.isoformat(), "linked_event_id": "",
            })
    add_history("salary", "Monthly income", income, "income", "in", "fixed")
    add_history("essential_expense", "Essential monthly expenses", essential, "essential", "out", "fixed")
    add_history("flexible_expense", "Flexible monthly expenses", flexible, "subscription", "out", "reducible_or_stoppable")
    df = engine.normalize_financial_events(__import__("pandas").DataFrame(rows))
    CUSTOM_EVENTS[uid] = df
    CUSTOM_DATA_BY_AUTH_ID[uid] = finpilot


def resolve_request_identity(authorization: str | None):
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        profile = auth_profile(token)
        return profile
    return {"auth_user_id": None, "demo_user_id": DEFAULT_USER, "display_name": "Guest demo", "data_mode": "demo_dataset"}


def get_profile(user_id: str):
    if str(user_id) in CUSTOM_PROFILES:
        return CUSTOM_PROFILES[str(user_id)]
    rows = PROFILES[PROFILES["user_id"].astype(str) == str(user_id)]
    return rows.iloc[0] if not rows.empty else None


def get_user_events(user_id: str):
    if str(user_id) in CUSTOM_EVENTS:
        return CUSTOM_EVENTS[str(user_id)].copy()
    return engine.get_active_events(EVENTS, user_id)


def money(value, currency):
    try:
        return f"{float(value):,.2f} {currency}"
    except Exception:
        return f"{value} {currency}"


def extract_amount(text: str):
    m = re.search(r"(?:₹|rs\.?|inr|usd|\$|eur|€|zar|idr)?\s*([0-9][0-9,]*(?:\.\d+)?)\s*(k|thousand|m|million)?", text.lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").lower()
    if suffix in {"k", "thousand"}:
        value *= 1000
    elif suffix in {"m", "million"}:
        value *= 1_000_000
    return value


def extract_percent(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def user_summary(user_id: str):
    profile = get_profile(user_id)
    if profile is None:
        return None
    home = str(profile["home_currency"])
    events = get_user_events(user_id)
    getter = profile.get if isinstance(profile, dict) else profile.get
    personal = _personal_finpilot_data(user_id)
    result = {
        "user_id": user_id,
        "home_currency": home,
        "current_available_balance": float(getter("current_available_balance", 0)),
        "minimum_balance_to_keep": float(getter("minimum_balance_to_keep", 0)),
        "financial_priorities": str(getter("financial_priorities", "")),
        "payment_methods_user_will_consider": str(getter("payment_methods_user_will_consider", "")),
        "active_events": 0 if personal else int(len(events)),
    }
    if personal:
        result.update({
            "monthly_income": float(personal.get("monthly_income", 0)),
            "monthly_essential_expenses": float(personal.get("monthly_essential_expenses", 0)),
            "monthly_flexible_expenses": float(personal.get("monthly_flexible_expenses", 0)),
            "monthly_surplus": float(personal.get("monthly_income", 0)) - float(personal.get("monthly_essential_expenses", 0)) - float(personal.get("monthly_flexible_expenses", 0)),
        })
    return result


def latest_request_template(user_id: str, amount: float, question: str):
    rows = REQUESTS[REQUESTS["user_id"].astype(str) == str(user_id)]
    if rows.empty:
        request_date = date.today()
        desired = request_date + timedelta(days=30)
        return {
            "request_id": f"chat_{user_id}_{amount}", "user_id": user_id,
            "request_date": request_date.isoformat(), "request_type": "purchase",
            "requested_amount": amount, "desired_completion_date": desired.isoformat(),
            "allows_partial_payment": True, "request_text": question,
        }
    row = rows.sort_values("request_date").iloc[-1].copy()
    request_date = engine.parse_date(row["request_date"])
    desired = engine.parse_date(row["desired_completion_date"]) or (request_date + timedelta(days=30))
    row["requested_amount"] = amount
    row["request_text"] = question
    row["request_date"] = request_date.isoformat()
    row["desired_completion_date"] = desired.isoformat()
    return row.to_dict()



def _personal_finpilot_data(user_id: str):
    """Return the persisted personal onboarding data for a signed-in user."""
    data = CUSTOM_DATA_BY_AUTH_ID.get(str(user_id))
    if isinstance(data, dict):
        return data
    return None


def personal_forecast(user_id: str, days: int = 90):
    """Conservative personal forecast using the user's monthly snapshot.

    Monthly net cash flow is spread evenly across the forecast days because
    onboarding does not yet contain exact salary/expense dates. This keeps
    calculations deterministic and avoids pretending we know transaction dates.
    """
    profile = get_profile(user_id)
    data = _personal_finpilot_data(user_id)
    if profile is None or not data:
        return None
    balance = float(profile.get("current_available_balance", 0))
    minimum = float(profile.get("minimum_balance_to_keep", 0))
    income = float(data.get("monthly_income", 0))
    essential = float(data.get("monthly_essential_expenses", 0))
    flexible = float(data.get("monthly_flexible_expenses", 0))
    daily_net = (income - essential - flexible) / 30.0
    start = date.today()
    points = []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        projected = balance + daily_net * i
        points.append((d, projected))
    low_date, low_balance = min(points, key=lambda x: x[1])
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=days)).isoformat(),
        "lowest_projected_balance": round(low_balance, 2),
        "lowest_projected_date": low_date.isoformat(),
        "minimum_balance": minimum,
        "safe_spending_capacity_today": round(max(0.0, low_balance - minimum), 2),
        "safe": bool(low_balance >= minimum - 1e-9),
        "currency": str(profile.get("home_currency", "INR")),
        "monthly_income": income,
        "monthly_essential_expenses": essential,
        "monthly_flexible_expenses": flexible,
        "monthly_surplus": income - essential - flexible,
    }


def personal_affordability(user_id: str, amount: float, question: str):
    profile = get_profile(user_id)
    data = _personal_finpilot_data(user_id)
    if profile is None or not data:
        return {"error": "Complete your FinPilot financial profile first."}
    currency = str(profile.get("home_currency", "INR"))
    balance = float(profile.get("current_available_balance", 0))
    minimum = float(profile.get("minimum_balance_to_keep", 0))
    income = float(data.get("monthly_income", 0))
    essential = float(data.get("monthly_essential_expenses", 0))
    flexible = float(data.get("monthly_flexible_expenses", 0))
    monthly_surplus = income - essential - flexible
    daily_net = monthly_surplus / 30.0
    safe_today = max(0.0, min(amount, balance - minimum))
    # Full payment date is the first forecast day where paying the entire
    # amount would still leave the minimum balance protected.
    earliest = None
    for i in range(91):
        projected_before_payment = balance + daily_net * i
        if projected_before_payment - amount >= minimum - 1e-9:
            earliest = date.today() + timedelta(days=i)
            break
    desired = date.today() + timedelta(days=30)
    if safe_today >= amount - 1e-9:
        status = "affordable_now"
        method = "full_payment"
        plan = f"{date.today().isoformat()}:{amount:.2f}"
    elif earliest is not None and earliest <= desired:
        status = "affordable_later"
        method = "wait"
        plan = f"{earliest.isoformat()}:{amount:.2f}"
    elif safe_today > 0:
        status = "affordable_with_plan"
        method = "partial_payment"
        if earliest is not None:
            plan = f"{date.today().isoformat()}:{safe_today:.2f}|{earliest.isoformat()}:{amount-safe_today:.2f}"
        else:
            plan = f"{date.today().isoformat()}:{safe_today:.2f}"
    else:
        status = "not_affordable"
        method = "not_recommended"
        plan = "none"
    if earliest is None:
        earliest_text = "not within the 90-day forecast"
    else:
        earliest_text = earliest.isoformat()
    forecast = personal_forecast(user_id)
    explanation = (
        f"The requested amount is {amount:.2f} {currency}. The maximum amount that can safely be paid today is "
        f"{safe_today:.2f} {currency}. Your current balance is {balance:.2f} {currency} and your required minimum is "
        f"{minimum:.2f} {currency}. Your monthly income is {income:.2f} {currency}, with {essential:.2f} {currency} in "
        f"essential expenses and {flexible:.2f} {currency} in flexible expenses, leaving a monthly surplus of "
        f"{monthly_surplus:.2f} {currency}. The lowest projected balance before the purchase is "
        f"{forecast['lowest_projected_balance']:.2f} {currency} on {forecast['lowest_projected_date']}."
    )
    return {
        "request_id": f"personal_{user_id}_{amount}",
        "amount_safe_to_pay": round(safe_today, 2),
        "affordability_status": status,
        "recommended_payment_method": method,
        "payment_plan": plan,
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else None,
        "spending_changes_needed": "none",
        "decision_explanation": explanation,
        "currency": currency,
    }


def personal_expenses(user_id: str):
    data = _personal_finpilot_data(user_id)
    if not data:
        return []
    c = str(get_profile(user_id).get("home_currency", "INR"))
    return [
        {"category": "essential expenses", "amount": float(data.get("monthly_essential_expenses", 0)), "currency": c},
        {"category": "flexible expenses", "amount": float(data.get("monthly_flexible_expenses", 0)), "currency": c},
    ]


def personal_income(user_id: str):
    data = _personal_finpilot_data(user_id)
    if not data:
        return []
    c = str(get_profile(user_id).get("home_currency", "INR"))
    return [{"category": "monthly income", "amount": float(data.get("monthly_income", 0)), "currency": c}]

def run_affordability(user_id: str, amount: float, question: str, simulated_events=None):
    if str(user_id) in CUSTOM_PROFILES and _personal_finpilot_data(user_id):
        return personal_affordability(user_id, amount, question)
    profile = get_profile(user_id)
    if profile is None:
        return {"error": f"Unknown user {user_id}."}
    request = latest_request_template(user_id, amount, question)
    if str(user_id) in CUSTOM_PROFILES:
        import pandas as pd
        profile_df = pd.DataFrame([profile])
        events_df = simulated_events if simulated_events is not None else get_user_events(user_id)
    else:
        profile_df = PROFILES
        events_df = simulated_events if simulated_events is not None else EVENTS
    result = engine.process_request(request, profile_df, events_df, PAYMENT_OPTIONS, RATES)
    result["user"] = user_summary(user_id)
    return result


def forecast_snapshot(user_id: str):
    if str(user_id) in CUSTOM_PROFILES and _personal_finpilot_data(user_id):
        return personal_forecast(user_id)
    profile = get_profile(user_id)
    if profile is None:
        return None
    home = str(profile["home_currency"])
    start = engine.parse_date(profile.get("history_end_date", "")) if "history_end_date" in profile else None
    if start is None:
        user_events = get_user_events(user_id)
        dates = [engine.parse_date(x) for x in user_events.get("event_date", [])] if not user_events.empty else []
        dates = [d for d in dates if d]
        start = max(dates) if dates else date.today()
    end = start + timedelta(days=90)
    events = get_user_events(user_id)
    recurring = engine.generate_recurring_events(events, start, end)
    forecast = engine.forecast_balance(float(profile["current_available_balance"]), float(profile["minimum_balance_to_keep"]), events, recurring, home, RATES, start, end)
    safe_capacity = max(0.0, forecast["lowest_balance"] - float(profile["minimum_balance_to_keep"]))
    return {
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "lowest_projected_balance": round(float(forecast["lowest_balance"]), 2),
        "lowest_projected_date": engine.date_string(forecast["lowest_date"]),
        "minimum_balance": float(profile["minimum_balance_to_keep"]),
        "safe_spending_capacity_today": round(safe_capacity, 2),
        "safe": bool(forecast["safe"]), "currency": home,
    }


def expense_breakdown(user_id: str):
    if str(user_id) in CUSTOM_PROFILES and _personal_finpilot_data(user_id):
        return personal_expenses(user_id)
    events = get_user_events(user_id)
    if events.empty:
        return []
    exp = events[events["direction"].apply(engine.direction_is_expense)]
    if exp.empty:
        return []
    grouped = exp.groupby("category", dropna=False)["amount"].sum().sort_values(ascending=False).head(8)
    return [{"category": str(k), "amount": round(float(v), 2)} for k, v in grouped.items()]


def income_breakdown(user_id: str):
    if str(user_id) in CUSTOM_PROFILES and _personal_finpilot_data(user_id):
        return personal_income(user_id)
    events = get_user_events(user_id)
    if events.empty:
        return []
    inc = events[~events["direction"].apply(engine.direction_is_expense)]
    if inc.empty:
        return []
    grouped = inc.groupby("category", dropna=False)["amount"].sum().sort_values(ascending=False).head(8)
    return [{"category": str(k), "amount": round(float(v), 2)} for k, v in grouped.items()]


def upcoming_events(user_id: str, limit=8):
    events = get_user_events(user_id).copy()
    if events.empty:
        return []
    events["_date"] = events["event_date"].apply(engine.parse_date)
    events = events[events["_date"].notna()].sort_values("_date")
    out = []
    for _, row in events.head(limit).iterrows():
        out.append({
            "event_id": str(row.get("event_id", "")), "date": row["_date"].isoformat(),
            "description": str(row.get("description", "")), "category": str(row.get("category", "")),
            "direction": str(row.get("direction", "")), "amount": round(float(engine.to_float(row.get("amount", 0))), 2),
            "currency": str(row.get("currency", "")), "status": str(row.get("status", "")),
        })
    return out


def changeable_expenses(user_id: str):
    profile = get_profile(user_id)
    events = get_user_events(user_id)
    if profile is None:
        return []
    return engine.find_changeable_recurring_expenses(events, profile)[:8]


def match_change_candidate(user_id: str, text: str):
    candidates = changeable_expenses(user_id)
    lower = text.lower()
    for c in candidates:
        terms = [c["event_id"].lower(), c["category"].lower(), c["description"].lower()]
        if any(term and term in lower for term in terms):
            return c
    tokens = set(re.findall(r"[a-z0-9]+", lower))
    for c in candidates:
        words = set(re.findall(r"[a-z0-9]+", (c["category"] + " " + c["description"]).lower()))
        if words & tokens:
            return c
    return candidates[0] if candidates else None


def simulate_change(user_id: str, candidate: dict, new_amount: float):
    events = get_user_events(user_id).copy()
    mask = (
        events["event_type"].astype(str).eq(str(candidate.get("event_type", "")))
        & events["category"].astype(str).eq(str(candidate.get("category", "")))
        & events["description"].astype(str).eq(str(candidate.get("description", "")))
        & events["direction"].astype(str).eq(str(candidate.get("direction", "")))
    )
    if "amount" in events.columns:
        events.loc[mask, "amount"] = new_amount
    return events


def what_if(user_id: str, text: str):
    candidate = match_change_candidate(user_id, text)
    if not candidate:
        return {"ok": False, "answer": "I couldn't find a recurring expense that your profile allows me to stop or reduce."}
    pct = extract_percent(text)
    lower = text.lower()
    if any(k in lower for k in ["stop", "cancel", "remove"]):
        new_amount, action = 0.0, "stop"
    elif pct is not None:
        new_amount = max(candidate.get("new_amount", 0.0), candidate["amount"] * (1 - pct / 100.0))
        action = f"reduce by {pct:g}%"
    else:
        new_amount, action = float(candidate.get("new_amount", candidate["amount"] * 0.5)), "reduce"
    before = forecast_snapshot(user_id)
    simulated = simulate_change(user_id, candidate, new_amount)
    profile = get_profile(user_id)
    start, end = engine.parse_date(before["start_date"]), engine.parse_date(before["end_date"])
    recurring = engine.generate_recurring_events(simulated, start, end)
    forecast = engine.forecast_balance(float(profile["current_available_balance"]), float(profile["minimum_balance_to_keep"]), simulated, recurring, str(profile["home_currency"]), RATES, start, end)
    after_capacity = max(0.0, forecast["lowest_balance"] - float(profile["minimum_balance_to_keep"]))
    return {
        "ok": True, "description": candidate["description"], "category": candidate["category"],
        "old_amount": candidate["amount"], "new_amount": round(new_amount, 2), "action": action,
        "before": before,
        "after": {"lowest_projected_balance": round(float(forecast["lowest_balance"]), 2), "lowest_projected_date": engine.date_string(forecast["lowest_date"]), "safe_spending_capacity_today": round(after_capacity, 2), "safe": bool(forecast["safe"])},
        "capacity_improvement": round(after_capacity - before["safe_spending_capacity_today"], 2),
    }


def classify_intent(text: str, history=None):
    lower = re.sub(r"\s+", " ", text.lower()).strip()
    if any(k in lower for k in ["what if", "if i stop", "if i reduce", "cancel my", "cut my", "reduce my", "remove my", "stop my", "lower my"]):
        return "what_if"
    if any(k in lower for k in ["safe to spend", "safely spend", "safe spending", "spending capacity", "how much can i spend", "maximum i can spend", "max i can spend", "safe amount to spend"]):
        return "safe_spend"
    if any(k in lower for k in ["where am i spending", "where do i spend", "biggest expense", "biggest expenses", "largest expense", "largest expenses", "spending breakdown", "expense breakdown", "top expenses"]):
        return "expenses"
    if any(k in lower for k in ["upcoming", "next payment", "next payments", "what payments", "payments coming", "future payments"]):
        return "upcoming"
    if any(k in lower for k in ["income", "salary", "earnings", "money coming", "income breakdown"]):
        return "income"
    if any(k in lower for k in ["snapshot", "financial health", "financial summary", "how much money", "financial position"]) or lower in {"balance", "my balance"}:
        return "snapshot"
    if any(k in lower for k in ["afford", "buy", "purchase", "cost", "can i pay", "should i buy", "should i pay"]) or extract_amount(text) is not None:
        return "affordability"
    if any(k in lower for k in ["help", "what can you do", "hello", "hi ", "hey"]):
        return "help"
    # Short follow-ups inherit the last meaningful intent.
    if history:
        for item in reversed(history):
            if item.get("role") == "user":
                prior = item.get("content", "")
                if prior and prior != text:
                    p = classify_intent(prior, None)
                    if p not in {"general", "help"}:
                        return p
    return "general"


# A small tool registry makes the financial agent explicit and testable.
TOOLS = {
    "financial_snapshot": lambda uid, text: user_summary(uid),
    "safe_spending_capacity": lambda uid, text: forecast_snapshot(uid),
    "expense_breakdown": lambda uid, text: expense_breakdown(uid),
    "income_breakdown": lambda uid, text: income_breakdown(uid),
    "upcoming_payments": lambda uid, text: upcoming_events(uid),
    "affordability_check": lambda uid, text: run_affordability(uid, extract_amount(text), text) if extract_amount(text) is not None else None,
    "what_if_simulator": lambda uid, text: what_if(uid, text),
}

INTENT_TO_TOOL = {
    "snapshot": "financial_snapshot", "safe_spend": "safe_spending_capacity", "expenses": "expense_breakdown",
    "income": "income_breakdown", "upcoming": "upcoming_payments", "affordability": "affordability_check",
    "what_if": "what_if_simulator",
}


def tool_result(user_id: str, text: str, history=None):
    intent = classify_intent(text, history)
    tool_name = INTENT_TO_TOOL.get(intent)
    result = TOOLS[tool_name](user_id, text) if tool_name else None
    return intent, tool_name, result


def deterministic_answer(user_id: str, text: str, history=None):
    summary = user_summary(user_id)
    if summary is None:
        return "I don't recognize that demo user. Try user_26 or another user from the dataset."
    intent, _, result = tool_result(user_id, text, history)
    currency = summary["home_currency"]

    if intent == "affordability":
        amount = extract_amount(text)
        if amount is None:
            return f"I can check that. Tell me the purchase amount, for example **Can I afford ₹60,000?** Your current available balance is **{money(summary['current_available_balance'], currency)}**."
        if not result or "error" in result:
            return result.get("error", "I couldn't complete the affordability check.") if isinstance(result, dict) else "I couldn't complete the affordability check."
        return (f"### Affordability check\n\n**Requested:** {money(amount, currency)}\n\n**Safe to pay today:** {money(result['amount_safe_to_pay'], currency)}\n\n**Decision:** `{result['affordability_status']}`\n\n**Recommended:** `{result['recommended_payment_method']}`\n\n**Earliest full payment:** {result.get('earliest_date_for_full_payment') or 'not within the forecast'}\n\n**Payment plan:** `{result.get('payment_plan', 'none')}`\n\n{result.get('decision_explanation', '')}")

    if intent == "snapshot":
        return f"### Your financial snapshot\n\n**Available balance:** {money(summary['current_available_balance'], currency)}\n\n**Minimum balance to keep:** {money(summary['minimum_balance_to_keep'], currency)}\n\n**Active financial events:** {summary['active_events']}\n\n**Priorities:** {summary['financial_priorities'] or 'not specified'}\n\n**Payment preferences:** {summary['payment_methods_user_will_consider'] or 'not specified'}"

    if intent == "safe_spend":
        f = result
        return f"### Safe spending capacity\n\nYou can safely spend up to **{money(f['safe_spending_capacity_today'], currency)}** today while keeping the required minimum balance.\n\nThe lowest projected balance is **{money(f['lowest_projected_balance'], currency)}** on **{f['lowest_projected_date']}**."

    if intent == "expenses":
        items = result or []
        if not items: return "I couldn't find expense records for this demo user."
        return "### Spending breakdown\n\n" + "\n".join(f"- **{i['category']}** — {money(i['amount'], currency)}" for i in items)

    if intent == "income":
        items = result or []
        if not items: return "I couldn't find income records for this demo user."
        return "### Income breakdown\n\n" + "\n".join(f"- **{i['category']}** — {money(i['amount'], currency)}" for i in items)

    if intent == "upcoming":
        items = result or []
        if not items: return "I couldn't find upcoming financial events in this demo dataset."
        return "### Upcoming financial events\n\n" + "\n".join(f"- **{i['date']}** {'↑' if not engine.direction_is_expense(i['direction']) else '↓'} {money(i['amount'], i['currency'])} — {i['description']}" for i in items)

    if intent == "what_if":
        r = result
        if not r.get("ok"): return r["answer"]
        return f"### What-if simulation\n\n**Change:** {r['action']} — {r['description']}\n\n**Amount:** {money(r['old_amount'], currency)} → {money(r['new_amount'], currency)}\n\n**Safe spending capacity before:** {money(r['before']['safe_spending_capacity_today'], currency)}\n\n**Safe spending capacity after:** {money(r['after']['safe_spending_capacity_today'], currency)}\n\n**Improvement:** {money(r['capacity_improvement'], currency)}\n\n**Lowest projected balance after:** {money(r['after']['lowest_projected_balance'], currency)} on {r['after']['lowest_projected_date']}"

    return "### I can help you decide\n\nTry **Can I afford ₹60,000?**, **How much can I safely spend?**, **Where am I spending the most?**, **What payments are coming up?**, or **What if I stop my biggest recurring expense?**"


SYSTEM_PROMPT = """You are FinPilot, a concise conversational financial decision copilot. The deterministic FinPilot tool output is the source of truth for all financial numbers, dates, forecasts, affordability decisions and simulations. Never invent financial facts and never claim live bank access. Explain the supplied result naturally. If information is missing, ask one focused question. Personal onboarding data is user-provided and persisted with the signed-in account; it is not live bank data. Do not imply live bank access or regulated financial advice."""


def llm_answer(user_id: str, text: str, history: list):
    deterministic = deterministic_answer(user_id, text, history)
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return deterministic, False
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        intent, tool_name, result = tool_result(user_id, text, history)
        context = {"intent": intent, "tool": tool_name, "tool_result": result, "authoritative_answer": deterministic}
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history[-12:]:
            role = item.get("role", "user")
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": str(item.get("content", ""))[:6000]})
        messages.append({"role": "user", "content": text})
        messages.append({"role": "system", "content": "Authoritative FinPilot tool context:\n" + json.dumps(context, default=str)})
        response = client.responses.create(model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"), input=messages)
        return response.output_text or deterministic, True
    except Exception:
        return deterministic, False


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict] = Field(default_factory=list)


class WhatIfRequest(BaseModel):
    event_id: str | None = None
    new_amount: float = Field(ge=0)


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION, "ai_enabled": bool(os.getenv("OPENAI_API_KEY")), "supabase_ready": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))}


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in session.")
    uid = identity.get("demo_user_id", DEFAULT_USER)
    mode = identity.get("data_mode", "demo_dataset")
    return {"authenticated": bool(identity.get("auth_user_id")), "auth_user_id": identity.get("auth_user_id"), "email": (identity.get("auth_user") or {}).get("email"), "display_name": identity.get("display_name"), "financial_profile": user_summary(uid) if get_profile(uid) is not None else None, "data_mode": mode}


@app.get("/api/onboarding")
def onboarding_get(authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if not identity or not identity.get("auth_user_id"):
        raise HTTPException(status_code=401, detail="Sign in required.")
    auth_user = identity.get("auth_user") or {}
    metadata = auth_user.get("user_metadata") or {}
    finpilot = metadata.get("finpilot") if isinstance(metadata, dict) else None
    return {"completed": isinstance(finpilot, dict) and finpilot.get("balance") is not None, "data": finpilot if isinstance(finpilot, dict) else {}}


class OnboardingRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    balance: float = Field(ge=0)
    minimum_balance: float = Field(ge=0)
    monthly_income: float = Field(ge=0)
    monthly_essential_expenses: float = Field(ge=0)
    monthly_flexible_expenses: float = Field(ge=0)


def _supabase_update_user_metadata(access_token: str, data: dict):
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    headers = _supabase_headers(access_token)
    if not base or not headers:
        return False, "Supabase is not configured."
    req = URLRequest(f"{base}/auth/v1/user", headers=headers, method="PUT", data=json.dumps({"data": data}).encode("utf-8"))
    try:
        with urlopen(req, timeout=8) as response:
            return 200 <= response.status < 300, ""
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        return False, body
    except (URLError, TimeoutError) as e:
        return False, str(e)


@app.put("/api/onboarding")
def onboarding_save(req: OnboardingRequest, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required.")
    token = authorization[7:].strip()
    identity = auth_profile(token)
    if not identity or not identity.get("auth_user_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in session.")
    if req.minimum_balance > req.balance:
        raise HTTPException(status_code=400, detail="Minimum balance cannot be greater than your current balance.")
    data = {
        "currency": req.currency.upper(), "balance": req.balance, "minimum_balance": req.minimum_balance,
        "monthly_income": req.monthly_income, "monthly_essential_expenses": req.monthly_essential_expenses,
        "monthly_flexible_expenses": req.monthly_flexible_expenses,
        "updated_at": date.today().isoformat(),
    }
    auth_user = identity.get("auth_user") or {}
    metadata = auth_user.get("user_metadata") or {}
    merged = dict(metadata) if isinstance(metadata, dict) else {}
    merged["finpilot"] = data
    ok, error = _supabase_update_user_metadata(token, merged)
    if not ok:
        raise HTTPException(status_code=502, detail="Could not save your financial profile. " + error[:300])
    _cache_custom_user(identity["auth_user_id"], auth_user, data)
    return {"ok": True, "data_mode": "personal_metadata", "financial_profile": user_summary(identity["auth_user_id"])}


@app.get("/api/summary")
def my_summary(authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    uid = identity["demo_user_id"]
    if get_profile(uid) is None:
        raise HTTPException(status_code=409, detail="Complete your FinPilot financial profile first.")
    return user_summary(uid)


@app.get("/api/dashboard")
def my_dashboard(authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    uid = identity["demo_user_id"]
    if get_profile(uid) is None:
        raise HTTPException(status_code=409, detail="Complete your FinPilot financial profile first.")
    summary_data = user_summary(uid)
    return {"summary": summary_data, "forecast": forecast_snapshot(uid), "expenses": expense_breakdown(uid), "income": income_breakdown(uid), "changeable_expenses": changeable_expenses(uid), "upcoming": upcoming_events(uid)}


@app.post("/api/chat")
def chat(req: ChatRequest, authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Sign in required to use personal financial data.")
    uid = identity["demo_user_id"]
    if get_profile(uid) is None:
        raise HTTPException(status_code=409, detail="Complete your FinPilot financial profile first.")
    answer, ai_enabled = llm_answer(uid, req.message, req.history)
    intent, tool_name, _ = tool_result(uid, req.message, req.history)
    return {"answer": answer, "user_id": uid, "intent": intent, "tool": tool_name, "ai_enabled": ai_enabled, "data_mode": identity.get("data_mode", "demo_dataset")}


@app.post("/api/what-if")
def what_if_api(req: WhatIfRequest, authorization: str | None = Header(default=None)):
    identity = resolve_request_identity(authorization)
    if identity is None:
        raise HTTPException(status_code=401, detail="Sign in required to use personal financial data.")
    uid = identity["demo_user_id"]
    if get_profile(uid) is None:
        raise HTTPException(status_code=409, detail="Complete your FinPilot financial profile first.")
    candidates = changeable_expenses(uid)
    candidate = next((c for c in candidates if c["event_id"] == req.event_id), None) if req.event_id else (candidates[0] if candidates else None)
    if not candidate:
        return {"error": "No matching changeable recurring expense found."}
    simulated = simulate_change(uid, candidate, req.new_amount)
    before = forecast_snapshot(uid)
    profile = get_profile(uid)
    start, end = engine.parse_date(before["start_date"]), engine.parse_date(before["end_date"])
    recurring = engine.generate_recurring_events(simulated, start, end)
    forecast = engine.forecast_balance(float(profile["current_available_balance"]), float(profile["minimum_balance_to_keep"]), simulated, recurring, str(profile["home_currency"]), RATES, start, end)
    after_capacity = max(0.0, forecast["lowest_balance"] - float(profile["minimum_balance_to_keep"]))
    return {"event": candidate, "before": before, "after": {"safe_spending_capacity_today": round(after_capacity, 2), "lowest_projected_balance": round(float(forecast["lowest_balance"]), 2), "lowest_projected_date": engine.date_string(forecast["lowest_date"])}, "capacity_improvement": round(after_capacity - before["safe_spending_capacity_today"], 2)}
