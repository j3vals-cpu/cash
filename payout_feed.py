"""
Live payout feed.

Watches the CASHED payout wallet on BNB Smart Chain (USDC) and Ethereum (ETH + USDC)
and posts each outgoing transfer to a Discord channel, e.g.

    ✅ Payout sent! **Trader** has just received **$812.62**. Congratulations! [🔗](<tx link>)

On first run it posts the whole history (oldest first, grouped into messages).
After that it polls for new payouts. The tx links double as memory: on restart
it reads its own recent messages so nothing is posted twice.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import aiohttp
import discord

log = logging.getLogger("payout-feed")

NODEREAL_API_KEY = os.getenv("NODEREAL_API_KEY", "")
FEED_CHANNEL_ID = int(os.getenv("FEED_CHANNEL_ID", "0") or 0)
PAYOUT_WALLET = os.getenv("PAYOUT_WALLET", "0x2695a6D9CB6AFf1b042F2dA4485f37427f9860d1").lower()
HISTORY_START = os.getenv("HISTORY_START", "2026-06-25")  # wallet was funded ~late June 2026
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MIN_PAYOUT_USD = float(os.getenv("MIN_PAYOUT_USD", "1"))
BIG_PAYOUT_USD = float(os.getenv("BIG_PAYOUT_USD", "2500"))  # gold card + 🔥 at or above this
SKIP_ADDRESSES = {a.strip().lower() for a in os.getenv("SKIP_ADDRESSES", "").split(",") if a.strip()}

RANGE_LIMIT_RE = re.compile(r"less than (\d+)")

CHAINS = {
    "bsc": {
        "rpc": "https://bsc-mainnet.nodereal.io/v1/{key}",
        "explorer": "https://bscscan.com/tx/",
        "name": "BNB Chain",
        "native": None,  # BNB sends are gas top-ups, not payouts
        "tokens": {"0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC"},
        "range": 50_000,  # blocks per lookup (auto-shrinks if NodeReal says it's too big)
        "lag": 60,        # stay this many blocks behind the newest so NodeReal has indexed them
    },
    "eth": {
        "rpc": "https://eth-mainnet.nodereal.io/v1/{key}",
        "explorer": "https://etherscan.io/tx/",
        "name": "Ethereum",
        "native": "ETH",
        "tokens": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC"},
        "range": 999,     # NodeReal limit on Ethereum: under 1000 blocks
        "lag": 5,
    },
}
STABLES = {"USDC", "USDT", "BSC-USD"}
TX_LINK_RE = re.compile(r"(bscscan\.com|etherscan\.io)/tx/(0x[0-9a-fA-F]{64})")


def _int(x) -> int:
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    x = str(x)
    return int(x, 16) if x.lower().startswith("0x") else int(x)


class PayoutFeed:
    def __init__(self, client: discord.Client):
        self.client = client
        self.session: aiohttp.ClientSession | None = None
        self.cursor: dict[str, int] = {}
        self.seen: set[str] = set()
        self.price_cache: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(NODEREAL_API_KEY and FEED_CHANNEL_ID)

    # ------------------------------------------------------------------ RPC
    async def rpc(self, chain: str, method: str, params: list):
        url = CHAINS[chain]["rpc"].format(key=NODEREAL_API_KEY)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(6):
            try:
                async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429:  # rate limited: slow down and retry
                        await asyncio.sleep(1 + attempt)
                        continue
                    data = await r.json(content_type=None)
                err = data.get("error")
                if err:
                    msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                    if "limit" in msg.lower() and "range" not in msg.lower() and attempt < 5:
                        await asyncio.sleep(1 + attempt)  # rate/compute limit
                        continue
                    raise RuntimeError(f"{chain} {method}: {err}")
                return data["result"]
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 5:
                    raise
                log.warning("RPC %s %s failed (%s), retrying", chain, method, e)
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{chain} {method}: still rate limited after retries")

    async def latest_block(self, chain: str) -> int:
        return _int(await self.rpc(chain, "eth_blockNumber", []))

    async def block_time(self, chain: str, number: int) -> int:
        block = await self.rpc(chain, "eth_getBlockByNumber", [hex(number), False])
        return _int(block["timestamp"])

    async def block_at(self, chain: str, ts: int) -> int:
        """First block at or after unix time ts (binary search)."""
        lo, hi = 1, await self.latest_block(chain)
        if await self.block_time(chain, hi) < ts:
            return hi
        while lo < hi:
            mid = (lo + hi) // 2
            if await self.block_time(chain, mid) < ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    async def transfers(self, chain: str, start: int, end: int) -> list[dict]:
        """Outgoing payout transfers from the wallet between blocks start..end (inclusive)."""
        cfg = CHAINS[chain]
        out = []
        b = start
        while b <= end:
            to = min(b + cfg["range"] - 1, end)
            categories = ["20"] + (["external"] if cfg["native"] else [])
            params = {
                "category": categories,
                "fromBlock": hex(b),
                "toBlock": hex(to),
                "fromAddress": PAYOUT_WALLET,
                "order": "asc",
                "maxCount": hex(1000),
            }
            chunk = []
            try:
                page_key = None
                while True:
                    if page_key:
                        params["pageKey"] = page_key
                    res = await self.rpc(chain, "nr_getAssetTransfers", [params])
                    for t in res.get("transfers") or []:
                        item = self._parse(chain, t)
                        if item:
                            chunk.append(item)
                    page_key = res.get("pageKey")
                    if not page_key:
                        break
            except RuntimeError as e:
                m = RANGE_LIMIT_RE.search(str(e))
                if m and "range" in str(e):
                    new_range = max(1, int(m.group(1)) - 1)
                    if new_range < cfg["range"]:
                        log.warning("%s: block range too big, shrinking %d -> %d", chain, cfg["range"], new_range)
                        cfg["range"] = new_range
                        continue  # retry this chunk with the smaller range
                raise
            out += chunk
            b = to + 1
        return out

    async def nonce_at(self, chain: str, block: int) -> int | None:
        """Number of transactions the wallet had sent as of a block (None if the node can't say)."""
        try:
            return _int(await self.rpc(chain, "eth_getTransactionCount", [PAYOUT_WALLET, hex(block)]))
        except Exception:
            return None

    def _parse(self, chain: str, t: dict) -> dict | None:
        cfg = CHAINS[chain]
        if (t.get("from") or "").lower() != PAYOUT_WALLET:
            return None
        to_addr = (t.get("to") or "").lower()
        if to_addr in SKIP_ADDRESSES or to_addr == PAYOUT_WALLET:
            return None
        if str(t.get("receiptsStatus", 1)) == "0":
            return None
        cat = str(t.get("category"))
        if cat == "external":
            asset, decimals = cfg["native"], 18
        elif cat in ("20", "erc20"):
            contract = (t.get("contractAddress") or "").lower()
            if contract not in cfg["tokens"]:
                return None
            asset = cfg["tokens"][contract]
            decimals = _int(t.get("decimal")) if t.get("decimal") not in (None, "") else (18 if chain == "bsc" else 6)
        else:
            return None
        amount = _int(t.get("value")) / (10 ** decimals)
        if amount <= 0:
            return None
        return {
            "chain": chain,
            "hash": t["hash"].lower(),
            "block": _int(t.get("blockNum")),
            "ts": _int(t.get("blockTimeStamp") or t.get("blockTimestamp")),
            "asset": asset,
            "amount": amount,
        }

    # ---------------------------------------------------------------- prices
    async def eth_usd(self, ts: int) -> float | None:
        minute = ts // 60 * 60
        if minute in self.price_cache:
            return self.price_cache[minute]
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": "ETHUSDT", "interval": "1m", "startTime": minute * 1000, "limit": 1}
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                rows = await r.json(content_type=None)
            price = float(rows[0][4])
        except Exception:
            log.warning("Couldn't get ETH price for %s", minute, exc_info=True)
            return None
        self.price_cache[minute] = price
        return price

    async def usd_value(self, p: dict) -> float | None:
        if p["asset"] in STABLES:
            return p["amount"]
        if p["asset"] == "ETH":
            price = await self.eth_usd(p["ts"])
            return p["amount"] * price if price else None
        return None

    # --------------------------------------------------------------- posting
    async def build_card(self, p: dict) -> discord.Embed | None:
        """One payout as a clean card. The title is the amount and links to the transaction."""
        usd = await self.usd_value(p)
        if usd is not None:
            if usd < MIN_PAYOUT_USD:
                return None
            amount = f"${usd:,.2f}"
        else:
            amount = f"{p['amount']:,.4f} {p['asset']}"
        big = usd is not None and usd >= BIG_PAYOUT_USD
        network = f"{p['asset']} · {CHAINS[p['chain']]['name']}"
        embed = discord.Embed(
            title=f"{'🔥' if big else '💸'}  {amount} paid out",
            url=CHAINS[p["chain"]]["explorer"] + p["hash"],
            description=f"A **Trader** just got paid. Congratulations! 🎉\n-# {network}",
            colour=0xF1C40F if big else 0x2ECC71,
            timestamp=datetime.fromtimestamp(p["ts"], tz=timezone.utc),
        )
        embed.set_footer(text="Verified on-chain · tap the amount to view")
        return embed

    async def post(self, payouts: list[dict]):
        channel = self.client.get_channel(FEED_CHANNEL_ID) or await self.client.fetch_channel(FEED_CHANNEL_ID)
        payouts.sort(key=lambda p: (p["ts"], p["block"]))
        batch: list[discord.Embed] = []
        for p in payouts:
            if p["hash"] in self.seen:
                continue
            card = await self.build_card(p)
            self.seen.add(p["hash"])
            if not card:
                continue
            batch.append(card)
            if len(batch) == 10:  # Discord allows 10 cards per message
                await channel.send(embeds=batch)
                await asyncio.sleep(1)
                batch = []
        if batch:
            await channel.send(embeds=batch)

    # ------------------------------------------------------------- lifecycle
    async def load_state(self) -> datetime | None:
        """Read our own recent posts: remember tx hashes and when we last posted."""
        channel = self.client.get_channel(FEED_CHANNEL_ID) or await self.client.fetch_channel(FEED_CHANNEL_ID)
        last_post = None
        async for msg in channel.history(limit=1000):
            if msg.author.id != self.client.user.id:
                continue
            text = msg.content + " " + " ".join(f"{e.url or ''} {e.description or ''}" for e in msg.embeds)
            hashes = [h.lower() for _, h in TX_LINK_RE.findall(text)]
            if hashes:
                self.seen.update(hashes)
                last_post = last_post or msg.created_at
        return last_post

    async def run(self):
        if not self.enabled:
            log.info("Payout feed off (set NODEREAL_API_KEY and FEED_CHANNEL_ID to turn it on)")
            return
        await self.client.wait_until_ready()
        self.session = aiohttp.ClientSession()
        try:
            while True:  # keep trying to start; a hiccup here shouldn't kill the feed
                try:
                    last_post = await self.load_state()
                    if last_post:
                        # Restart: rescan from 1 hour before our last post; seen hashes stop duplicates
                        start_ts = int(last_post.timestamp()) - 3600
                        log.info("Payout feed resuming from %s (%d payouts already posted)",
                                 last_post, len(self.seen))
                    else:
                        start_ts = int(datetime.fromisoformat(HISTORY_START)
                                       .replace(tzinfo=timezone.utc).timestamp())
                        log.info("Payout feed first run: posting history since %s (this can take a few minutes)",
                                 HISTORY_START)
                    for chain in CHAINS:
                        self.cursor[chain] = await self.block_at(chain, start_ts)
                    break
                except Exception:
                    log.exception("Payout feed couldn't start; retrying in 60s")
                    await asyncio.sleep(60)

            nonces: dict[str, int | None] = {c: None for c in CHAINS}
            while True:
                await self.poll(nonces)
                await asyncio.sleep(POLL_SECONDS)
        finally:
            await self.session.close()

    async def poll(self, nonces: dict):
        """One pass over every chain. A chain's cursor only moves forward after its payouts are posted,
        and a failure on one chain never throws away what another chain found."""
        results = {}  # chain -> (payouts, scanned_up_to, nonce_there)
        for chain, cfg in CHAINS.items():
            try:
                safe = await self.latest_block(chain) - cfg["lag"]
                if safe < self.cursor[chain]:
                    continue
                # Cheap check: if the wallet's sent-transaction count hasn't changed, nothing went out.
                nonce = await self.nonce_at(chain, safe)
                if nonce is not None and nonces[chain] is not None and nonce == nonces[chain]:
                    results[chain] = ([], safe, nonce)
                    continue
                items = await self.transfers(chain, self.cursor[chain], safe)
                results[chain] = (items, safe, nonce)
            except Exception:
                log.exception("Payout feed: %s scan failed; will retry next round", chain)

        found = [p for items, _, _ in results.values() for p in items]
        if found:
            log.info("Payout feed: %d transfer(s) found", len(found))
            try:
                await self.post(found)
            except Exception:
                log.exception("Payout feed: posting to Discord failed; will retry")
                return  # don't move cursors, so these get retried (already-posted ones are skipped)

        for chain, (_, safe, nonce) in results.items():
            self.cursor[chain] = safe + 1
            nonces[chain] = nonce
