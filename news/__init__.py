# FIX-4: Absolute imports kept (this is consumed by engine.py as "from news import ...")
from news.feed_aggregator import NewsFeedAggregator, SentimentResult, NewsItem
from news.sentiment_engine import SentimentEngine, HARD_BLOCK_KEYWORDS

__all__ = ["NewsFeedAggregator", "SentimentEngine", "SentimentResult",
           "NewsItem", "HARD_BLOCK_KEYWORDS"]
