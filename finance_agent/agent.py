"""Question routing and composition using deterministic calculated summaries."""
from datetime import date, timedelta
import re

from .api import OpenRouterClient, ServiceError, convert_currency, fetch_stock_price
from .finance import (category_breakdown, goal_status, monthly_summary,
                      selected_transactions, summarize_user_data)
from .storage import DataError, currency_code, money


def decide_route(question):
    question = question.casefold()
    if re.search(r"\b(convert|conversion|exchange rate)\b", question):
        return "fx"
    if re.search(r"\b(stock|ticker|share price|price of)\b", question):
        return "stock"
    if re.search(r"\b(categorize|category|categories)\b", question):
        return "categorize"
    if re.search(r"\b(goal|goals|target|progress)\b", question):
        return "goals"
    summary = re.search(r"\b(month|monthly|summary|summarize|income|expenses|spending|spent|net|breakdown)\b", question)
    advice = re.search(r"\b(advice|suggest|recommend|mistake|mistakes|wrong|improve|plan|help|fix)\b", question)
    if summary:
        return "budget_plus_advice" if advice else "budget"
    return "rag"


def answer_question(settings, search, question, df, month=None, currency=None, *, history=None, tool_results=None):
    summary = summarize_user_data(df, month, currency)
    result = search.retrieve(question)
    client = OpenRouterClient(settings.api_key, settings.model)
    try:
        answer = client.answer(question, summary, result["hits"], history=history, tool_results=tool_results)
        return answer, result
    finally:
        client.close()


class Clarification(ValueError):
    """A request needs explicit parameters before a tool can run."""


def parse_fx(question, previous=None):
    """Accept explicit currency codes and amounts; never infer a currency from $."""
    text = question.strip().rstrip("?!.").strip()
    previous = previous or {}
    follow = re.fullmatch(r"(?:and|what about)\s+([\d,]+(?:\.\d+)?)", text, re.I)
    if follow and previous.get("route") == "fx":
        text = f"convert {follow[1]} {previous['from_code']} to {previous['to_code']}"
    amount_pattern = r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    match = re.fullmatch(
        rf"(?:please\s+)?(?:convert\s+|how much is\s+)?{amount_pattern}\s+([a-z]{{3}})\s+(?:to|in|into)\s+([a-z]{{3}})", text, re.I)
    if match:
        amount, source, target = match.groups()
        return {"amount": float(money(amount.replace(",", ""), nonnegative=True)),
                "from_code": currency_code(source), "to_code": currency_code(target)}
    rate = re.fullmatch(r"(?:what is (?:the )?)?exchange rate\s+(?:from\s+)?([a-z]{3})\s+(?:to|in)\s+([a-z]{3})", text, re.I)
    if rate:
        return {"amount": 1.0, "from_code": currency_code(rate[1]), "to_code": currency_code(rate[2])}
    raise Clarification("Enter one conversion with an amount and two currency codes, for example: Convert 100 USD to EGP. For a rate: Exchange rate USD to EGP.")


def parse_stock(question, previous=None):
    text = question.strip().rstrip("?!").strip()
    # Explicit 'ticker' accepts lower case. Otherwise require a ticker-shaped
    # uppercase symbol so a company name such as Apple is not guessed.
    explicit = re.fullmatch(r"(?:please\s+)?(?:fetch\s+|show\s+)?(?:stock\s+)?(?:price\s+(?:for|of)\s+)?ticker\s+([a-z0-9^][a-z0-9.^=-]{0,19})", text, re.I)
    if explicit:
        return explicit[1].upper()
    patterns = [r"(?i:(?:please\s+)?(?:what is (?:the )?|show\s+|fetch\s+)?(?:stock price|share price|price|stock)\s+(?:of\s+|for\s+)?)([A-Z^][A-Z0-9.^=-]{0,19})",
                r"([A-Z^][A-Z0-9.^=-]{0,19})(?i:\s+(?:stock|stock price|share price|price))"]
    if (previous or {}).get("route") == "stock":
        patterns.append(r"(?i:and|what about)\s+([A-Z^][A-Z0-9.^=-]{0,19})")
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return match[1]
    raise Clarification("Provide one ticker symbol, for example: Stock price AAPL, or Price for ticker msft. Company names and multiple tickers need an explicit single ticker.")


