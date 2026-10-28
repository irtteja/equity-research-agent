import yfinance as yf


def get_financial_statements(ticker: str) -> dict:
    """Fetch income statement, balance sheet, and cash flow for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        result = {}

        income = stock.financials
        if not income.empty:
            result["income_statement"] = income.iloc[:, :4].to_dict()

        balance = stock.balance_sheet
        if not balance.empty:
            result["balance_sheet"] = balance.iloc[:, :4].to_dict()

        cashflow = stock.cashflow
        if not cashflow.empty:
            result["cash_flow"] = cashflow.iloc[:, :4].to_dict()

        if not result:
            return {"error": f"No financial data for {ticker}"}
        return {"ticker": ticker, **result}
    except Exception as e:
        return {"error": str(e)}


def get_key_ratios(ticker: str) -> dict:
    """Get key financial ratios and metrics."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "ticker": ticker,
            "valuation": {
                "pe_trailing": info.get("trailingPE"),
                "pe_forward": info.get("forwardPE"),
                "peg_ratio": info.get("pegRatio"),
                "price_to_book": info.get("priceToBook"),
                "price_to_sales": info.get("priceToSalesTrailing12Months"),
                "ev_to_ebitda": info.get("enterpriseToEbitda"),
                "ev_to_revenue": info.get("enterpriseToRevenue"),
            },
            "profitability": {
                "gross_margin": info.get("grossMargins"),
                "operating_margin": info.get("operatingMargins"),
                "profit_margin": info.get("profitMargins"),
                "roe": info.get("returnOnEquity"),
                "roa": info.get("returnOnAssets"),
            },
            "growth": {
                "revenue_growth": info.get("revenueGrowth"),
                "earnings_growth": info.get("earningsGrowth"),
                "earnings_quarterly_growth": info.get("earningsQuarterlyGrowth"),
            },
            "financial_health": {
                "current_ratio": info.get("currentRatio"),
                "debt_to_equity": info.get("debtToEquity"),
                "quick_ratio": info.get("quickRatio"),
                "total_cash": info.get("totalCash"),
                "total_debt": info.get("totalDebt"),
                "free_cashflow": info.get("freeCashflow"),
            },
        }
    except Exception as e:
        return {"error": str(e)}
