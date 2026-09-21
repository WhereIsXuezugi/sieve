"""Text processing primitives.

Deliberately dependency-free. A hashed TF-IDF sparse vector over titles,
keywords, descriptions and (when available) transcripts turns out to be plenty
for "find me more like this" on a personal library of a few tens of thousands of
videos, and it costs nothing at rest. `embed.py` can swap in a real embedding
model if you want one; nothing else in the codebase cares which produced the
vector.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+#._-]*", re.IGNORECASE)

STOPWORDS = frozenset(["a", "an", "the", "and", "or", "but", "if", "then", "than", "that", "this", "these", "those", "is", "are", "was", "were", "be", "been", "being", "am", "i", "me", "my", "we", "our", "you", "your", "he", "she", "it", "its", "they", "them", "their", "what", "which", "who", "whom", "how", "why", "when", "where", "to", "of", "in", "on", "at", "by", "for", "with", "about", "against", "between", "into", "through", "during", "before", "after", "above", "below", "from", "up", "down", "out", "off", "over", "under", "again", "further", "once", "here", "there", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same", "so", "too", "very", "s", "t", "can", "will", "just", "don", "should", "now", "new", "video", "watch", "full", "part", "episode", "ep", "official", "get", "got", "go", "going", "make", "made", "one", "two", "three", "four", "five", "like", "really", "thing", "things", "lot", "best", "top", "vs"])

# Marker lexicons. Short on purpose: these are priors, and the online learner in
# learner.py corrects them against your actual behaviour.
TECHNICAL = frozenset(["algorithm", "compiler", "kernel", "assembly", "runtime", "latency", "throughput", "protocol", "topology", "register", "transistor", "voltage", "impedance", "amplifier", "firmware", "embedded", "microcontroller", "fpga", "verilog", "vhdl", "rust", "golang", "kubernetes", "docker", "postgres", "sqlite", "parser", "lexer", "bytecode", "heap", "allocator", "mutex", "concurrency", "pipeline", "instruction", "cache", "benchmark", "profiling", "entropy", "gradient", "tensor", "matrix", "eigenvalue", "integral", "derivative", "theorem", "proof", "lemma", "topology", "manifold", "quantum", "photon", "lattice", "reverse", "engineering", "exploit", "fuzzing", "disassembly", "binary", "syscall", "packet", "subnet", "cryptography", "thermodynamics", "metallurgy", "tolerance", "machining", "cad", "kinematics", "torque", "bearing", "hydraulic"])

ACADEMIC = frozenset(["lecture", "seminar", "course", "chapter", "proof", "derivation", "theory", "analysis", "introduction", "fundamentals", "explained", "deep", "dive", "walkthrough", "tutorial", "masterclass", "primer", "textbook", "syllabus", "problem", "set", "university", "professor", "phd", "research", "paper", "preprint", "arxiv", "doi", "journal", "citation", "methodology", "experiment", "hypothesis", "dataset", "replication", "peer", "review", "survey", "overview", "foundations"])

MEME = frozenset(["meme", "memes", "brainrot", "sigma", "rizz", "gyatt", "skibidi", "ohio", "npc", "cringe", "based", "fyp", "tiktok", "compilation", "funny", "moments", "fails", "try", "not", "to", "laugh", "reaction", "pov", "tier", "list", "ranking", "every", "single", "time", "literally", "nobody", "bro", "chat", "aura", "goofy", "sus", "edit", "edits", "slideshow", "shitpost", "cursed"])

CLICKBAIT = frozenset(["shocking", "insane", "crazy", "unbelievable", "exposed", "destroyed", "obliterated", "wrecked", "epic", "ultimate", "secret", "truth", "nobody", "hidden", "banned", "illegal", "dangerous", "terrifying", "gone", "wrong", "you", "won't", "believe", "must", "watch", "stop", "doing", "this", "changed", "forever", "finally", "revealed", "worst", "best", "ever", "actually", "i", "tried", "what", "happened", "watch", "till", "the", "end", "wait", "for", "it", "going", "viral"])

MUSIC_MARKERS = frozenset(["official", "music", "video", "lyrics", "lyric", "audio", "album", "single", "remix", "cover", "acoustic", "live", "session", "feat", "ft", "prod", "instrumental", "soundtrack", "ost", "mix", "playlist", "dj", "set", "concert", "mv"])

NSFW_MARKERS = frozenset(["nsfw", "nude", "nudity", "naked", "sexy", "hot", "lingerie", "erotic", "porn", "onlyfans", "thirst", "twerk", "explicit", "18+", "seductive", "fetish", "hentai", "ecchi", "bikini", "strip"])

AI_MARKERS = frozenset(["ai", "generated", "aivoice", "texttospeech", "tts", "synthesized", "narrated", "by", "ai", "midjourney", "stablediffusion", "elevenlabs", "heygen", "deepfake", "voiceover", "generated", "using", "chatgpt", "scripted", "by", "ai"])

ENTERTAINMENT = frozenset(["gameplay", "playthrough", "speedrun", "stream", "highlights", "vlog", "challenge", "prank", "comedy", "sketch", "podcast", "interview", "review", "unboxing", "reaction", "drama", "storytime", "gaming", "lets", "play", "montage"])

HOBBY = frozenset(["woodworking", "restoration", "repair", "build", "diy", "cooking", "recipe", "garden", "fishing", "climbing", "cycling", "photography", "painting", "knitting", "model", "kit", "workshop", "maintenance", "homelab", "3d", "printing", "brewing"])

# Profanity markers. This is a deliberately small, mild list: the goal is a
# *signal* ("this video swears a lot") that a user can weight, not a censor.
# Full-strength wordlists such as LDNOOBW exist and can be dropped in here —
# they are data, not code, and this function does not care how long the set is.
# Counting matters more than membership, so the score keys off density.
PROFANITY = frozenset(["damn", "dammit", "hell", "crap", "shit", "shitty", "bullshit", "fuck", "fucking", "fucked", "fucker", "motherfucker", "ass", "asshole", "arse", "bastard", "bitch", "bitches", "dick", "dickhead", "prick", "piss", "pissed", "cock", "twat", "wanker", "bollocks", "bugger", "slut", "whore", "cunt", "goddamn", "jackass", "dumbass", "shitpost", "wtf", "stfu"])

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\uFE0F\u2B00-\u2BFF]"
)
CITATION_RE = re.compile(r"(arxiv\.org|doi\.org|/10\.\d{4}|github\.com|scholar\.google)", re.I)
CHAPTER_RE = re.compile(r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}\s+\S", re.M)
SPONSOR_RE = re.compile(r"(sponsor|use code|discount|promo code|affiliate|patreon|brilliant\.org|nordvpn|squarespace)", re.I)


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [m.group(0).lower() for m in WORD_RE.finditer(text)]


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 2]


def lexicon_ratio(tokens: list[str], lexicon: frozenset[str]) -> float:
    """Fraction of tokens that hit a lexicon, squashed so a couple of hits in a
    short title still registers but a long transcript is not diluted to zero."""
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in lexicon)
    return 1.0 - math.exp(-3.0 * hits / max(12, len(tokens)) - 0.15 * min(hits, 6))


def lexicon_density(tokens: list[str], lexicon: frozenset[str]) -> float:
    """Hits per hundred tokens, squashed to 0..1.

    Unlike `lexicon_ratio` this does not saturate after a handful of hits, which
    is what you want for profanity: one swear word in a forty-minute talk and a
    swear word every sentence are different things.

    The denominator has a floor so that a short, blunt title is not treated as
    overwhelming evidence, but is not ignored either.
    """
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in lexicon)
    if not hits:
        return 0.0
    per_hundred = 100.0 * hits / max(20, len(tokens))
    return clamp(per_hundred / 4.0)


def caps_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def emoji_count(text: str) -> int:
    return len(EMOJI_RE.findall(text or ""))


def punctuation_heat(text: str) -> float:
    if not text:
        return 0.0
    bangs = text.count("!") + text.count("?")
    return min(1.0, bangs / 3.0)


def unique_ratio(tokens: list[str]) -> float:
    if len(tokens) < 20:
        return 0.5
    return len(set(tokens)) / len(tokens)


def words_per_minute(transcript: str, duration: int) -> float:
    if not transcript or duration <= 0:
        return 0.0
    return len(tokenize(transcript)) / (duration / 60.0)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def sigmoid(x: float) -> float:
    if x < -30:
        return 0.0
    if x > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


# -- sparse vectors ---------------------------------------------------------

Vector = dict[str, float]


def term_vector(parts: Iterable[tuple[str, float]], top_k: int = 64) -> Vector:
    """Build an L2-normalised sparse TF vector from (text, weight) pairs."""
    counts: Counter[str] = Counter()
    for text, weight in parts:
        if not text or weight <= 0:
            continue
        for token in content_tokens(text):
            counts[token] += weight
    if not counts:
        return {}
    top = counts.most_common(top_k)
    vec = {term: 1.0 + math.log(count) for term, count in top}
    return normalise(vec)


def normalise(vec: Vector) -> Vector:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0:
        return {}
    return {k: v / norm for k, v in vec.items()}


def cosine(a: Vector, b: Vector) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return max(0.0, min(1.0, sum(v * b.get(k, 0.0) for k, v in a.items())))


def combine(vectors: Iterable[tuple[Vector, float]]) -> Vector:
    out: Vector = {}
    for vec, weight in vectors:
        if weight <= 0:
            continue
        for key, value in vec.items():
            out[key] = out.get(key, 0.0) + value * weight
    return normalise(out)


def top_terms(vec: Vector, n: int = 8) -> list[str]:
    return [k for k, _ in sorted(vec.items(), key=lambda kv: -kv[1])[:n]]
