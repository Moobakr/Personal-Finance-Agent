# app.py

import math
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from rag_engine import answer_with_rag
from tools import (
    calculate_budget,
    categorize_expenses,
    convert_currency,
    fetch_stock_price,
    months_to_goal,
    detect_intent,
)

# =========================
# STREAMLIT CONFIG
# =========================

st.set_page_config(
    page_title="Personal Finance Agent",
    page_icon="💰",
    layout="wide",
)


def render_sidebar():
    st.sidebar.title("⚙️ Settings & Data")

    st.sidebar.markdown("### Personal Spending CSV")
    st.sidebar.caption("Optional: upload a CSV to use in the Expense Categorizer.")

    uploaded = st.sidebar.file_uploader(
        "Upload expenses CSV",
        type=["csv"],
        help="Expected at least an 'amount' column; 'description' and 'category' are optional.",
    )
    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded)
            st.session_state.expense_df = df
            st.sidebar.success("Expenses loaded!")
        except Exception as e:
            st.sidebar.error(f"Error reading CSV: {e}")

    st.sidebar.markdown("---")
    st.sidebar.info(
        "This app is **educational** only and not professional financial, legal, or tax advice."
    )


# =========================
# TABS / UI
# =========================

def chat_agent_tab():
    st.header("💬 Personal Finance Agent (Local RAG + Tools)")

    st.markdown(
        "Ask questions like:\n"
        "- *“How should I start budgeting?”*\n"
        "- *“What’s a simple long-term investment strategy?”*\n"
        "- *“Convert 100 USD to EUR”*\n"
        "- *“What’s the current price of AAPL?”*\n\n"
        "Answers are generated using a **local rule-based RAG engine**, not external AI APIs."
    )

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for role, content in st.session_state.chat_history:
        with st.chat_message(role):
            st.markdown(content)

    user_msg = st.chat_input("Type your question...")
    if user_msg:
        st.session_state.chat_history.append(("user", user_msg))
        intent = detect_intent(user_msg)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                if intent == "currency":
                    # naive parse: look for pattern like "100 usd to eur"
                    words = user_msg.replace(",", " ").split()
                    amount = None
                    from_code = None
                    to_code = None
                    for w in words:
                        try:
                            amount = float(w)
                            break
                        except ValueError:
                            continue

                    msg_l = user_msg.lower()
                    if " to " in msg_l:
                        parts = msg_l.split(" to ")
                        left = parts[0].split()
                        right = parts[1].split()
                        from_code = left[-1].upper()
                        to_code = right[0].upper()

                    if amount is not None and from_code and to_code:
                        try:
                            result = convert_currency(amount, from_code, to_code)
                            answer = (
                                f"{amount:.2f} {from_code} ≈ **{result:.2f} {to_code}** "
                                f"(live rate via exchangerate.host)"
                            )
                        except Exception as e:
                            answer = f"Couldn't fetch currency data: `{e}`"
                    else:
                        answer = (
                            "I detected a currency question but couldn't parse it.\n\n"
                            "Try: `Convert 100 USD to EUR`."
                        )

                elif intent == "stock":
                    # look for a probable ticker: last uppercase word
                    words = user_msg.replace(",", " ").split()
                    ticker_candidates = [w.upper() for w in words if w.isalpha()]
                    symbol = ticker_candidates[-1] if ticker_candidates else None
                    if symbol:
                        data = fetch_stock_price(symbol)
                        if data is None:
                            answer = f"I couldn't find recent data for ticker `{symbol}`."
                        else:
                            answer = (
                                f"Latest daily data for **{data['symbol']}**:\n\n"
                                f"- Date: `{data['date']}`\n"
                                f"- Open: {data['open']:.2f}\n"
                                f"- High: {data['high']:.2f}\n"
                                f"- Low: {data['low']:.2f}\n"
                                f"- Close: {data['close']:.2f}"
                            )
                    else:
                        answer = "I detected a stock question, but couldn't find a ticker symbol like `AAPL`."

                elif intent in ["budget", "savings"]:
                    base_answer = answer_with_rag(user_msg)
                    tool_hint = (
                        "\n\nYou can also use the **Budget Calculator** or **Savings Goal Planner** "
                        "tabs in this app to run detailed numbers for your own situation."
                    )
                    answer = base_answer + tool_hint

                else:  # general advice
                    answer = answer_with_rag(user_msg)

                st.markdown(answer)
                st.session_state.chat_history.append(("assistant", answer))


