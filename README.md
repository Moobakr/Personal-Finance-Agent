# Personal Finance Agent

A Streamlit-based personal finance assistant that combines a local rule-based RAG knowledge base with financial tools for budgeting, expense tracking, currency conversion, stock lookup, and savings goal planning.

## Overview

Personal Finance Agent is an educational finance dashboard built with Python and Streamlit. It allows users to ask general finance questions, calculate monthly budgets, categorize spending from CSV files, convert currencies, check stock prices, and estimate how long it will take to reach a savings goal.

The app does not rely on external AI APIs for the chat assistant. Instead, it uses a simple local RAG-style knowledge base with keyword retrieval and cosine similarity.

## Features

- Streamlit web interface
- Local personal finance assistant
- Rule-based RAG knowledge base
- Budget calculator
- 50/30/20 budgeting reference
- Expense categorization from CSV files
- Example expense CSV included
- Currency converter using exchange-rate API data
- Stock price lookup using Yahoo Finance data
- Savings goal planner with monthly compounding
- Educational financial guidance disclaimer

## Tech Stack

- Python
- Streamlit
- Pandas
- NumPy
- Requests
- yfinance

## Project Structure

```text
personal-finance-agent/
├── app.py
├── rag_engine.py
├── tools.py
├── expenses_example.csv
├── requirements.txt
├── README.md
└── .gitignore
