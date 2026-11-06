"""Keep payout complaints ("my payout has been pending 6 hours") out of general chat.

Uses a Discord AutoMod rule, so the message is blocked before anyone sees it and only the sender gets a private
notice telling them to open a payout ticket. Happy messages ("my payout just got paid") are let through.
The rule only applies in the channels you switch it on for (/fud on); every other channel is exempt.
"""
import json
import logging
import os
import re

import discord

log = logging.getLogger("ticket-bot")

RULE_NAME = "Cashed Bot: payout complaints in general"
DEFAULT_TICKET_CHANNEL = 1565738108698492978

# Spelling tricks are caught too: "pay out", "pay-out", "pay0ut", "p@yout", "pend1ng", "w8ing" etc.
# Payout words: payout, payment, withdrawal, money, funds, cash out, profits, earnings, payday
_PAY = r"p[a@4]y[ -]?[o0]u[tr]s?|p[a@4]ym[e3]nts?|w[i1]thdr[a@4]w[a-z]*|m[o0]n[e3]y|funds|cash ?outs?|profits?|earnings|payday"
_W = r"(?:" + _PAY + r")"
_PEND = r"p[e3]nd[i1]n[g']?"
_WAIT = r"(?:w[a@4][i1]t[i1]n[g']?|w8[i1]n[g']?)"
_N = r"(?:[0-9]+|a few|few|several|many|multiple) *(?:hours?|hrs?|days?|weeks?)"
# Rust-flavoured regex (AutoMod): no lookarounds, max 10 patterns of 260 chars each. Unicode classes (., \w, \d, \s)
# blow past Discord's compiled-size limit, so only small ASCII classes are used.
PATTERNS = [
    # "payout ... pending / delayed / stuck / taking so long / been 48 hours"
    r"(?i)\b" + _W + r"\b[ -~]{0,40}\b(?:" + _PEND + r"|delay[a-z]*|stuck|late|overdue|taking (?:so )?long|been [0-9]+|been (?:ages|forever))\b",
    # "payout ... still pending / still waiting / still nothing"
    r"(?i)\b" + _W + r"\b[ -~]{0,40}\bstill (?:" + _PEND + r"|" + _WAIT + r"|not|no|nothing|hasn'?t|haven'?t)\b",
    # "payout ... not paid / not received / never came"
    r"(?i)\b" + _W + r"\b[ -~]{0,40}\b(?:not (?:been )?(?:paid|received|sent|processed|arrived)|never (?:came|arrived|received|paid))\b",
    # "when will my payout come", "where's payouts", "when do i get my money"
    r"(?i)\b(?:when|where)(?:'s|s)?(?: (?:is|are|r|will|do|am)(?: i| we)?(?: get| getting| receive)?)? +(?:(?:my|the|our) +(?:[a-z0-9]+ +)?)?" + _W + r"\b",
    # "no response on my payout ticket", "payout where", "payout when??"
    r"(?i)\bno (?:reply|response|answer|update|one)\b[ -~]{0,30}\b(?:" + _PAY + r"|ticket)\b|\bp[a@4]y[ -]?[o0]u[tr]s? +(?:where|wen|when)\b",
    # "waiting ... for my payout", "pending payout", "still waiting to get paid"
    r"(?i)\b(?:" + _WAIT + r"|" + _PEND + r")\b[ -~]{0,40}\b(?:" + _PAY + r"|get paid|be paid)\b",
    # "haven't received my payout", "still not paid"
    r"(?i)\b(?:hasn'?t|haven'?t|not|never|didn'?t) +(?:yet +)?(?:been +)?(?:got|get|receive[ds]?|paid|arrived|came)\b[ -~]{0,30}\b(?:" + _PAY + r"|yet|paid)\b",
    # "6 hours and still pending", "3 days no payout", "haven't been paid"
    r"(?i)\b" + _N + r"\b[ -~]{0,30}\b(?:" + _PEND + r"|" + _WAIT + r"|still|no re(?:ply|sponse)|nothing|no p[a@4]y[a-z0 ]*)\b|\b(?:hasn'?t|haven'?t|not) +(?:yet +)?been paid\b",
    # "pending for 6 hours", "when am i getting paid", "why haven't i been paid"
    r"(?i)\b(?:" + _PEND + r"|" + _WAIT + r") +(?:for +)?(?:over +|more than +)?" + _N + r"\b|\b(?:when|why)\b[ -~]{0,20}\b(?:get|getting|be|being|been|not) paid\b",
    # FUD: "cashed is a scam", "they won't pay"
    r"(?i)\b(?:cashed|they|firm|you guys|this)\b[ -~]{0,30}\b(?:sc[a@4]m[a-z]*|rug[a-z]*|fraud[a-z]*|won'?t pay|wont pay|don'?t pay|doesn'?t pay|never pays?|not paying|refus[a-z]* to pay|steal[a-z]*)\b",
]
_COMPILED = [re.compile(p) for p in PATTERNS]


def would_block(text: str) -> bool:
    return any(p.search(text or "") for p in _COMPILED)