def budget_tab():
    st.header("📊 Budget Calculator")

    col1, col2 = st.columns(2)

    with col1:
        monthly_income = st.number_input("Monthly Net Income", min_value=0.0, value=10000.0, step=100.0)
        fixed_expenses = st.number_input("Fixed Expenses (rent, loans, etc.)", min_value=0.0, value=5000.0, step=100.0)
        variable_expenses = st.number_input("Variable Expenses (food, transport, etc.)", min_value=0.0, value=2000.0, step=100.0)

    if st.button("Calculate Budget"):
        result = calculate_budget(monthly_income, fixed_expenses, variable_expenses)
        with col2:
            st.subheader("Results")
            st.write(f"**Total expenses:** {result['total_expenses']:.2f}")
            st.write(f"**Estimated savings:** {result['savings']:.2f}")
            st.write(f"**Savings rate:** {result['savings_rate']:.1f}%")

            st.markdown("**50/30/20 Reference (on your income):**")
            st.write(f"- Needs (50%): {result['recommended_needs']:.2f}")
            st.write(f"- Wants (30%): {result['recommended_wants']:.2f}")
            st.write(f"- Savings/Debt (20%): {result['recommended_savings']:.2f}")


def expenses_tab():
    st.header("💳 Expense Categorizer")

    st.markdown(
        "Use the CSV from the sidebar or upload one here.\n\n"
        "**Required column:** `amount`\n\n"
        "**Optional columns:** `description`, `category`"
    )

    df = st.session_state.get("expense_df")

    local_upload = st.file_uploader("Or upload CSV here", type=["csv"])
    if local_upload is not None:
        try:
            df = pd.read_csv(local_upload)
            st.session_state.expense_df = df
        except Exception as e:
            st.error(f"Error reading CSV: {e}")
            return

    if df is None:
        st.info("No expenses loaded yet.")
        return

    st.subheader("Raw Data")
    st.dataframe(df.head())

    if st.button("Categorize Expenses"):
        try:
            detailed_df, summary_df = categorize_expenses(df)
        except Exception as e:
            st.error(f"Error categorizing: {e}")
            return

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("By Category")
            st.dataframe(summary_df)

        with col2:
            st.subheader("Detailed (with Inferred Categories)")
            st.dataframe(detailed_df.head(50))


def currency_tab():
    st.header("💱 Currency Converter (Tool)")

    col1, col2, col3 = st.columns(3)
    with col1:
        amount = st.number_input("Amount", min_value=0.0, value=100.0)
    with col2:
        from_code = st.text_input("From currency (e.g., USD)", value="USD")
    with col3:
        to_code = st.text_input("To currency (e.g., EUR)", value="EUR")

    if st.button("Convert"):
        try:
            result = convert_currency(amount, from_code, to_code)
            st.success(f"{amount:.2f} {from_code.upper()} ≈ {result:.2f} {to_code.upper()}")
        except Exception as e:
            st.error(f"Error calling currency API: {e}")


def stocks_tab():
    st.header("📈 Stock Price Fetcher")

    symbol = st.text_input("Ticker symbol (e.g., AAPL, MSFT, TSLA)", value="AAPL")

    if st.button("Get Latest Price"):
        data = fetch_stock_price(symbol)
        if data is None:
            st.error("Could not fetch data. Check the symbol or your internet connection.")
        else:
            st.success(f"Latest price for {data['symbol']}")
            st.write(f"Date: {data['date']}")
            st.write(f"Open: {data['open']:.2f}")
            st.write(f"High: {data['high']:.2f}")
            st.write(f"Low: {data['low']:.2f}")
            st.write(f"Close: {data['close']:.2f}")


def savings_tab():
    st.header("🏦 Savings Goal Planner")

    col1, col2 = st.columns(2)
    with col1:
        current_savings = st.number_input("Current Savings", min_value=0.0, value=0.0, step=100.0)
        monthly_contribution = st.number_input("Monthly Contribution", min_value=0.0, value=500.0, step=50.0)
        annual_return = st.number_input("Expected Annual Return (%)", min_value=0.0, value=5.0, step=0.5)
        goal_amount = st.number_input("Goal Amount", min_value=0.0, value=50000.0, step=1000.0)

    if st.button("Estimate Time to Goal"):
        months = months_to_goal(current_savings, monthly_contribution, annual_return, goal_amount)
        with col2:
            if math.isinf(months):
                st.error("With the current inputs, the goal is not reachable within 50 years.")
            else:
                years = months / 12.0
                target_date = datetime.today() + timedelta(days=int(months * 30))
                st.success(
                    f"Estimated time to reach {goal_amount:.2f} is about "
                    f"**{months:.0f} months** (~{years:.1f} years)."
                )
                st.write(f"Approximate date: `{target_date.date()}`")


# =========================
# MAIN
# =========================

def main():
    render_sidebar()

    tabs = st.tabs(
        [
            "🤖 Finance Agent",
            "📊 Budget Calculator",
            "💳 Expense Categorizer",
            "💱 Currency Converter",
            "📈 Stock Prices",
            "🏦 Savings Goal Planner",
        ]
    )

    with tabs[0]:
        chat_agent_tab()
    with tabs[1]:
        budget_tab()
    with tabs[2]:
        expenses_tab()
    with tabs[3]:
        currency_tab()
    with tabs[4]:
        stocks_tab()
    with tabs[5]:
        savings_tab()


if __name__ == "__main__":
    main()
