import yfinance as yf
from datetime import datetime


def get_news_sentiment(ticker: str) -> dict:
    """Fetch recent news headlines and analyst recommendations for a ticker."""
    try:
        stock = yf.Ticker(ticker)
        result = {"ticker": ticker}

        # News
        news = stock.news or []
        parsed_news = []
        for item in news[:15]:
            parsed_news.append({
                "title": item.get("title", ""),
                "publisher": item.get("publisher", ""),
                "published": datetime.fromtimestamp(item.get("providerPublishTime", 0)).strftime("%Y-%m-%d") if item.get("providerPublishTime") else "",
                "url": item.get("link", ""),
            })
        result["recent_news"] = parsed_news

        # Analyst recommendations
        try:
            recs = stock.recommendations
            if recs is not None and not recs.empty:
                recent_recs = recs.tail(10)
                result["analyst_recommendations"] = recent_recs.to_dict(orient="records")
        except Exception:
            pass

        # Upgrades/downgrades
        try:
            upgrades = stock.upgrades_downgrades
            if upgrades is not None and not upgrades.empty:
                result["upgrades_downgrades"] = upgrades.head(10).to_dict(orient="records")
        except Exception:
            pass

        return result
    except Exception as e:
        return {"error": str(e)}
