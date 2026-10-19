"""Streamlit interface; calculations and persistence live in separate modules."""
from datetime import date
import time

import pandas as pd
import streamlit as st

from .agent import run_question
from .api import ServiceError, convert_currency, fetch_stock_price
from .config import load_settings
from .database import FinanceRepository, export_csv
from .dashboard import overview_totals, search_transactions
from .demo import DemoWorkspace, DemoRequestLimit
from .finance import (budget_plan, category_breakdown, goal_status, monthly_summary,
                      selected_transactions, transactions_frame)
from .retrieval import KnowledgeSearch
from .storage import DataError


def show_summary_charts(summary):
    for code in sorted(summary.currency.unique()):
        values = summary[summary.currency == code]
        st.write(f"Income and expenses ({code})")
        st.bar_chart(values.set_index("month")[["income", "expenses", "net"]], stack=False)
        latest = values.iloc[-1]
        st.caption(f"50/30/20 reference for {latest['month']} ({code})")
        st.json(budget_plan(latest["income"]))


def add_transaction_form(repository):
    with st.expander("Add Transaction", expanded=True):
        with st.form("add_transaction", clear_on_submit=False):
            tx_date = st.date_input("Date", value=date.today())
            merchant = st.text_input("Merchant", value="", max_chars=200)
            description = st.text_input("Description", value="", max_chars=1000)
            tx_type = st.selectbox("Type", ["Expense", "Income"])
            amount = st.number_input("Amount", min_value=0.0, max_value=1e12, value=100.0, step=1.0)
            currency = st.text_input("Currency", value="EGP", max_chars=3)
            allow_duplicate = st.checkbox("This is a separate purchase even if it matches an existing transaction")
            submit = st.form_submit_button("Save Transaction")
        if submit:
            try:
                repository.add_transaction({
                    "date": tx_date.isoformat(), "merchant": merchant,
                    "description": description, "currency": currency,
                    "amount": -amount if tx_type == "Expense" else amount,
                }, allow_duplicate=allow_duplicate)
                st.session_state["notice"] = "Transaction saved."
                st.rerun()
            except (DataError, OSError) as exc:
                st.error(str(exc))


def currency_form(settings):
    with st.expander("Currency Converter"):
        with st.form("currency_converter"):
            amount = st.number_input("Amount to convert", min_value=0.0, max_value=1e12, value=100.0)
            from_code = st.text_input("From", value="USD", max_chars=3)
            to_code = st.text_input("To", value="EGP", max_chars=3)
            submit = st.form_submit_button("Convert")
        if submit:
            st.session_state.pop("fx_result", None)
            try:
                with st.spinner("Checking exchange rate..."):
                    result = convert_currency(amount, from_code, to_code, settings.fx_path)
                st.session_state["fx_result"] = {"amount": amount, "from": from_code.upper(), "to": to_code.upper(), "result": result}
            except (DataError, ServiceError, OSError) as exc:
                st.error(str(exc))
        if "fx_result" in st.session_state:
            saved = st.session_state["fx_result"]
            result = saved["result"]
            st.success(f"{saved['amount']:,.2f} {saved['from']} = {result['converted']:,.2f} {saved['to']}")
            st.caption(f"Last requested result. Source: {result['source']}. Rate: {result['rate']:.6g}. {result['note']}")


def stock_form():
    with st.expander("Stock Price Fetcher"):
        with st.form("stock_price"):
            ticker = st.text_input("Ticker", value="AAPL", max_chars=20)
            submit = st.form_submit_button("Fetch")
        if submit:
            st.session_state.pop("stock_result", None)
            try:
                with st.spinner("Fetching price..."):
                    result = fetch_stock_price(ticker)
                st.session_state["stock_result"] = result
            except (DataError, ServiceError) as exc:
                st.error(str(exc))
        if "stock_result" in st.session_state:
            result = st.session_state["stock_result"]
            unit = result.get("currency", "quoted currency")
            if unit in {"GBp", "GBX"}:
                unit += " (pence)"
            st.success(f"{result['ticker']}: latest daily close {result['close']:,.2f} {unit}")
            st.caption(f"Source: {result.get('source', 'Yahoo Finance')}. Market date: {result['market_date']}. Retrieved: {result.get('retrieved_at', 'previous request')}. Completed trading session; not a real-time quote.")


