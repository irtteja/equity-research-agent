import yfinance as yf


def run_dcf_valuation(ticker: str, growth_rate: float = 0.10, discount_rate: float = 0.12, terminal_growth: float = 0.03, years: int = 5) -> dict:
    """Run a simple DCF valuation using free cash flow projections."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        fcf = info.get("freeCashflow")
        shares = info.get("sharesOutstanding")
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")

        if not fcf or not shares:
            return {"error": f"Insufficient data for DCF on {ticker}. Need free cash flow and shares outstanding."}

        projected_fcfs = []
        cf = fcf
        for i in range(1, years + 1):
            cf = cf * (1 + growth_rate)
            pv = cf / ((1 + discount_rate) ** i)
            projected_fcfs.append({"year": i, "projected_fcf": round(cf), "present_value": round(pv)})

        terminal_value = (cf * (1 + terminal_growth)) / (discount_rate - terminal_growth)
        terminal_pv = terminal_value / ((1 + discount_rate) ** years)

        total_pv = sum(p["present_value"] for p in projected_fcfs) + terminal_pv
        intrinsic_value = total_pv / shares

        margin_of_safety = ((intrinsic_value - current_price) / intrinsic_value * 100) if intrinsic_value else None

        return {
            "ticker": ticker,
            "assumptions": {
                "base_fcf": fcf,
                "growth_rate": f"{growth_rate*100}%",
                "discount_rate": f"{discount_rate*100}%",
                "terminal_growth": f"{terminal_growth*100}%",
                "projection_years": years,
            },
            "projected_fcfs": projected_fcfs,
            "terminal_value": round(terminal_value),
            "total_present_value": round(total_pv),
            "intrinsic_value_per_share": round(intrinsic_value, 2),
            "current_price": current_price,
            "margin_of_safety_pct": round(margin_of_safety, 1) if margin_of_safety else None,
            "verdict": "undervalued" if margin_of_safety and margin_of_safety > 20 else ("overvalued" if margin_of_safety and margin_of_safety < -10 else "fairly valued"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_peer_comparison(ticker: str, peers: list = None) -> dict:
    """Compare a stock against its peers on key valuation and profitability metrics."""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        if not peers:
            peers = []
            sector = info.get("sector", "")
            # If no peers provided, try to get from yfinance recommendations
            try:
                recs = stock.recommendations_summary
                if recs is not None and not recs.empty:
                    pass
            except Exception:
                pass

        tickers_to_compare = [ticker] + (peers or [])
        comparison = []

        for t in tickers_to_compare:
            try:
                s = yf.Ticker(t)
                i = s.info
                comparison.append({
                    "ticker": t,
                    "name": i.get("shortName", t),
                    "market_cap": i.get("marketCap"),
                    "pe_trailing": i.get("trailingPE"),
                    "pe_forward": i.get("forwardPE"),
                    "price_to_book": i.get("priceToBook"),
                    "ev_to_ebitda": i.get("enterpriseToEbitda"),
                    "profit_margin": i.get("profitMargins"),
                    "roe": i.get("returnOnEquity"),
                    "revenue_growth": i.get("revenueGrowth"),
                    "debt_to_equity": i.get("debtToEquity"),
                })
            except Exception:
                continue

        return {"ticker": ticker, "comparison": comparison}
    except Exception as e:
        return {"error": str(e)}
