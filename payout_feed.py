"""
Live payout feed.

Watches the CASHED payout wallets - BNB Smart Chain (USDC), Ethereum (ETH + USDC)
and Solana (SOL + USDC + USDT) - and posts each outgoing transfer to a Discord channel as a card.

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
SKIP_ADDRESSES = {a.strip().lower() for a in os.getenv("SKIP_ADDRESSES", "").split(",") if a.strip()}

# Solana payout wallet (Solana addresses are case-sensitive - don't lowercase)
SOLANA_WALLET = os.getenv("SOLANA_WALLET", "AgP3yC6cGup9XCaMihzJU9FQsHFW6ZznZ9noPpg8kz8n").strip()
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOL_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY8N1KxQLWPRnWdM": "USDT",
}
SOL_DELAY = float(os.getenv("SOLANA_DELAY", "0.3"))  # pause between lookups (public Solana RPC is rate-limited)

RANGE_LIMIT_RE = re.compile(r"less than (\d+)")

# Card colour + label by amount (highest first)
TIERS = [
    (10_000, 0xF5B700, "RECORD PAYOUT"),
    (5_000,  0xA855F7, "MAJOR PAYOUT"),
    (1_000,  0x3B82F6, "BIG PAYOUT"),
    (0,      0x22C55E, "PAYOUT SENT"),
]
DIVIDER = "━" * 30  # makes every card the same, wider width


def tier_for(usd):
    for minimum, colour, label in TIERS:
        if (usd or 0) >= minimum:
            return colour, label
    return TIERS[-1][1], TIERS[-1][2]

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
NETWORKS = {c: {"explorer": v["explorer"], "name": v["name"]} for c, v in CHAINS.items()}
NETWORKS["sol"] = {"explorer": "https://solscan.io/tx/", "name": "Solana"}

STABLES = {"USDC", "USDT", "BSC-USD"}
TX_LINK_RE = re.compile(
    r"(bscscan\.com|etherscan\.io|solscan\.io)/tx/(0x[0-9a-fA-F]{64}|[1-9A-HJ-NP-Za-km-z]{64,90})")


USD_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _first_usd(text: str):
    m = USD_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


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
        self.ledger: dict[str, tuple[float, int]] = {}  # tx hash -> (usd, unix time)
        self.ledger_ready = False
        self.sol_last_sig: str | None = None   # newest Solana signature already handled
        self.sol_start_ts: int = 0
        self.sol_tx_version = 1                      # Solana transaction format (auto-upgrades)
        self.sol_parsed: dict[str, list[dict]] = {}  # signature -> payouts found (cache for retries)

    @property
    def enabled(self) -> bool:
        return bool(NODEREAL_API_KEY and FEED_CHANNEL_ID)

    # ------------------------------------------------------------------ RPC
    async def rpc(self, chain: str, method: str, params: list):
        url = SOLANA_RPC_URL if chain == "sol" else CHAINS[chain]["rpc"].format(key=NODEREAL_API_KEY)
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
        return await self.price_usd("ETHUSDT", ts)

    async def price_usd(self, symbol: str, ts: int) -> float | None:
        """Coin price in USD at the minute of the payout."""
        minute = ts // 60 * 60
        if (symbol, minute) in self.price_cache:
            return self.price_cache[(symbol, minute)]
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1m", "startTime": minute * 1000, "limit": 1}
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                rows = await r.json(content_type=None)
            price = float(rows[0][4])
        except Exception:
            log.warning("Couldn't get %s price for %s", symbol, minute, exc_info=True)
            return None
        self.price_cache[(symbol, minute)] = price
        return price

    async def usd_value(self, p: dict) -> float | None:
        if p["asset"] in STABLES:
            return p["amount"]
        if p["asset"] in ("ETH", "SOL"):
            price = await self.price_usd(f"{p['asset']}USDT", p["ts"])
            return p["amount"] * price if price else None
        return None

    # --------------------------------------------------------------- posting
    async def build_card(self, p: dict) -> discord.Embed | None:
        """One payout as a wide, clean text card. Colour and label depend on the amount."""
        usd = await self.usd_value(p)
        if usd is not None:
            if usd < MIN_PAYOUT_USD:
                return None
            amount = f"${usd:,.2f}"
        else:
            amount = f"{p['amount']:,.4f} {p['asset']}"
        cfg = NETWORKS[p["chain"]]
        link = cfg["explorer"] + p["hash"]
        colour, label = tier_for(usd)

        embed = discord.Embed(
            description=(f"# {amount}\n"
                         f"Another trader just got **Cashed Up!**\n"
                         f"{DIVIDER}"),
            colour=colour,
            timestamp=datetime.fromtimestamp(p["ts"], tz=timezone.utc),
        )
        avatar = self.client.user.display_avatar.url if self.client.user else None
        # tx link hidden behind the label (no hash shown) - also lets the bot remember what it posted
        embed.set_author(name=label, icon_url=avatar, url=link)
        embed.add_field(name="Asset", value=f"`{p['asset']}`", inline=True)
        embed.add_field(name="Network", value=f"`{cfg['name']}`", inline=True)
        embed.set_footer(text="Cashed  ·  Verified on-chain")
        return embed

    async def post(self, payouts: list[dict]):
        channel = self.client.get_channel(FEED_CHANNEL_ID) or await self.client.fetch_channel(FEED_CHANNEL_ID)
        payouts.sort(key=lambda p: (p["ts"], p["block"]))
        batch: list[discord.Embed] = []
        for p in payouts:
            key = p["hash"].lower()
            if key in self.seen:
                continue
            card = await self.build_card(p)
            self.seen.add(key)
            if not card:
                continue
            usd = await self.usd_value(p)
            if usd is not None:
                self.ledger[key] = (usd, p["ts"])
            batch.append(card)
            if len(batch) == 10:  # Discord allows 10 cards per message
                await channel.send(embeds=batch)
                await asyncio.sleep(1)
                batch = []
        if batch:
            await channel.send(embeds=batch)

    # ------------------------------------------------------------- lifecycle
    async def load_state(self) -> datetime | None:
        """Read all our posts in the feed channel: remember tx hashes (no double posts)
        and each payout's amount (for /lifetime). Returns when we last posted."""
        channel = self.client.get_channel(FEED_CHANNEL_ID) or await self.client.fetch_channel(FEED_CHANNEL_ID)
        last_post = None
        async for msg in channel.history(limit=None):
            if msg.author.id != self.client.user.id:
                continue
            found = False
            # current style: one payout per card
            for e in msg.embeds:
                text = (f"{e.url or ''} {getattr(e.author, 'url', None) or ''} {e.description or ''} "
                        + " ".join(f.value or "" for f in e.fields))
                hashes = [h.lower() for _, h in TX_LINK_RE.findall(text)]
                if not hashes:
                    continue
                found = True
                self.seen.update(hashes)
                usd = _first_usd(e.description or "")
                if usd is not None:
                    ts = int((e.timestamp or msg.created_at).timestamp())
                    self.ledger[hashes[0]] = (usd, ts)
            # older plain-text style: one payout per line
            for line in msg.content.splitlines():
                hashes = [h.lower() for _, h in TX_LINK_RE.findall(line)]
                if not hashes:
                    continue
                found = True
                self.seen.update(hashes)
                usd = _first_usd(line)
                if usd is not None:
                    self.ledger[hashes[0]] = (usd, int(msg.created_at.timestamp()))
            if found:
                last_post = last_post or msg.created_at
        self.ledger_ready = True
        log.info("Payout feed: %d payouts on record, $%s lifetime", len(self.ledger),
                 f"{sum(v for v, _ in self.ledger.values()):,.2f}")
        return last_post

    def lifetime(self) -> dict:
        """Totals for /lifetime."""
        values = list(self.ledger.values())
        now = datetime.now(timezone.utc).timestamp()
        recent = [v for v, ts in values if ts >= now - 30 * 86400]
        return {
            "total": sum(v for v, _ in values),
            "count": len(values),
            "largest": max((v for v, _ in values), default=0.0),
            "last30": sum(recent),
            "since": min((ts for _, ts in values), default=None),
        }

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
                    # Solana: always scan its full history the first time (it may be newer than
                    # the channel's posts); already-posted payouts are skipped via their links.
                    self.sol_start_ts = int(datetime.fromisoformat(HISTORY_START)
                                            .replace(tzinfo=timezone.utc).timestamp())
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

        if SOLANA_WALLET:
            try:
                items, newest = await self.sol_scan()
                results["sol"] = (items, newest, None)
            except Exception:
                log.exception("Payout feed: sol scan failed; will retry next round")

        found = [p for items, _, _ in results.values() for p in items]
        if found:
            log.info("Payout feed: %d transfer(s) found", len(found))
            try:
                await self.post(found)
            except Exception:
                log.exception("Payout feed: posting to Discord failed; will retry")
                return  # don't move cursors, so these get retried (already-posted ones are skipped)

        for chain, (_, safe, nonce) in results.items():
            if chain == "sol":
                if safe:
                    self.sol_last_sig = safe
                continue
            self.cursor[chain] = safe + 1
            nonces[chain] = nonce

    # ---------------------------------------------------------------- Solana
    async def sol_scan(self) -> tuple[list[dict], str | None]:
        """New outgoing USDC/USDT transfers from the Solana wallet since the last scan.
        First scan goes back to the start time. Returns (payouts, newest signature seen)."""
        sigs, before = [], None
        while True:
            opts = {"limit": 1000, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            if self.sol_last_sig:
                opts["until"] = self.sol_last_sig
            page = await self.rpc("sol", "getSignaturesForAddress", [SOLANA_WALLET, opts])
            if not page:
                break
            stop = False
            for s in page:  # newest first
                if not self.sol_last_sig and (s.get("blockTime") or 0) < self.sol_start_ts:
                    stop = True
                    break
                sigs.append(s)
            if stop or len(page) < 1000:
                break
            before = page[-1]["signature"]
            await asyncio.sleep(SOL_DELAY)

        newest = sigs[0]["signature"] if sigs else None
        if not self.sol_last_sig:
            log.info("Payout feed: Solana first scan, %d transaction(s) to check (this can take a few minutes)",
                     len(sigs))
        out = []
        for n, s in enumerate(reversed(sigs), 1):  # oldest first
            sig_id = s["signature"]
            if s.get("err") is not None or sig_id.lower() in self.seen:
                continue
            if sig_id not in self.sol_parsed:  # remember results so a retry doesn't redo work
                tx = await self.sol_get_tx(sig_id)
                await asyncio.sleep(SOL_DELAY)
                self.sol_parsed[sig_id] = self._sol_parse(s, tx)
            out += self.sol_parsed[sig_id]
            if not self.sol_last_sig and n % 200 == 0:
                log.info("Payout feed: Solana first scan %d/%d checked, %d payout(s) found so far",
                         n, len(sigs), len(out))
        return out, newest

    async def sol_get_tx(self, sig_id: str):
        """Fetch one Solana transaction, upgrading the supported transaction version if the network asks."""
        while True:
            try:
                return await self.rpc("sol", "getTransaction", [sig_id, {
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": self.sol_tx_version,
                    "commitment": "confirmed"}])
            except RuntimeError as e:
                m = re.search(r'maxSupportedTransactionVersion"?\s*:\s*(\d+)', str(e))
                if m and int(m.group(1)) > self.sol_tx_version:
                    self.sol_tx_version = int(m.group(1))
                    log.info("Payout feed: Solana now using transaction version %d", self.sol_tx_version)
                    continue
                raise

    def _sol_parse(self, sig: dict, tx: dict | None) -> list[dict]:
        if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
            return []
        meta = tx["meta"]

        def balances(rows):
            b = {}
            for r in rows or []:
                mint, owner = r.get("mint"), r.get("owner")
                if mint in SOL_MINTS and owner:
                    amt = float((r.get("uiTokenAmount") or {}).get("uiAmountString") or 0)
                    b[(owner, mint)] = b.get((owner, mint), 0.0) + amt
            return b

        pre, post = balances(meta.get("preTokenBalances")), balances(meta.get("postTokenBalances"))
        out = []
        for mint, asset in SOL_MINTS.items():
            sent = pre.get((SOLANA_WALLET, mint), 0.0) - post.get((SOLANA_WALLET, mint), 0.0)
            if sent <= 0:
                continue
            receivers = [o for (o, m) in set(pre) | set(post)
                         if m == mint and o != SOLANA_WALLET
                         and post.get((o, m), 0.0) - pre.get((o, m), 0.0) > 0]
            if any(r.lower() in SKIP_ADDRESSES for r in receivers):
                continue
            out.append({
                "chain": "sol",
                "hash": sig["signature"],
                "block": sig.get("slot") or tx.get("slot") or 0,
                "ts": sig.get("blockTime") or tx.get("blockTime") or 0,
                "asset": asset,
                "amount": round(sent, 6),
            })
        if out:
            return out  # token payout found; any SOL change here is just fees/rent

        # Native SOL payout: the wallet's SOL went down by more than the network fee
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
        if SOLANA_WALLET not in keys:
            return []
        i = keys.index(SOLANA_WALLET)
        pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i >= len(pre_b) or i >= len(post_b):
            return []
        fee = (meta.get("fee") or 0) if i == 0 else 0  # fee payer is always account 0
        lamports = pre_b[i] - post_b[i] - fee
        if lamports <= 0:
            return []
        receivers = [keys[j] for j in range(min(len(keys), len(pre_b), len(post_b)))
                     if j != i and post_b[j] > pre_b[j]]
        if not receivers or any(r.lower() in SKIP_ADDRESSES for r in receivers):
            return []
        return [{
            "chain": "sol",
            "hash": sig["signature"],
            "block": sig.get("slot") or tx.get("slot") or 0,
            "ts": sig.get("blockTime") or tx.get("blockTime") or 0,
            "asset": "SOL",
            "amount": round(lamports / 1e9, 6),
        }]