def goals_form(repository, goals):
    with st.expander("Savings Goal Tracker"):
        if goals:
            st.dataframe(pd.DataFrame([goal_status(goal) for goal in goals]), hide_index=True)
        by_id = {goal["id"]: goal for goal in goals}
        options = [""] + list(by_id)
        previous = st.session_state.get("managed_goal", "")
        selection = st.selectbox("Goal to manage", options, key="managed_goal",
                                 index=options.index(previous) if previous in options else 0,
                                 format_func=lambda value: "Add a new goal" if not value else by_id[value]["name"])
        first = by_id[selection] if selection else {
            "name": "Emergency Fund", "currency": "EGP", "target_amount": 30000,
            "current_amount": 0, "deadline": ""}
        with st.form(f"goal_{selection or 'new'}"):
            name = st.text_input("Goal name", value=first["name"], max_chars=200)
            currency = st.text_input("Goal currency", value=first["currency"], max_chars=3)
            target = st.number_input("Target amount", min_value=0.01, max_value=1e12, value=float(first["target_amount"]))
            current = st.number_input("Current amount", min_value=0.0, max_value=1e12, value=float(first["current_amount"]))
            deadline = st.text_input("Deadline (YYYY-MM-DD, optional)", value=first.get("deadline", ""))
            submit = st.form_submit_button("Save goal")
        if submit:
            try:
                updated = {**first, "name": name, "currency": currency, "target_amount": target,
                           "current_amount": current, "deadline": deadline.strip()}
                if selection:
                    repository.update_goal(selection, updated)
                else:
                    repository.add_goal(updated)
                st.session_state["notice"] = "Goal saved."
                st.rerun()
            except (DataError, OSError) as exc:
                st.error(str(exc))
        if selection:
            confirmed = st.checkbox("Delete selected goal permanently", key=f"delete_goal_confirm_{selection}")
            if st.button("Delete goal", disabled=not confirmed):
                try:
                    repository.delete_goal(selection)
                    st.session_state["notice"] = "Goal deleted."
                    st.rerun()
                except DataError as exc:
                    st.error(str(exc))


def manage_transactions(repository, filtered, originals):
    with st.expander("Edit / Delete Transactions"):
        if filtered.empty:
            st.info("No transactions match the current filter.")
            return
        rows = filtered.sort_values("date", ascending=False).to_dict("records")
        by_id = {row["id"]: row for row in rows}
        options = list(by_id)
        previous = st.session_state.get("managed_transaction")
        selection = st.selectbox("Transaction to manage", options, key="managed_transaction",
                                 index=options.index(previous) if previous in options else 0, format_func=lambda value:
                                 f"{by_id[value]['date']} | {by_id[value]['merchant']} | {by_id[value]['amount']:.2f} {by_id[value]['currency']} | {value[-6:]}")
        row = by_id[selection]
        with st.form(f"edit_transaction_{selection}"):
            tx_date = st.date_input("Edit date", value=date.fromisoformat(row["date"]))
            merchant = st.text_input("Edit merchant", value=row["merchant"], max_chars=200)
            description = st.text_input("Edit description", value=row["description"], max_chars=1000)
            amount = st.number_input("Signed amount (expense negative)", min_value=-1e12, max_value=1e12, value=float(row["amount"]))
            currency = st.text_input("Edit currency", value=row["currency"], max_chars=3)
            # An empty override keeps rule-based categorization active after edits.
            original = originals.get(selection)
            if original is None:
                st.info("This transaction was removed. Refresh the page to update the list.")
                return
            category = st.text_input("Category override (optional)", value=str(original.get("category") or ""), max_chars=100)
            submit = st.form_submit_button("Save changes")
        if submit:
            try:
                repository.update_transaction(selection, {"date": tx_date.isoformat(), "merchant": merchant,
                    "description": description, "amount": amount, "currency": currency, "category": category.strip()})
                st.session_state["notice"] = "Transaction updated."
                st.rerun()
            except DataError as exc:
                st.error(str(exc))
        confirmed = st.checkbox("Delete selected transaction permanently", key=f"delete_tx_confirm_{selection}")
        if st.button("Delete transaction", disabled=not confirmed):
            try:
                repository.delete_transaction(selection)
                st.session_state["notice"] = "Transaction deleted."
                st.rerun()
            except DataError as exc:
                st.error(str(exc))


