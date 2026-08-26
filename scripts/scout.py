#!/usr/bin/env python3
"""Daily screening: which Polymarket traders are both ACTIVE right now and
REALLY profitable, not just a one-big-lucky-bet-3-months-ago account.

Runs on the Germany VPS (already has confirmed, reliable direct Polymarket
access - see atlantis-polymarket-screening's own README for why). Pure
stdlib, no dependencies - consistent with polymarket_read_proxy.py on this
same box.

Criteria (all real data, no leaderboard-PNL-alone shortcuts):
  1. >= MIN_WEEKLY_TRADES trades in the last 7 days - filters out accounts
     whose big number came from one bet placed months ago that just
     resolved, with nothing since (exactly the pattern the owner flagged
     as showing up in polymarket-copy-platform's Discover page, which
     only orders by monthly PNL with no activity requirement at all).
  2. Real, positive realized profit (from /closed-positions, not a naive
     trade-flow sum - see polymarket-copy-platform's adapter.ts for the
     same "buying an open position looks like a loss" trap this avoids)
     in ALL THREE of the 7d/30d/60d windows - consistency, not a lucky
     streak in just one window.
  3. Final list is diversified across market categories (sports, esports,
     politics, finance/macro, entertainment, other) - capped per category
     so one vertical can't dominate the whole page.
"""
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

DATA_API = "https://data-api.polymarket.com"
USER_AGENT = "atlantis-polymarket-collector/0.1"  # Cloudflare bans Python's default UA outright - confirmed live
REQUEST_TIMEOUT = 20

POOL_LIMIT = 150          # candidates per leaderboard pull (PNL + VOLUME, deduped)
MIN_WEEKLY_TRADES = 10
CLOSED_POSITIONS_PAGES = 8  # 400 rows - matches polymarket-copy-platform's own budget
MAX_WORKERS = 6           # modest concurrency - avoid tripping Cloudflare on a burst
PER_CATEGORY_CAP = 5
TOTAL_SHOWCASE = 20

CATEGORY_TERMS = {
    "Sports": {
        "nba", "wnba", "nfl", "mlb", "nhl", "epl", "ucl", "soccer", "football",
        "basketball", "baseball", "hockey", "tennis", "atp", "wta", "ufc",
        "mma", "boxing", "golf", "formula 1", "f1", "nascar", "cricket",
        "rugby", "olympics", "world cup", "champions league", "premier league",
        "la liga", "serie a", "bundesliga",
    },
    "Esports": {
        "esports", "lol:", "league of legends", "counter-strike", "valorant",
        "dota 2", "overwatch", "rainbow six", "call of duty", "rocket league",
        "apex legends",
    },
    "Politics": {
        "election", "president", "senate", "governor", "congress", "primary",
        "poll", "impeach", "shutdown", "nominee", "prime minister", "parliament",
    },
    "Finance/Macro": {
        "fed ", "interest rate", "rate cut", "rate hike", "inflation", "cpi",
        "gdp", "recession", "market cap", "stock", "nasdaq", "s&p", "ipo",
        "earnings",
    },
    "Crypto": {
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "dogecoin",
        "crypto", "up or down", "5m", "hourly",
    },
}


def http_get(path: str, params: dict) -> list:
    query = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items() if v is not None)
    url = f"{DATA_API}{path}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read())


def http_get_retry(path: str, params: dict, attempts: int = 2) -> list:
    last_err = None
    for i in range(attempts):
        try:
            return http_get(path, params)
        except Exception as e:  # noqa: BLE001 - genuinely want to retry on anything transient
            last_err = e
            time.sleep(0.5)
    raise last_err


def get_leaderboard(time_period: str, order_by: str, limit: int) -> list[dict]:
    return http_get_retry("/v1/leaderboard", {"category": "OVERALL", "timePeriod": time_period, "orderBy": order_by, "limit": limit})