class FilterStore:
    def __init__(self, data_dir: str):
        self.file = os.path.join(data_dir, "general_filter.json")
        self.guilds: dict[str, dict] = {}
        try:
            with open(self.file, encoding="utf-8") as f:
                self.guilds = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Couldn't read general filter settings")

    def get(self, guild_id: int) -> dict:
        g = self.guilds.setdefault(str(guild_id), {})
        g.setdefault("channels", [])
        g.setdefault("tag_channels", list(g["channels"]))  # older setups had both on together
        g.setdefault("ticket_channel", DEFAULT_TICKET_CHANNEL)
        return g

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.guilds, f)
            os.replace(tmp, self.file)
        except Exception:
            log.exception("Couldn't save general filter settings")


def notice_text(bot: discord.Client, cfg: dict) -> str:
    ch = bot.get_channel(int(cfg.get("ticket_channel") or 0))
    # <#id> shows as a clickable channel link where Discord renders it
    where = f"<#{ch.id}>" if ch else "the payout ticket channel"
    msg = f"We understand your issue, however general is not the place for it. Please open a payout ticket in {where}."
    return msg[:150]


def _exempt_channels(guild: discord.Guild, watched: set[int], use_categories: bool) -> list:
    """Every channel except the watched ones. Categories are exempted whole (covers new ticket channels too)
    unless a watched channel sits inside them."""
    out = []
    for cat in guild.categories:
        kids = cat.channels
        if use_categories and not any(c.id in watched for c in kids):
            out.append(cat)
        else:
            out += [c for c in kids if c.id not in watched]
    out += [c for c in guild.channels
            if c.category is None and not isinstance(c, discord.CategoryChannel) and c.id not in watched]
    return out


TAG_RULE = "Cashed Bot: one tag per message"
TAG_NOTICE = "You can only tag one person per message."


async def _upsert(guild, rules, name, trigger, message, roles, watched) -> str | None:
    rule = next((r for r in rules if r.name == name), None)
    actions = [discord.AutoModRuleAction(custom_message=message[:150])]
    last = None
    for use_cats in (True, False):  # categories first; fall back to listing channels one by one
        exempt = _exempt_channels(guild, watched, use_cats)
        if len(exempt) > 50:
            last = (f"This server has {len(exempt)} channels/categories outside the filtered ones, and Discord "
                    f"only allows 50 exemptions. Move loose channels into categories, then try again.")
            continue
        try:
            kw = dict(trigger=trigger, actions=actions, exempt_roles=roles, exempt_channels=exempt, enabled=True)
            if rule:
                await rule.edit(reason="General filter updated", **kw)
            else:
                await guild.create_automod_rule(name=name, event_type=discord.AutoModRuleEventType.message_send,
                                                reason="General filter set up", **kw)
            return None
        except discord.HTTPException as e:
            if isinstance(e, discord.Forbidden):
                raise
            last = f"Discord refused the {name!r} rule: {e.text or e}"
            log.warning("%s (categories=%s) refused: %s", name, use_cats, e)
    return last


async def apply_rule(bot: discord.Client, guild: discord.Guild, cfg: dict, staff_role_ids) -> str | None:
    """Create, update or remove the AutoMod rules for this server: payout complaints, and one tag per message.
    Returns an error message, or None."""
    try:
        rules = await guild.fetch_automod_rules()
    except discord.Forbidden:
        return "The bot needs the **Manage Server** permission (Server Settings → Roles → bot role)."
    watched = {int(c) for c in cfg.get("channels", []) if guild.get_channel(int(c))}
    tagged = {int(c) for c in cfg.get("tag_channels", []) if guild.get_channel(int(c))}
    try:
        roles = [r for r in (guild.get_role(i) for i in staff_role_ids) if r][:20]
        errs = []
        # Payout complaint filter (/fud on|off)
        if watched:
            e1 = await _upsert(guild, rules, RULE_NAME,
                               discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword,
                                                      regex_patterns=PATTERNS),
                               notice_text(bot, cfg), roles, watched)
            if e1:
                errs.append(e1)
        else:
            for r in rules:
                if r.name == RULE_NAME:
                    await r.delete(reason="Payout complaint filter switched off")
        # One tag per message (/fud tags)
        if tagged:
            other = next((r for r in rules if r.trigger.type == discord.AutoModRuleTriggerType.mention_spam
                          and r.name != TAG_RULE), None)
            if other:
                errs.append(f"One-tag limit not set: Discord allows one mention-spam rule and **{other.name}** "
                            f"already exists. Delete it in Server Settings → AutoMod, then run `/fud status`.")
            else:
                e2 = await _upsert(guild, rules, TAG_RULE,
                                   discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.mention_spam,
                                                          mention_limit=1),
                                   TAG_NOTICE, roles, tagged)
                if e2:
                    errs.append(e2)
        else:
            for r in rules:
                if r.name == TAG_RULE:
                    await r.delete(reason="One-tag limit switched off")
        return "\n".join(errs) or None
    except discord.Forbidden:
        return "The bot needs the **Manage Server** permission (Server Settings → Roles → bot role)."