def question_scope(question, df, month=None, currency=None, *, today=None):
    """Explicit question filters override sidebar values for this answer only."""
    today = today or date.today()
    periods = re.findall(r"\b\d{4}-\d{1,2}\b", question)
    if re.search(r"\bthis month\b", question, re.I):
        periods.append(today.strftime("%Y-%m"))
    if re.search(r"\blast month\b", question, re.I):
        periods.append((today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    all_dates = bool(re.search(r"\b(all dates|all months|all time)\b", question, re.I))
    if len(set(periods)) > 1 or (periods and all_dates):
        raise Clarification("Choose one month (YYYY-MM), or say all dates. Comparisons across selected months are not supported yet.")
    # Do not silently return all-time totals for an unsupported natural-language range.
    if re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|week|year|quarter|yesterday|today)\b", question, re.I) or re.search(r"\b\d{4}-\d{2}-\d{2}\b", question):
        raise Clarification("For transaction totals, specify a month as YYYY-MM, this month, last month, or all dates.")
    if periods:
        month = periods[0]
    elif all_dates:
        month = None
    known = set(df.currency.unique()) | {"USD", "EGP", "EUR", "GBP", "SAR", "AED", "CAD", "AUD", "JPY", "CHF", "CNY", "INR"}
    codes = {word.upper() for word in re.findall(r"\b[a-zA-Z]{3}\b", question) if word.upper() in known}
    explicit_code = re.search(r"\b(?:in|currency)\s+([a-z]{3})\b", question, re.I)
    if explicit_code and explicit_code[1].lower() not in {"the", "all", "for"}:
        codes.add(explicit_code[1].upper())
    all_codes = bool(re.search(r"\ball currencies\b", question, re.I))
    if len(codes) > 1 or (codes and all_codes):
        raise Clarification("Choose one currency code, or say all currencies to see separate totals.")
    if codes:
        currency = codes.pop()
    elif all_codes:
        currency = None
    selected_transactions(df, month, currency)  # validates even empty selections
    return month, currency


def run_question(settings, search, question, df, goals, month=None, currency=None, *, history=None, previous=None, ai_gate=None):
    """Execute bounded, read-only tools; optional AI explains current results.

    Return display-ready data so rerendering a saved answer never calls a service.
    No database write capability is exposed to the chat or language model.
    """
    question = question.strip()
    response = {"answer": "", "tables": [], "notes": [], "warnings": [], "sources": [],
                "context": None, "clarification": False, "error": ""}
    if not question or len(question) > 4000:
        response["clarification"] = True
        response["answer"] = "Enter a question between 1 and 4,000 characters."
        return response
    previous = previous or {}
    route = decide_route(question)
    follow = bool(re.match(r"^(and|what about)\b", question, re.I))
    if re.fullmatch(r"(?:please\s+)?(?:how much is\s+)?[\d,.]+\s+[a-z]{3}\s+(?:to|in|into)\s+[a-z]{3}[?!.]?", question, re.I):
        route = "fx"
    if follow and route == "rag" and previous.get("route"):
        route = previous["route"]
        month, currency = previous.get("month", month), previous.get("currency", currency)
    if follow and route in {"rag", "budget"} and previous.get("route") in {"budget", "budget_plus_advice", "categorize"}:
        route = previous["route"]
        month, currency = previous.get("month", month), previous.get("currency", currency)
    if re.match(r"^(?:(?:please|can you|could you)\s+)?(?:add|update|edit|delete|remove|import|save(?=\s+.{0,60}\b(?:transaction|goal|record)\b))\b", question, re.I):
        response["answer"] = "Use the transaction, CSV or goal forms to change saved data. Chat can show summaries, categories, goal progress, conversions and stock prices."
        return response
    try:
        if route == "fx":
            params = parse_fx(question, previous)
            result = convert_currency(**params, fallback_path=settings.fx_path)
            response["answer"] = (f"{params['amount']:,.2f} {params['from_code']} = "
                                  f"{result['converted']:,.2f} {params['to_code']}")
            response["notes"].append(f"Source: {result['source']}. Rate: {result['rate']:.6g}. {result['note']}")
            if result["source"] == "offline fallback":
                response["warnings"].append("This is an estimate using saved rates, which may be outdated.")
            response["context"] = {"route": route, **params}
            return response
        if route == "stock":
            ticker = parse_stock(question, previous)
            result = fetch_stock_price(ticker)
            unit = result.get("currency", "quoted currency")
            if unit in {"GBp", "GBX"}:
                unit += " (pence)"
            response["answer"] = f"{result['ticker']}: latest daily close {result['close']:,.2f} {unit}."
            response["notes"].append(f"Source: {result.get('source', 'Yahoo Finance')}. Market date: {result['market_date']}. Retrieved: {result.get('retrieved_at', 'previous request')}. Completed trading session; not a real-time quote.")
            response["context"] = {"route": route, "ticker": ticker}
            return response
        # A short follow-up must still identify a supported filter, not silently
        # repeat the previous report for an unrelated noun.
        if follow and route in {"budget", "budget_plus_advice", "categorize", "goals"}:
            if not re.fullmatch(r"(?:and|what about)\s+(?:\d{4}-\d{1,2}|[A-Za-z]{3}|this month|last month|all dates|all currencies)[?!.]?", question, re.I):
                raise Clarification("For a report follow-up, specify a month (YYYY-MM) or a currency code, or ask a complete new question.")
            code_only = re.fullmatch(r"(?:and|what about)\s+([a-z]{3})[?!.]?", question, re.I)
            if code_only:
                currency = currency_code(code_only[1])
        month, currency = question_scope(question, df, month, currency) if route != "goals" else (month, currency)
        response["context"] = {"route": route, "month": month, "currency": currency}
        tool_results = {}
        if route in {"budget", "budget_plus_advice", "categorize", "rag"}:
            response["notes"].append(f"Period: {month or 'all dates'}. Currency: {currency or 'all currencies, separate totals'}.")
        if route in {"budget", "budget_plus_advice"}:
            rows = monthly_summary(df, month, currency).to_dict("records")
            response["tables"].append({"label": "Monthly totals", "rows": rows})
            tool_results["monthly_totals"] = rows
            response["answer"] = "Calculated from all matching transactions." if rows else "No transactions match this period and currency."
        if route in {"budget", "budget_plus_advice", "categorize"}:
            rows = category_breakdown(df, month, currency).to_dict("records")
            response["tables"].append({"label": "Expenses by category and currency", "rows": rows})
            tool_results["expense_categories"] = rows
            if route == "categorize":
                response["answer"] = "Expense categories for the selected period." if rows else "No expenses match this period and currency."
        if route == "goals":
            if follow:
                raise Clarification("Goal progress shows all saved goals. Ask a complete question about your goals or use Savings Goal Tracker to select one.")
            rows = [goal_status(goal) for goal in goals]
            response["tables"].append({"label": "Savings goal progress", "rows": rows})
            tool_results["goals"] = rows
            response["answer"] = "Current progress for all saved goals (independent of transaction filters)." if rows else "No savings goals saved yet. Add one in Savings Goal Tracker."
        wants_advice = route in {"rag", "budget_plus_advice"} or (route in {"goals", "categorize"} and bool(re.search(r"\b(advice|suggest|recommend|improve|help|plan)\b", question, re.I)))
        if wants_advice:
            if not settings.api_key:
                response["notes"].append("Set OPENROUTER_API_KEY to enable AI advice. See README.md for setup.")
            else:
                if ai_gate is not None:
                    allowed, reason = ai_gate()
                    if not allowed:
                        response["warnings"].append(reason)
                        return response
                try:
                    answer, result = answer_question(settings, search, question, df, month, currency,
                                                     history=history, tool_results=tool_results)
                    response["answer"] = answer
                    response["sources"] = result["hits"]
                    if result["warning"]:
                        response["warnings"].append(result["warning"])
                except (ServiceError, OSError, ValueError, RuntimeError) as exc:
                    response["warnings"].append(f"AI advice unavailable: {exc}")
        return response
    except Clarification as exc:
        response.update(answer=str(exc), clarification=True, context=None)
    except (DataError, ServiceError, OSError, ValueError, RuntimeError) as exc:
        response.update(error=str(exc), context=None)
    return response
