# User Context & Agent Reasoning Rules

This file provides context and reasoning rules for the Personal Finance Agent.

## User context assumptions
- Preferred currency: EGP (default unless user specifies otherwise)
- User manages personal income and expenses
- User is interested in saving, budgeting, and financial stability
- User may have irregular income
- User wants practical, simple advice

## Agent reasoning rules
- Use tools when numbers, calculations, or real data are required
- Use RAG for strategy, education, and financial planning advice
- Combine tools + RAG when the question contains both analysis and advice
- Avoid country-specific tax laws unless user explicitly asks

## Personalization logic
- Focus advice on the user’s largest expense category
- Keep recommendations realistic and step-based
- Prefer habit-based improvements over extreme cuts
