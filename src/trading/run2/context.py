"""Point-in-time regime and structured-news features for shadow arms."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .config import RunConfig
from .ledger import RunLedger, utc_now


CBOE_VIX_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FRED_HY_OAS_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"
SEC_TICKERS_JSON = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class ContextError(RuntimeError):
    """Raised when a required research input cannot be captured faithfully."""


def _close(frame: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(frame["Close"], errors="coerce").dropna()


def _below_trailing_percentile(series: pd.Series, quantile: float, window: int = 252) -> bool:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 60:
        raise ContextError("At least 60 observations are required for a regime percentile")
    history = clean.iloc[-window - 1 : -1]
    if history.empty:
        raise ContextError("No prior observations for a point-in-time percentile")
    return bool(clean.iloc[-1] < history.quantile(quantile))


def _breadth(frames: Mapping[str, pd.DataFrame], symbols: tuple[str, ...], window: int = 50) -> float:
    flags = []
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None or len(frame) < window:
            continue
        close = _close(frame)
        flags.append(float(close.iloc[-1] > close.rolling(window).mean().iloc[-1]))
    if not flags:
        raise ContextError("No eligible symbols for breadth")
    return float(np.mean(flags))


def _median_crypto_correlation(
    frames: Mapping[str, pd.DataFrame], symbols: tuple[str, ...], window: int = 60
) -> pd.Series:
    returns = {}
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is not None and len(frame) >= window + 1:
            returns[symbol] = _close(frame).pct_change()
    panel = pd.DataFrame(returns).dropna(how="all")
    values: list[tuple[pd.Timestamp, float]] = []
    for end in range(window, len(panel) + 1):
        corr = panel.iloc[end - window : end].corr()
        upper = corr.where(np.triu(np.ones(corr.shape), 1).astype(bool)).stack()
        if not upper.empty:
            values.append((panel.index[end - 1], float(upper.median())))
    if not values:
        raise ContextError("Insufficient aligned crypto data for correlation regime")
    return pd.Series(dict(values)).sort_index()


def compute_regime_scores(
    cfg: RunConfig,
    frames: Mapping[str, pd.DataFrame],
    vix: pd.Series,
    high_yield_oas: pd.Series,
) -> dict[str, dict[str, Any]]:
    """Compute the two pre-registered four-component regime scores."""
    spy = frames.get("SPY")
    btc = frames.get("BTC/USD")
    if spy is None or len(spy) < 200 or btc is None or len(btc) < 252:
        raise ContextError("SPY and BTC/USD require sufficient history for regime scoring")

    spy_close = _close(spy)
    btc_close = _close(btc)
    equity_flags = {
        "spy_above_sma200": bool(spy_close.iloc[-1] > spy_close.rolling(200).mean().iloc[-1]),
        "watchlist_breadth": _breadth(frames, cfg.stock_symbols) >= 0.5,
        "vix_not_extreme": _below_trailing_percentile(vix, cfg.research.vix_percentile),
        "credit_not_extreme": _below_trailing_percentile(
            high_yield_oas, cfg.research.volatility_percentile
        ),
    }
    btc_vol = btc_close.pct_change().rolling(30).std() * np.sqrt(365)
    corr = _median_crypto_correlation(frames, cfg.crypto_symbols, cfg.portfolio.correlation_window)
    crypto_flags = {
        "btc_above_sma200": bool(btc_close.iloc[-1] > btc_close.rolling(200).mean().iloc[-1]),
        "watchlist_breadth": _breadth(frames, cfg.crypto_symbols) >= 0.5,
        "btc_vol_not_extreme": _below_trailing_percentile(
            btc_vol, cfg.research.volatility_percentile
        ),
        "correlation_not_extreme": _below_trailing_percentile(
            corr, cfg.research.volatility_percentile
        ),
    }
    return {
        "equity": {"value": sum(equity_flags.values()), "flags": equity_flags},
        "crypto": {"value": sum(crypto_flags.values()), "flags": crypto_flags},
    }


def fetch_vix_history() -> pd.Series:
    df = pd.read_csv(CBOE_VIX_CSV)
    date_col = next(c for c in df.columns if c.strip().upper() == "DATE")
    close_col = next(c for c in df.columns if c.strip().upper() == "CLOSE")
    out = pd.Series(
        pd.to_numeric(df[close_col], errors="coerce").values,
        index=pd.to_datetime(df[date_col]),
        name="VIX",
    )
    return out.dropna().sort_index()


def fetch_high_yield_oas() -> pd.Series:
    df = pd.read_csv(FRED_HY_OAS_CSV)
    value_col = next(c for c in df.columns if c != "observation_date")
    out = pd.Series(
        pd.to_numeric(df[value_col], errors="coerce").values,
        index=pd.to_datetime(df["observation_date"]),
        name="high_yield_oas",
    )
    return out.dropna().sort_index()


def robust_z(current: float, history: pd.Series) -> float:
    clean = pd.to_numeric(history, errors="coerce").dropna()
    if clean.empty:
        raise ContextError("Cannot calculate news z-score without history")
    median = float(clean.median())
    mad = float((clean - median).abs().median())
    if mad == 0:
        return 0.0 if current == median else (10.0 if current > median else -10.0)
    return (current - median) / (1.4826 * mad)


def normalize_headline(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", value.lower())).strip()


class FinBertScorer:
    """Lazy pinned FinBERT wrapper so ordinary paper commands stay lightweight."""

    def __init__(self, model: str, revision: str):
        try:
            from transformers import pipeline
        except ImportError as exc:  # pragma: no cover - optional production dependency
            raise ContextError(
                "FinBERT is not installed; run `pip install -e '.[research]'` before collecting news"
            ) from exc
        self.pipe = pipeline(
            "text-classification",
            model=model,
            revision=revision,
            top_k=None,
            truncation=True,
        )

    def __call__(self, text: str) -> dict[str, float]:
        result = self.pipe(text[:4000])
        # transformers has returned both list[dict] and list[list[dict]] across
        # releases for top_k=None. Normalize without weakening the pinned model.
        while isinstance(result, list) and len(result) == 1 and isinstance(result[0], list):
            result = result[0]
        if isinstance(result, dict):
            result = [result]
        return {str(item["label"]).lower(): float(item["score"]) for item in result}


class ContextCollector:
    def __init__(self, cfg: RunConfig, ledger: RunLedger, raw_dir: str | Path):
        self.cfg = cfg
        self.ledger = ledger
        self.raw_dir = Path(raw_dir)

    def _persist(
        self,
        *,
        scope: str,
        name: str,
        value: Any,
        source: str,
        observed_at: str,
        symbol: str | None = None,
        published_at: str | None = None,
        raw: Any | None = None,
    ) -> None:
        payload = json.dumps(raw if raw is not None else value, default=str, sort_keys=True)
        digest = sha256(payload.encode()).hexdigest()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.raw_dir / source.replace("/", "_") / f"{digest}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            raw_path.write_text(payload)
        self.ledger.record_feature(
            {
                "run_id": self.cfg.run_id,
                "scope": scope,
                "symbol": symbol,
                "observed_at": observed_at,
                "published_at": published_at,
                "captured_at": utc_now(),
                "source": source,
                "name": name,
                "value_json": json.dumps(value, sort_keys=True),
                "payload_hash": digest,
                "raw_path": str(raw_path),
            }
        )

    def collect_regimes(
        self,
        frames: Mapping[str, pd.DataFrame],
        vix: pd.Series | None = None,
        high_yield_oas: pd.Series | None = None,
    ) -> dict[str, dict[str, Any]]:
        vix_series = vix if vix is not None else fetch_vix_history()
        oas_series = high_yield_oas if high_yield_oas is not None else fetch_high_yield_oas()
        scores = compute_regime_scores(self.cfg, frames, vix_series, oas_series)
        for name, series, source in (
            ("vix_history", vix_series, "cboe:vix-history"),
            ("high_yield_oas_history", oas_series, "fred:BAMLH0A0HYM2"),
        ):
            clean = pd.to_numeric(series, errors="coerce").dropna().sort_index().tail(400)
            observed_at = pd.Timestamp(clean.index[-1]).isoformat()
            raw = [
                {"date": pd.Timestamp(index).isoformat(), "value": float(value)}
                for index, value in clean.items()
            ]
            self._persist(
                scope="macro",
                name=name,
                value={"value": float(clean.iloc[-1]), "observations": len(clean)},
                source=source,
                observed_at=observed_at,
                raw=raw,
            )
        for scope, value in scores.items():
            observed = (
                _close(frames["SPY"]).index[-1]
                if scope == "equity"
                else _close(frames["BTC/USD"]).index[-1]
            )
            self._persist(
                scope=scope,
                name="regime_score",
                value=value,
                source="derived:cboe+fred+alpaca",
                observed_at=pd.Timestamp(observed).isoformat(),
                raw=value,
            )
        return scores

    def collect_news(
        self,
        articles: list[dict[str, Any]],
        scorer: Callable[[str], dict[str, float]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, float]:
        """Store raw articles and prospectively update per-symbol negative load."""
        now = now or datetime.now(timezone.utc)
        scorer = scorer or FinBertScorer(
            self.cfg.research.model, self.cfg.research.model_revision
        )
        by_symbol: dict[str, float] = {s: 0.0 for s, _ in self.cfg.symbols}
        seen: set[tuple[str, str]] = set()
        symbol_index = {s.replace("/", ""): s for s, _ in self.cfg.symbols}
        for article in articles:
            headline = str(article.get("headline") or "")
            url = str(article.get("url") or "")
            key = (url, normalize_headline(headline))
            if key in seen:
                continue
            seen.add(key)
            text = f"{headline}. {article.get('summary') or ''}".strip()
            scores = scorer(text)
            negative = float(scores.get("negative", 0.0))
            published = str(article.get("created_at") or now.isoformat())
            for raw_symbol in article.get("symbols") or []:
                symbol = symbol_index.get(str(raw_symbol).replace("/", ""))
                if not symbol:
                    continue
                by_symbol[symbol] += negative
                self._persist(
                    scope="news",
                    name="article_sentiment",
                    symbol=symbol,
                    value={"negative": negative, "scores": scores},
                    source="alpaca-benzinga",
                    observed_at=published,
                    published_at=published,
                    raw=article,
                )

        day = pd.Timestamp(now).floor("D").isoformat()
        for symbol, load in by_symbol.items():
            self._persist(
                scope="news",
                name="negative_load",
                symbol=symbol,
                value={"value": load},
                source="derived:finbert",
                observed_at=day,
                raw={"negative_load": load, "captured_at": now.isoformat()},
            )
            rows = list(
                self.ledger.conn.execute(
                    """SELECT observed_at, value_json FROM feature_snapshots
                       WHERE run_id=? AND scope='news' AND symbol=? AND name='negative_load'
                       ORDER BY observed_at DESC, captured_at DESC""",
                    (self.cfg.run_id, symbol),
                )
            )
            daily: dict[str, float] = {}
            for row in rows:
                daily.setdefault(
                    str(row["observed_at"]), float(json.loads(row["value_json"])["value"])
                )
                if len(daily) >= self.cfg.research.news_lookback_days + 1:
                    break
            values = list(reversed(list(daily.values())))
            z = 0.0
            if len(values) > self.cfg.research.news_min_observations:
                z = robust_z(values[-1], pd.Series(values[:-1]))
            self._persist(
                scope="news",
                name="negative_news_z",
                symbol=symbol,
                value={"value": z, "observations": max(0, len(values) - 1)},
                source="derived:finbert",
                observed_at=day,
            )
        return by_symbol

    def collect_sec_filings(self, filings: list[dict[str, Any]]) -> dict[str, int]:
        """Persist primary-source filing events without turning them into trades."""
        counts = {symbol: 0 for symbol in self.cfg.stock_symbols}
        for filing in filings:
            symbol = str(filing["symbol"])
            if symbol not in counts:
                continue
            counts[symbol] += 1
            self._persist(
                scope="sec",
                name="filing",
                symbol=symbol,
                value={
                    "form": filing["form"],
                    "accession_number": filing["accession_number"],
                    "url": filing["url"],
                },
                source="sec:edgar-submissions",
                observed_at=str(filing["accepted_at"]),
                published_at=str(filing["accepted_at"]),
                raw=filing,
            )
        observed = datetime.now(timezone.utc).isoformat()
        for symbol, count in counts.items():
            self._persist(
                scope="sec",
                name="filing_count",
                symbol=symbol,
                value={"value": count, "lookback_hours": self.cfg.research.sec_lookback_hours},
                source="derived:sec-edgar",
                observed_at=observed,
            )
        return counts


def _sec_json(url: str, user_agent: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 — fixed official HTTPS URLs
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip

            body = gzip.decompress(body)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ContextError(f"SEC returned malformed JSON from {url}")
    return value


def fetch_sec_filings(
    cfg: RunConfig,
    start: datetime | None = None,
    user_agent: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent allowlisted filings from the official EDGAR submissions API."""
    user_agent = (user_agent or os.getenv("SEC_USER_AGENT") or "").strip()
    if not user_agent or "@" not in user_agent:
        raise ContextError(
            "SEC_USER_AGENT must identify you with a contact email, e.g. "
            "'money research name@example.com'"
        )
    start = start or datetime.now(timezone.utc) - timedelta(
        hours=cfg.research.sec_lookback_hours
    )
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    ticker_payload = _sec_json(SEC_TICKERS_JSON, user_agent)
    cik_by_ticker = {
        str(row["ticker"]).upper(): int(row["cik_str"])
        for row in ticker_payload.values()
        if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None
    }
    filings: list[dict[str, Any]] = []
    allowed_forms = set(cfg.research.sec_forms)
    for symbol in cfg.stock_symbols:
        cik = cik_by_ticker.get(symbol.upper())
        if cik is None:
            continue
        time.sleep(0.12)  # stay comfortably inside SEC's 10-request/second fair-access cap
        payload = _sec_json(SEC_SUBMISSIONS.format(cik=cik), user_agent)
        recent = payload.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        for index, accession in enumerate(accessions):
            try:
                form = str(recent["form"][index])
                accepted_at = pd.Timestamp(recent["acceptanceDateTime"][index])
                if accepted_at.tzinfo is None:
                    accepted_at = accepted_at.tz_localize("America/New_York")
                accepted_at = accepted_at.tz_convert("UTC")
                if accepted_at.to_pydatetime() < start:
                    continue
                if form not in allowed_forms:
                    continue
                accession_compact = str(accession).replace("-", "")
                document = str(recent["primaryDocument"][index])
                filings.append(
                    {
                        "symbol": symbol,
                        "cik": cik,
                        "company": payload.get("name"),
                        "form": form,
                        "accession_number": str(accession),
                        "accepted_at": accepted_at.isoformat(),
                        "filing_date": str(recent["filingDate"][index]),
                        "report_date": str(recent["reportDate"][index]),
                        "primary_document": document,
                        "url": (
                            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                            f"{accession_compact}/{document}"
                        ),
                    }
                )
            except (IndexError, KeyError, TypeError, ValueError):
                continue
    return sorted(filings, key=lambda row: (row["accepted_at"], row["symbol"]))


def fetch_alpaca_news(cfg: RunConfig, start: datetime | None = None) -> list[dict[str, Any]]:
    """Fetch current Benzinga-backed Alpaca news using existing paper keys."""
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError as exc:  # pragma: no cover
        raise ContextError("alpaca-py news client is unavailable") from exc
    client = NewsClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"), raw_data=True)
    start = start or datetime.now(timezone.utc) - timedelta(hours=24)
    symbols = ",".join(s.replace("/", "") for s, _ in cfg.symbols)
    articles: list[dict[str, Any]] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        result = client.get_news(
            NewsRequest(
                start=start,
                symbols=symbols,
                limit=50,
                include_content=False,
                sort="ASC",
                page_token=page_token,
            )
        )
        if not isinstance(result, dict):
            break
        articles.extend(dict(item) for item in result.get("news", []))
        next_token = result.get("next_page_token")
        if not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(str(next_token))
        page_token = str(next_token)
    return articles
