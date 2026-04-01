# Market Intelligence Agent

A multi-agent market research system that combines live API data, AI specialist analysis, and lightweight forecasting to generate structured market-intelligence reports.

## Overview

The Market Intelligence Agent is designed to act like a small analyst team rather than a single chatbot. Given a product or category query, it gathers live market signals, routes them through specialist agents, and produces a presentation-ready report.

The system is built to support questions like:
- What is the current demand for this product category?
- What pricing signals exist in the market right now?
- Are there trade or tariff risks that could affect supply-side constraints?
- What recommendation should a user make based on current evidence?

## Current Architecture

### Backend
- Python
- FastAPI
- Async API orchestration
- Multi-agent analysis pipeline
- ARIMA-based Google Trends forecasting

### Frontend
- HTML
- CSS
- Vanilla JavaScript
- Live report rendering
- Downloadable HTML export

## Agent Workflow

The app follows a plan-act-synthesize workflow:

1. **Planner**
   - Selects which tools are needed for the query
   - Avoids always pulling every source by default

2. **Live Evidence Collection**
   - News coverage
   - Google Trends
   - eBay pricing
   - WTO trade/tariff context
   - Stored pricing history

3. **Specialist Agents**
   - **Industry Context Agent**
     - Uses web search to build a broader market context brief
     - Supplies citations and high-level category/brand context
   - **Demand Specialist**
     - Interprets demand momentum, seasonality, and consumer attention
   - **WTO Trade Evidence Agent**
     - Interprets tariff/trade context and likely supply-chain exposure
   - **Market Constraints Specialist**
     - Interprets pricing pressure, competition, and supply-side constraints

4. **Final Synthesis**
   - Reconciles the specialist views
   - Produces a combined recommendation and confidence summary

## Data Sources

### Live sources
- **NewsAPI**
  - recent news coverage and article volume
- **Google Trends / pytrends**
  - search interest and momentum
- **eBay pricing**
  - live sold/completed pricing snapshots
- **WTO trade/tariff data**
  - tariff context and partner inference
- **Stored local pricing history**
  - historical weekly pricing continuity

### AI models
- **OpenAI**
  - planner
  - Industry Context Agent
  - Demand Specialist
  - WTO Trade Evidence Agent
  - final synthesis
  - forecasting / scoring layers
- **Claude**
  - Market Constraints Specialist

## Forecasting

The system includes an ARIMA-based forecasting layer for Google Trends.

Current forecasting behavior:
- tests multiple ARIMA specifications
- compares holdout RMSE
- prefers models that:
  - reject nonstationarity
  - pass residual checks
- falls back gracefully if no ideal model exists

This helps avoid choosing a visually plausible but statistically weak forecast.

## Key Features

- Multi-agent market analysis
- Live API evidence collection
- Structured specialist outputs
- ARIMA robustness checks
- Downloadable HTML reports
- UI sections for:
  - workflow
  - specialist agents
  - signal scores
  - pricing
  - forecasting
  - supporting evidence

## Example Use Cases

- product demand validation
- pricing and resale monitoring
- trend analysis
- trade/tariff risk awareness
- market presentation support
- rapid market-intelligence reporting

## Project Goal

This project was built to explore how agentic AI can support market-intelligence workflows. Instead of just summarizing a prompt, the system coordinates live evidence gathering, specialist interpretation, and synthesis into a usable recommendation.

## Running Locally

### 1. Create and activate a virtual environment
```bash
python -m venv venv