def get_user_trades(wallet: str, limit: int = 500, offset: int = 0) -> list[dict]:
    return http_get_retry("/trades", {"user": wallet, "limit": limit, "offset": offset})


def get_closed_positions_paged(wallet: str, max_pages: int = CLOSED_POSITIONS_PAGES) -> list[dict]:
    all_rows: list[dict] = []
    for page in range(max_pages):
        batch = http_get_retry("/closed-positions", {"user": wallet, "limit": 50, "offset": page * 50})
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < 50:
            break
    return all_rows


def classify_category(titles: list[str]) -> str:
    haystack = " ".join(t.lower() for t in titles)
    for name, terms in CATEGORY_TERMS.items():
        if any(term in haystack for term in terms):
            return name
    return "Other"


def realized_window(closed_positions: list[dict], days: int) -> dict:
    cutoff = time.time() - days * 86400
    pnl = 0.0
    bought = 0.0
    count = 0
    for row in closed_positions:
        ts = float(row.get("timestamp") or 0)
        if ts < cutoff:
            continue
        pnl += float(row.get("realizedPnl") or 0)
        bought += float(row.get("totalBought") or 0)
        count += 1
    roi_pct = (pnl / bought * 100) if bought > 0 and count > 0 else None
    return {"realizedPnl": round(pnl), "roiPct": round(roi_pct, 1) if roi_pct is not None else None, "count": count}


def screen_wallet(wallet: str, username: str) -> dict | None:
    trades = get_user_trades(wallet, limit=500)
    now = time.time()
    trades_7d = [t for t in trades if float(t.get("timestamp") or 0) >= now - 7 * 86400]
    if len(trades_7d) < MIN_WEEKLY_TRADES:
        return None

    closed = get_closed_positions_paged(wallet)
    d7 = realized_window(closed, 7)
    d30 = realized_window(closed, 30)
    d60 = realized_window(closed, 60)
    if not all(w["roiPct"] is not None and w["roiPct"] > 0 for w in (d7, d30, d60)):
        return None

    titles = [str(t.get("title") or "") for t in trades[:80]]
    category = classify_category(titles)
    consistency_score = min(d7["roiPct"], d30["roiPct"], d60["roiPct"])

    return {
        "wallet": wallet,
        "username": username or f"{wallet[:6]}...{wallet[-4:]}",
        "category": category,
        "trades_7d": len(trades_7d),
        "d7": d7,
        "d30": d30,
        "d60": d60,
        "consistency_score": consistency_score,
    }


def gather_candidates() -> dict[str, str]:
    candidates: dict[str, str] = {}
    for period in ("MONTH",):
        for order_by in ("PNL", "VOLUME"):
            for row in get_leaderboard(period, order_by, POOL_LIMIT):
                wallet = str(row.get("proxyWallet") or "").lower()
                if wallet:
                    candidates.setdefault(wallet, str(row.get("userName") or ""))
    return candidates


def build_showcase(qualified: list[dict]) -> list[dict]:
    qualified.sort(key=lambda t: t["consistency_score"], reverse=True)
    per_category: dict[str, int] = {}
    showcase = []
    for trader in qualified:
        cat = trader["category"]
        if per_category.get(cat, 0) >= PER_CATEGORY_CAP:
            continue
        per_category[cat] = per_category.get(cat, 0) + 1
        showcase.append(trader)
        if len(showcase) >= TOTAL_SHOWCASE:
            break
    return showcase