def csv_tools(repository, filtered, all_rows):
    with st.expander("Import / Export CSV"):
        st.caption("Required columns: date, amount, currency. Optional: id, merchant, description, category. Expenses are negative. UTF-8; up to 2 MB and 10,000 rows.")
        st.download_button("Export filtered transactions", data=export_csv(filtered.to_dict("records")),
                           file_name="transactions.csv", mime="text/csv", key="export_filtered")
        st.download_button("Export all transactions", data=export_csv(all_rows),
                           file_name="all_transactions.csv", mime="text/csv", key="export_all")
        uploaded = st.file_uploader("Transactions CSV", type=["csv"], key="transactions_csv")
        skip_matching = st.checkbox("Skip matching transactions", value=True)
        st.caption("Matches use date, amount, currency, merchant and description. Uncheck to keep identical purchases with different IDs. Existing IDs are never overwritten.")
        if uploaded is not None:
            try:
                content = uploaded.getvalue()
                preview = repository.preview_csv(content, skip_matching)
                st.info(f"{preview['new']} new transactions; {preview['duplicates']} duplicates will be skipped.")
                st.dataframe(pd.DataFrame(preview["rows"]).head(50), hide_index=True)
                if st.button("Import transactions", disabled=preview["new"] == 0):
                    result = repository.import_csv(content, skip_matching)
                    st.session_state["notice"] = f"Imported {result['imported']} transactions; skipped {result['duplicates']} duplicates."
                    st.rerun()
            except DataError as exc:
                st.error(str(exc))



def render_agent_response(response, details=True):
    if response["answer"]:
        if response["clarification"]:
            st.info(response["answer"])
        else:
            st.markdown(response["answer"])
    if response["error"]:
        st.error(response["error"])
    for note in response["notes"]:
        if "OPENROUTER_API_KEY" in note:
            st.info(note)
        else:
            st.caption(note)
    for warning in response["warnings"]:
        st.warning(warning)
    for table in response["tables"] if details else []:
        if table["rows"]:
            st.write(table["label"])
            st.dataframe(pd.DataFrame(table["rows"]), use_container_width=True, hide_index=True)
            if table["label"] == "Monthly totals":
                with st.expander("Income, expenses and budget reference"):
                    show_summary_charts(pd.DataFrame(table["rows"]))
    if response["sources"]:
        with st.expander("Knowledge sources"):
            for hit in response["sources"]:
                st.write(hit["id"])
                st.caption(hit["text"])



