"""
qd.features.news — headline classification, channel 2 of 4.

Deliberately deterministic. A rule-based classifier is worse than a language
model at reading a headline, and better at being backtested: it returns the same
label for the same text forever, so a result measured on 2019 headlines still
describes the system running today. Swap in a model whose weights change under
you and every historical result silently stops applying — and worse, if that
model was trained on data covering the backtest period, its "reading" of a 2019
headline is contaminated by knowing how 2019 ended.

If you do want a model in the loop, the hook is `classify_with()`: run it at
INGEST time, cache the label on the NewsItem, and replay reads the cached label.
Classification must happen when the headline arrives, never at decision time.

The priors below are asserted, not fitted. They encode ordinary market
knowledge — a buyout target gaps up, a secondary offering dilutes — with
magnitudes that are round numbers rather than optimised constants. Calibrating
them against realised forward returns is a research task, and until that is
done they are exactly what they look like: reasonable guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping, Optional, Sequence

from qd.config import NewsConfig
from qd.types import Evidence, NewsItem, Source, ensure_utc, clamp

# ─────────────────────────────────────────────────────────────────────────────
# Taxonomy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Category:
    """One event type: how to spot it, which way it cuts, and how hard."""
    name: str
    pattern: re.Pattern
    score: float        # directional prior in [-1, 1]
    confidence: float   # how reliably the pattern identifies the event
    note: str = ""


def _p(*alternatives: str) -> re.Pattern:
    return re.compile("|".join(alternatives), re.IGNORECASE)


# Ordered by specificity: the first match wins, so narrow high-conviction
# patterns must precede broad ones. "agrees to be acquired" has to be tested
# before the generic "acquisition" or every deal headline collapses into the
# weaker category.
CATEGORIES: tuple[Category, ...] = (
    # ── Corporate actions: the largest and fastest repricings ────────────────
    Category("acquisition_target",
             _p(r"\bto be acquired\b", r"\bagrees? to be (acquired|bought)\b",
                r"\bbuyout offer\b", r"\bto acquire\b", r"\btakeover bid\b",
                r"\bagreed to acquire\b", r"\bmerger agreement\b"),
             0.90, 0.80,
             "target gaps to near the offer; direction depends on role — see _ma_role"),
    Category("bankruptcy",
             _p(r"\bchapter 11\b", r"\bbankruptcy\b", r"\bgoing concern\b",
                r"\bdefaults? on\b", r"\bdelisting\b"),
             -0.95, 0.85),
    Category("offering",
             _p(r"\b(public|secondary|common stock) offering\b", r"\bprices? \$?\d+.{0,20}offering\b",
                r"\bshelf registration\b", r"\bconvertible notes? offering\b",
                r"\bat-the-market\b", r"\bdilut"),
             -0.60, 0.75,
             "new shares for existing holders' claim on the same earnings"),
    Category("buyback",
             _p(r"\bbuyback\b", r"\brepurchase program\b", r"\bshare repurchase\b",
                r"\bdividend increase\b", r"\braises? (its )?dividend\b"),
             0.45, 0.70),
    Category("index_inclusion",
             _p(r"\bjoin(ing)? the s&p\b", r"\badded to the s&p\b", r"\bindex inclusion\b",
                r"\bwill replace\b.{0,30}\bin the s&p\b", r"\badded to (the )?nasdaq-100\b"),
             0.70, 0.80,
             "forced index-fund buying, largely mechanical"),

    # ── Guidance: usually a bigger mover than the reported quarter ───────────
    Category("guidance_raise",
             _p(r"\braises? (its )?(fy\d*|full[- ]year|q\d|guidance|outlook|forecast)",
                r"\bboosts? (its )?(guidance|outlook|forecast)\b",
                r"\bguidance above\b", r"\bupbeat (guidance|outlook|forecast)\b"),
             0.75, 0.75),
    Category("guidance_cut",
             _p(r"\b(cuts?|lowers?|slashes?|trims?) (its )?(fy\d*|full[- ]year|q\d|guidance|outlook|forecast)",
                r"\bguidance below\b", r"\bwarns? on\b", r"\bprofit warning\b",
                r"\bwithdraws?\b.{0,20}\b(guidance|outlook|forecast)\b",
                r"\bsuspends?\b.{0,20}\b(guidance|outlook|dividend)\b"),
             -0.80, 0.80),

    # ── Regulatory / clinical ────────────────────────────────────────────────
    Category("fda_approval",
             _p(r"\bfda approv", r"\bapproved by the fda\b", r"\bgrants? (full )?approval\b",
                r"\bceo?e mark\b", r"\bbreakthrough therapy\b"),
             0.80, 0.80),
    Category("fda_rejection",
             _p(r"\bcomplete response letter\b", r"\bcrl\b", r"\bfda reject",
                r"\bdeclines? to approve\b", r"\brefuses? to approve\b",
                r"\bclinical hold\b", r"\bfails? (its )?(phase|primary endpoint)",
                r"\bmisses? primary endpoint\b", r"\bhalts? (the )?trial\b"),
             -0.85, 0.80),
    Category("investigation",
             _p(r"\bsec (probe|investigation|subpoena)\b", r"\bdoj (probe|investigation)\b",
                r"\bantitrust (probe|suit|lawsuit)\b", r"\bformal investigation\b",
                r"\bftc (sues?|blocks?)\b"),
             -0.65, 0.70),
    Category("legal_adverse",
             _p(r"\bclass action\b", r"\bjury (finds?|awards?)\b", r"\bordered to pay\b",
                r"\bpatent (loss|invalidat)", r"\brecall(s|ed|ing)?\b", r"\bdata breach\b"),
             -0.50, 0.60),

    # ── Sell-side and short sellers ──────────────────────────────────────────
    Category("short_report",
             _p(r"\bshort seller\b", r"\bshort report\b", r"\bhindenburg\b",
                r"\bmuddy waters\b", r"\bcitron\b", r"\baccounting (fraud|irregularit)"),
             -0.70, 0.65),
    Category("upgrade",
             _p(r"\bupgrade[sd]?\b", r"\braised to (buy|outperform|overweight)\b",
                r"\binitiated (at |with )?(buy|outperform|overweight)\b",
                r"\bdouble upgrade\b"),
             0.35, 0.55,
             "weak and fast-decaying; the move is usually over in minutes"),
    Category("downgrade",
             _p(r"\bdowngrade[sd]?\b", r"\bcut to (sell|underperform|underweight|hold)\b",
                r"\blowered to (sell|underperform|underweight)\b"),
             -0.40, 0.55),
    Category("price_target",
             _p(r"\bprice target\b", r"\bpt (raised|cut|lowered)\b", r"\btarget to \$"),
             0.15, 0.30,
             "near-noise; included so it does not fall through to unclassified"),

    # ── Operating news ───────────────────────────────────────────────────────
    Category("major_contract",
             _p(r"\bwins? (a )?\$?\d+.{0,15}(contract|order|deal)\b", r"\bawarded a\b.{0,25}contract\b",
                r"\blanded? (a )?deal\b", r"\bsigns? (a )?(multi-year|\$\d+)"),
             0.50, 0.60),
    Category("partnership",
             _p(r"\bpartnership with\b", r"\bteams? up with\b", r"\bcollaborat(es?|ion) with\b",
                r"\bstrategic alliance\b"),
             0.30, 0.45),
    Category("executive_change",
             _p(r"\bceo (steps? down|resigns?|departs?|to retire)\b",
                r"\bcfo (steps? down|resigns?|departs?)\b", r"\bnames? new (ceo|cfo)\b",
                r"\bexecutive (shakeup|shake-up)\b"),
             -0.30, 0.45,
             "ambiguous — an abrupt CFO exit reads worse than a planned CEO succession"),
    Category("restructuring",
             _p(r"\blayoffs?\b", r"\bjob cuts?\b", r"\brestructuring\b", r"\bcost[- ]cutting\b"),
             0.10, 0.35,
             "cost relief against demand weakness; genuinely two-sided, kept small"),
    Category("insider_buying",
             _p(r"\binsider buying\b", r"\bceo buys?\b", r"\bdirector (buys?|purchases?)\b",
                r"\bform 4\b.{0,20}\bpurchase\b"),
             0.35, 0.45),
)

# Hedging language. "In talks to be acquired" is not "agrees to be acquired",
# and treating them alike is how a rumour gets sized like a signed deal.
HEDGE = _p(r"\brumor", r"\breportedly\b", r"\bin talks\b", r"\bexploring\b",
           r"\bconsidering\b", r"\bmay\b", r"\bcould\b", r"\bmight\b",
           r"\bweighs?\b", r"\bsources? say\b", r"\bdenies?\b", r"\bdenied\b",
           r"\bspeculat", r"\bis said to\b", r"\bmulls?\b")

# Explicit negation flips the sign — "FDA declines to approve" matches the
# approval pattern but means its opposite.
NEGATION = _p(r"\bnot\b", r"\bno longer\b", r"\bwithdraws?\b", r"\bterminat",
              r"\bcall(s|ed) off\b", r"\bscraps?\b", r"\babandons?\b",
              r"\bfails? to\b", r"\bdeclines? to\b", r"\brejects?\b")


@dataclass(frozen=True)
class Classification:
    category: str
    score: float
    confidence: float
    hedged: bool
    negated: bool
    matched: str = ""


def classify(text: str) -> Optional[Classification]:
    """First-match classification. None when nothing recognisable fires.

    Unclassified is the common case and the correct answer for most of the
    wire: the majority of headlines are neither tradeable nor interpretable,
    and a classifier that always produces a label is producing noise.
    """
    if not text:
        return None
    for cat in CATEGORIES:
        m = cat.pattern.search(text)
        if not m:
            continue
        hedged = bool(HEDGE.search(text))
        # Negation only counts when it sits OUTSIDE the matched phrase.
        # "FDA declines to approve" matches fda_rejection *because of* the word
        # "declines"; flipping on it would double-negate an already-negative
        # category and report a rejection as an approval. But "calls off merger
        # agreement" matches acquisition_target on "merger agreement" while the
        # negation sits elsewhere in the headline, and that one must flip.
        negated = any(
            n.end() <= m.start() or n.start() >= m.end()
            for n in NEGATION.finditer(text)
        )
        score = cat.score
        conf = cat.confidence
        if negated:
            score = -score
            conf *= 0.7    # negation detection is crude; back off rather than commit
        if hedged:
            score *= 0.45
            conf *= 0.6
        return Classification(
            category=cat.name, score=score, confidence=conf,
            hedged=hedged, negated=negated, matched=m.group(0),
        )
    return None


# M&A role: the same headline is bullish for one ticker and mildly bearish for
# the other, so the sign has to depend on which side our symbol sits.
_ACQUIRE_VERB = _p(r"\bto acquire\b", r"\bagreed to acquire\b", r"\bto buy\b",
                   r"\bacquires?\b", r"\bbuys?\b", r"\btakeover of\b")


def _ma_role(text: str, symbol: str) -> str:
    """'target' | 'acquirer' | 'unknown' for an M&A headline.

    Split on the acquisition verb: whoever is named before it is buying,
    whoever is named after it is being bought.
    """
    m = _ACQUIRE_VERB.search(text)
    if not m:
        return "unknown"
    before, after = text[: m.start()].upper(), text[m.end():].upper()
    sym = symbol.upper()
    in_before = re.search(rf"\b{re.escape(sym)}\b", before) is not None
    in_after = re.search(rf"\b{re.escape(sym)}\b", after) is not None
    if in_after and not in_before:
        return "target"
    if in_before and not in_after:
        return "acquirer"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Novelty
# ─────────────────────────────────────────────────────────────────────────────

class NoveltyTracker:
    """Remembers recent headlines so rewrites do not read as confirmation.

    A single story reaches the tape through a wire, three aggregators and a
    dozen syndication feeds. Scoring each copy as independent evidence turns
    one event into an avalanche of agreement, and the confluence requirement —
    the main defence against acting on a single bad input — is exactly what
    that avalanche defeats.
    """

    __slots__ = ("_seen", "_window")

    def __init__(self, window: timedelta) -> None:
        self._seen: dict[str, list[datetime]] = {}
        self._window = window

    def observe(self, item: NewsItem) -> int:
        """Record the item; return how many times it was already seen."""
        key = item.dedup_key()
        now = item.known_at
        prior = [t for t in self._seen.get(key, []) if now - t <= self._window]
        self._seen[key] = prior + [now]
        return len(prior)

    def repeats(self, item: NewsItem) -> int:
        key = item.dedup_key()
        now = item.known_at
        return len([t for t in self._seen.get(key, []) if now - t <= self._window])

    def prune(self, now: datetime) -> None:
        now = ensure_utc(now)
        for key in list(self._seen):
            kept = [t for t in self._seen[key] if now - t <= self._window]
            if kept:
                self._seen[key] = kept
            else:
                del self._seen[key]


# ─────────────────────────────────────────────────────────────────────────────
# Evidence
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(
    symbol: str,
    items: Sequence[NewsItem],
    now: datetime,
    cfg: NewsConfig,
    novelty: Optional[NoveltyTracker] = None,
) -> list[Evidence]:
    """Score headlines for one symbol into evidence.

    Only items with `known_at <= now` are considered; the filter is here rather
    than assumed upstream because this is the channel most likely to be fed
    from a bulk historical dump where every item looks equally available.
    """
    now = ensure_utc(now)
    out: list[Evidence] = []

    for item in items:
        if item.known_at > now:
            continue                       # not yet ours to know
        if symbol.upper() not in item.symbols:
            continue
        if now - item.known_at > cfg.ttl:
            continue

        cls = classify(item.text)
        if cls is None:
            continue

        score, conf = cls.score, cls.confidence

        # M&A sign correction.
        if cls.category == "acquisition_target":
            role = _ma_role(item.text, symbol)
            if role == "acquirer":
                score = -0.20          # acquirers usually drift down on deal news
                conf *= 0.6
            elif role == "unknown":
                conf *= 0.7

        # Source tier.
        src = (item.source or "").lower()
        weight = cfg.default_source_weight
        for name, w in cfg.source_weights.items():
            if name and name in src:
                weight = w
                break
        conf *= weight

        # Repetition. Each prior sighting cuts confidence geometrically.
        repeats = novelty.observe(item) if novelty else 0
        if repeats:
            conf *= cfg.repeat_confidence_decay ** repeats

        # Feed latency. A headline that took ten minutes to reach us has been
        # tradeable by faster participants for ten minutes; whatever edge it
        # carried is gone, and acting on it is buying the top of the reaction.
        if item.latency > cfg.max_latency:
            conf *= 0.25

        conf = clamp(conf, 0.0, 1.0)
        if conf < cfg.min_confidence or abs(score) < 0.05:
            continue

        out.append(Evidence(
            source=Source.NEWS,
            kind=cls.category,
            symbol=symbol.upper(),
            score=score,
            confidence=conf,
            observed_at=item.known_at,
            ttl=cfg.ttl,
            detail={
                "headline": item.headline[:200],
                "source": item.source,
                "matched": cls.matched,
                "hedged": cls.hedged,
                "negated": cls.negated,
                "repeats": repeats,
                "latency_s": round(item.latency.total_seconds(), 1),
                "url": item.url,
            },
        ))

    return out


def classify_with(
    items: Iterable[NewsItem],
    classifier: Callable[[NewsItem], Optional[tuple[str, float, float]]],
) -> list[NewsItem]:
    """Attach externally-produced labels at INGEST time.

    For plugging in a language model without breaking replay: the label is
    frozen onto the record as `labels`, so a later backtest reads what the
    classifier said *then* rather than re-running today's model over old text
    and inheriting its hindsight.
    """
    from dataclasses import replace as _replace

    out = []
    for it in items:
        res = classifier(it)
        if res is None:
            out.append(it)
            continue
        cat, score, conf = res
        out.append(_replace(it, labels=tuple(it.labels) + (f"{cat}:{score:.3f}:{conf:.3f}",)))
    return out


__all__ = [
    "Category", "CATEGORIES", "Classification", "classify", "classify_with",
    "NoveltyTracker", "evaluate",
]