def render_html(showcase: list[dict], pool_size: int, qualified_count: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = "\n".join(
        f"""
        <tr>
          <td class="mono">{t['username']}<br><span class="addr">{t['wallet'][:6]}...{t['wallet'][-4:]}</span></td>
          <td><span class="badge">{t['category']}</span></td>
          <td class="mono num">{t['trades_7d']}</td>
          <td class="mono num pos">+{t['d7']['roiPct']}%</td>
          <td class="mono num pos">+{t['d30']['roiPct']}%</td>
          <td class="mono num pos">+{t['d60']['roiPct']}%</td>
        </tr>"""
        for t in showcase
    )
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trader Scout — Replication Market</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1100px; margin: 0 auto; padding: 32px 20px 80px; background: #fafbfc; color: #131829; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #0a0c14; color: #f4f7fa; }} }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .sub {{ color: #6b7280; font-size: 0.9rem; margin-bottom: 24px; }}
  .stats {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .stat {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 16px; }}
  @media (prefers-color-scheme: dark) {{ .stat {{ border-color: #232a3d; }} }}
  .stat .n {{ font-size: 1.3rem; font-weight: 600; font-family: ui-monospace, monospace; }}
  .stat .l {{ font-size: 0.75rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.03em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 10px 12px; border-bottom: 2px solid #e5e7eb; font-size: 0.75rem; text-transform: uppercase; color: #6b7280; letter-spacing: 0.03em; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #eef0f2; }}
  @media (prefers-color-scheme: dark) {{ th {{ border-color: #232a3d; }} td {{ border-color: #1a2030; }} }}
  .mono {{ font-family: ui-monospace, "SF Mono", monospace; }}
  .addr {{ color: #6b7280; font-size: 0.75rem; }}
  .num {{ text-align: right; }}
  .pos {{ color: #16a34a; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef2ff; color: #4338ca; font-size: 0.75rem; }}
  @media (prefers-color-scheme: dark) {{ .badge {{ background: #1e2547; color: #a5b4fc; }} }}
  .note {{ margin-top: 24px; font-size: 0.8rem; color: #6b7280; line-height: 1.5; }}
  .empty {{ padding: 40px; text-align: center; color: #6b7280; }}
</style>
</head>
<body>
  <h1>Trader Scout</h1>
  <p class="sub">Traders reales y activos de Polymarket - actualizado {now}</p>

  <div class="stats">
    <div class="stat"><div class="n">{pool_size}</div><div class="l">candidatos evaluados</div></div>
    <div class="stat"><div class="n">{qualified_count}</div><div class="l">activos + rentables (7/30/60d)</div></div>
    <div class="stat"><div class="n">{len(showcase)}</div><div class="l">mostrados (diversificado)</div></div>
  </div>

  {"<table><thead><tr><th>Trader</th><th>Categoría</th><th class='num'>Trades 7d</th><th class='num'>ROI 7d</th><th class='num'>ROI 30d</th><th class='num'>ROI 60d</th></tr></thead><tbody>" + rows + "</tbody></table>" if showcase else '<div class="empty">Ningún trader cumplió los criterios hoy.</div>'}

  <p class="note">
    Criterios: mínimo {MIN_WEEKLY_TRADES} operaciones en los últimos 7 días (actividad real, no una apuesta grande
    de hace meses que recién resolvió) + profit real (de posiciones cerradas, no una suma ingenua de flujo de
    trades) positivo en 7, 30 <strong>y</strong> 60 días a la vez + lista diversificada por categoría (máx.
    {PER_CATEGORY_CAP} por rama). No es asesoría de inversión, el rendimiento pasado no garantiza resultados
    futuros.
  </p>
</body>
</html>
"""


def main() -> None:
    candidates = gather_candidates()
    print(f"{len(candidates)} unique candidate wallets")

    qualified: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(screen_wallet, wallet, username): wallet for wallet, username in candidates.items()}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:  # noqa: BLE001 - one wallet's failure shouldn't kill the run
                print(f"  wallet {futures[future]} failed: {e}")
                continue
            if result:
                qualified.append(result)

    print(f"{len(qualified)} qualified (active + profitable in all 3 windows)")
    showcase = build_showcase(qualified)
    html = render_html(showcase, len(candidates), len(qualified))

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("wrote docs/index.html")


if __name__ == "__main__":
    main()