def session_services(settings):
    """Reuse lightweight services per session, without caching financial records."""
    identity = None
    if settings.db_path.exists():
        stat = settings.db_path.stat()
        identity = (stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", 0))
    key = (str(settings.db_path), str(settings.transactions_path), str(settings.goals_path))
    cached = st.session_state.get("repository_service")
    migrated = False
    if cached is None or cached[0] != key or cached[1] != identity:
        repository = FinanceRepository(settings.db_path)
        migrated = repository.migrate_json(settings.transactions_path, settings.goals_path)
        stat = settings.db_path.stat()
        identity = (stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", 0))
        st.session_state["repository_service"] = (key, identity, repository)
    else:
        repository = cached[2]
    search_key = (str(settings.knowledge_dir), str(settings.store_dir), settings.embedding_backend,
                  settings.embedding_model, settings.allow_model_download)
    if st.session_state.get("search_service_key") != search_key:
        st.session_state["search_service"] = KnowledgeSearch(*search_key)
        st.session_state["search_service_key"] = search_key
    return repository, st.session_state["search_service"], migrated


def overview_page(filtered, goals):
    st.subheader("Overview")
    if filtered.empty:
        st.info("No transactions match this period and currency. Add a transaction or adjust the filters.")
    else:
        for total in overview_totals(filtered):
            st.write(f"**{total['currency']}** · {total['count']} transactions")
            income, expenses, net, rate = st.columns(4)
            income.metric("Income", f"{total['income']:,.2f}", help=f"Total income in {total['currency']}.")
            expenses.metric("Expenses", f"{total['expenses']:,.2f}", help=f"Total expenses in {total['currency']}.")
            net.metric("Net balance", f"{total['net']:,.2f}", help=f"Income minus expenses in {total['currency']}.")
            rate.metric("Savings rate", "—" if total["savings_rate"] is None else f"{total['savings_rate']:.1f}%",
                        help="Net balance divided by income. Unavailable when there is no income.")
        st.divider()
        codes = sorted(filtered.currency.unique())
        chart_code = st.selectbox("Chart currency", codes)
        chart_data = filtered[filtered.currency == chart_code]
        trend, categories = st.columns([1.4, 1])
        with trend:
            st.write(f"**Income and spending · {chart_code}**")
            summary = monthly_summary(chart_data)
            st.bar_chart(summary.set_index("month")[["income", "expenses", "net"]], stack=False)
        with categories:
            st.write(f"**Expense categories · {chart_code}**")
            breakdown = category_breakdown(chart_data)
            if breakdown.empty:
                st.caption("No expenses in this selection.")
            else:
                st.bar_chart(breakdown.set_index("category")[["total"]], horizontal=True)
        with st.expander("Monthly totals and budget reference"):
            st.dataframe(monthly_summary(filtered), use_container_width=True, hide_index=True)
            latest = summary.iloc[-1]
            st.caption(f"50/30/20 reference based on income for {latest['month']} ({chart_code})")
            st.json(budget_plan(latest["income"]))
    st.divider()
    st.subheader("Savings goals")
    if not goals:
        st.info("Create your first savings goal in the Goals section.")
    for goal in goals[:6]:
        status = goal_status(goal)
        st.write(f"**{status['name']}** · {status['current']:,.2f} / {status['target']:,.2f} {status['currency']}")
        st.progress(min(1.0, status["progress_pct"] / 100))
        st.caption(f"{status['progress_pct']:.1f}% saved · {status['remaining']:,.2f} {status['currency']} remaining")
    if len(goals) > 6:
        st.caption(f"Showing 6 of {len(goals)} goals. Manage all goals in the Goals section.")


@st.cache_resource(show_spinner=False)
def shared_demo_ai_limit():
    return DemoRequestLimit()


def permit_demo_ai(shared=False):
    now = time.monotonic()
    budget = st.session_state.setdefault("demo_ai_budget", {"count": 0, "last": None})
    if budget["count"] >= 5:
        return False, "This demo session has used its five AI requests. Finance tools remain available."
    if budget["last"] is not None and now - budget["last"] < 15:
        return False, "Please wait 15 seconds between AI requests."
    if shared and not shared_demo_ai_limit().acquire(now):
        return False, "The shared demo AI allowance has been used for this 24-hour window. Finance tools remain available."
    budget.update(count=budget["count"] + 1, last=now)
    return True, ""


def assistant_page(settings, search, df, goals, month, currency, valid_filter, demo=False, shared_ai=False):
    st.subheader("Ask the agent")
    st.caption("Review your finances, check goal progress, or use the currency and stock tools.")
    question = st.text_area("Question", value="Summarize my month and give me a plan to improve my spending.", max_chars=4000, height=110)
    with st.expander("Examples and privacy"):
        st.write("Convert 100 USD to EGP · Stock price AAPL · Summarize 2026-01 in EGP · Show goal progress")
        st.caption("AI advice sends your question, recent conversation, calculated totals, relevant goal progress and knowledge to OpenRouter. Transaction rows stay local. Conversation is kept only in this browser session (last 10 turns).")
    scope = (month, currency)
    if st.session_state.get("chat_scope") != scope:
        st.session_state["chat_context"] = None
        st.session_state["chat_scope"] = scope
    history = st.session_state.setdefault("chat_history", [])
    run_col, clear_col = st.columns([1, 4])
    run = run_col.button("Run", disabled=not valid_filter, type="primary", use_container_width=True)
    if clear_col.button("Clear conversation"):
        st.session_state["chat_history"] = []
        st.session_state["chat_context"] = None
        history = st.session_state["chat_history"]
    if run:
        if not question.strip():
            st.warning("Enter a question first.")
        else:
            with st.spinner("Preparing your answer..."):
                response = run_question(settings, search, question, df, goals, month, currency,
                                        history=list(history), previous=st.session_state.get("chat_context"),
                                        ai_gate=(lambda: permit_demo_ai(shared_ai)) if demo else None)
            if demo:
                response["notes"] = ["Enter your OpenRouter key under Demo AI in the sidebar to enable AI advice. Local tools work without a key."
                                     if "Set OPENROUTER_API_KEY" in note else note for note in response["notes"]]
            history.append({"question": question.strip(), "answer": response["answer"], "response": response})
            del history[:-10]
            st.session_state["chat_context"] = response["context"]
    old_details = st.checkbox("Show tables and charts for older answers", value=False) if len(history) > 1 else False
    for index, turn in enumerate(history):
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            render_agent_response(turn["response"], details=old_details or index == len(history) - 1)
    if history:
        st.caption("Previous answers are snapshots. Ask again to refresh after changing data or filters.")


def transactions_page(repository, transactions, filtered, valid_filter, scope):
    st.subheader("Transactions")
    st.caption("Add a transaction, search your records, or import a CSV. Reports use the complete selection.")
    add_transaction_form(repository)
    query = st.text_input("Search transactions", placeholder="Merchant, description or category", max_chars=200)
    matches = search_transactions(filtered, query).sort_values(["date", "id"], ascending=[False, True])
    token = (scope, query.strip())
    if st.session_state.get("transaction_page_scope") != token:
        st.session_state["transaction_page"] = 1
        st.session_state["transaction_page_scope"] = token
    pages = max(1, (len(matches) + 24) // 25)
    if st.session_state.get("transaction_page", 1) > pages:
        st.session_state["transaction_page"] = pages
    page = st.number_input("Page", min_value=1, max_value=pages, step=1, key="transaction_page")
    start = (page - 1) * 25
    visible = matches.iloc[start:start + 25]
    if matches.empty:
        st.info("No transactions match the current filters and search.")
    else:
        st.caption(f"Showing {start + 1}–{min(start + 25, len(matches))} of {len(matches)} matches · Page {page} of {pages}")
        st.dataframe(visible, use_container_width=True, hide_index=True)
    if valid_filter:
        manage_transactions(repository, visible, {row["id"]: row for row in transactions})
        csv_tools(repository, matches, transactions)


def reset_demo_workspace():
    workspace = st.session_state.pop("demo_workspace", None)
    if workspace is not None:
        workspace.close()
    for key in ("repository_service", "search_service", "search_service_key", "chat_history", "chat_context",
                "chat_scope", "transaction_page", "transaction_page_scope", "fx_result", "stock_result",
                "managed_transaction", "managed_goal", "notice"):
        st.session_state.pop(key, None)


def main(demo=False):
    st.set_page_config(page_title="Personal Finance Agent", page_icon="💰", layout="wide")
    st.title("Personal Finance Agent")
    st.caption("Track your money. Understand your spending. Plan your next step.")
    notice_slot = st.empty()
    migration_slot = st.empty()
    try:
        if demo:
            st.info("Public demo · Fictional sample data. Your edits belong to this browser session and reset when the session or server restarts. Export CSV to keep a copy.")
            with st.sidebar:
                st.caption("Your demo workspace")
                st.button("Reset demo data", on_click=reset_demo_workspace)
                with st.expander("Demo AI"):
                    visitor_key = st.text_input("Your OpenRouter API key (optional)", type="password", key="demo_visitor_key",
                                                help="Used only in this session. Never saved to the finance database or source code.")
                    st.caption("AI advice sends your question, calculated totals and recent chat to OpenRouter. Use fictional data here.")
            try:
                host_secrets = st.secrets.to_dict()
            except FileNotFoundError:
                host_secrets = {}
            settings = load_settings(host_secrets=host_secrets)
            if "demo_workspace" not in st.session_state:
                st.session_state["demo_workspace"] = DemoWorkspace()
            settings = st.session_state["demo_workspace"].settings(settings, visitor_key)
            shared_ai = settings.demo_shared_ai and not visitor_key.strip()
            if shared_ai:
                st.sidebar.caption("Shared demo AI: up to 5 requests per session, 15 seconds apart; 20 attempts per rolling 24 hours across this running app.")
        else:
            settings = load_settings()
        repository, search, migrated = session_services(settings)
        transactions, goals = repository.snapshot()
        df = transactions_frame(transactions)
    except (DataError, OSError, ValueError) as exc:
        st.error(str(exc))
        return
    if "notice" in st.session_state:
        notice_slot.success(st.session_state.pop("notice"))
    if migrated and not demo:
        migration_slot.success("Existing transactions and goals are ready. Original JSON files were kept as a backup.")
    with st.sidebar:
        st.header("Workspace")
        section = st.radio("Section", ["Overview", "Assistant", "Transactions", "Goals", "Tools"], key="section")
        st.divider()
        st.subheader("Filters")
        month = st.text_input("Month (YYYY-MM, blank for all)", value="").strip() or None
        currencies = sorted(df.currency.unique().tolist())
        selected = st.selectbox("Currency filter", ["All currencies (separate totals)"] + currencies)
        currency = None if selected.startswith("All currencies") else selected
        st.button("Refresh data", help="Reload saved transactions and goals.")
        st.caption(f"{len(transactions)} transactions · {len(goals)} goals")
        st.divider()
        st.write(f"AI answers: {'Configured' if settings.api_key else 'API key needed'}")
        st.caption("Local finance tools work without an AI key.")
        with st.expander("Settings and knowledge"):
            st.caption(f"Model: {settings.model}")
            if st.button("Build / Rebuild RAG Index"):
                try:
                    with st.spinner("Preparing knowledge search..."):
                        result = search.rebuild()
                    st.success(f"Indexed {result['document_count']} documents.")
                    if result["warning"]:
                        st.warning(result["warning"])
                except (OSError, ValueError, RuntimeError) as exc:
                    st.error(f"Knowledge search could not be prepared: {exc}")
            if not demo:
                st.text(f"Database: {settings.db_path}\nKnowledge: {settings.knowledge_dir}")
    valid_filter = True
    try:
        filtered = selected_transactions(df, month, currency)
    except DataError as exc:
        st.error(str(exc))
        valid_filter = False
        filtered = df.iloc[0:0]
    st.caption(f"{month or 'All dates'} · {currency or 'All currencies, separate totals'} · {len(filtered)} matching transactions")
    if section == "Overview":
        overview_page(filtered, goals)
    elif section == "Assistant":
        assistant_page(settings, search, df, goals, month, currency, valid_filter, demo, shared_ai if demo else False)
    elif section == "Transactions":
        transactions_page(repository, transactions, filtered, valid_filter, (month, currency))
    elif section == "Goals":
        st.subheader("Goals")
        st.caption("Manage each savings goal independently of your transaction filters.")
        goals_form(repository, goals)
    else:
        st.subheader("Tools")
        st.caption("Convert currencies or check the latest daily stock close.")
        left, right = st.columns(2)
        with left:
            currency_form(settings)
        with right:
            stock_form()
