"""Where videos come from when Sieve knows nothing about you yet.

With no subscriptions, no history and no interests, the ranking has nothing to
rank. Rather than an empty page and an instruction to go and find an export
file, Sieve starts from topics you pick: for each, a few search phrases (used
(any backend can search; see ytsearch.py) and a handful of
long-running, well-known channels whose public RSS feeds need nothing at all.

Once you watch things, your derived interests take over the searches and the
channels the ranking rates well take over from these. This list is a starting
point, not a curation: each channel is a public feed, refreshed like any other,
and ranked and filtered like everything else.

A channel id that stops resolving (renamed, deleted) is skipped and remembered
as dead for a week by the puller, so a stale entry here costs one request.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    searches: tuple[str, ...]
    channels: tuple[tuple[str, str], ...] = field(default_factory=tuple)


TOPICS: dict[str, Topic] = {t.key: t for t in (
    Topic("science", "Science", ("science explained", "physics experiment"), (
        ("UCHnyfMqiRRG1u-2MsSQLbXA", "Veritasium"),
        ("UCsXVk37bltHxD1rDPwtNM8Q", "Kurzgesagt"),
        ("UC6107grRI4m0o2-emgoDnAA", "SmarterEveryDay"),
        ("UCEIwxahdLz7bap-VDs9h35A", "Steve Mould"),
        ("UCUHW94eEFW7hkUMVaZz4eDg", "minutephysics"),
        ("UCZYTClx2T1of7BRZ86-8fow", "SciShow"),
    )),
    Topic("engineering", "Engineering", ("engineering explained", "how it's made engineering"), (
        ("UCMOqf8ab-42UUQIdVoKwjlQ", "Practical Engineering"),
        ("UCR1IuLEqb6UEA_zQ81kwXfg", "Real Engineering"),
        ("UCj1VqrHhDte54oLgPG4xpuQ", "Stuff Made Here"),
        ("UCY1kMZp36IQSyNx_9h4mpCg", "Mark Rober"),
    )),
    Topic("mathematics", "Mathematics", ("mathematics explained", "math visualization",
                                         "advanced mathematics lecture", "mathematical proof"), (
        ("UC1_uAIS3r8Vu6JjXWvastJg", "Mathologer"),
        ("UCYO_jab_esuFRV4b17AJtAw", "3Blue1Brown"),
        ("UCoxcjq-8xIDTYp3uz647V5A", "Numberphile"),
        ("UCSju5G2aFaWMqn-_0YBtq5A", "Stand-up Maths"),
    )),
    Topic("security", "Cyber security", ("cyber security explained", "reverse engineering",
                                         "CTF walkthrough", "binary exploitation"), (
        ("UClcE-kVhqyiHCcjYwcpfj9w", "LiveOverflow"),
        ("UCVeW9qkBjo3zosnqUbG7CFw", "John Hammond"),
        ("UC9x0AN7BWHpCDHSm9NiJFJQ", "NetworkChuck"),
    )),
    Topic("programming", "Programming", ("programming tutorial", "computer science explained"), (
        ("UC9-y-6csu5WGm29I7JiwpnA", "Computerphile"),
        ("UCsBjURrPoezykLs9EqgamOA", "Fireship"),
        ("UCS0N5baNlQWJCUrhCEo8WlA", "Ben Eater"),
        ("UCvjgXvBlbQiydffZU7m1_aw", "The Coding Train"),
    )),
    Topic("technology", "Technology", ("technology review", "tech explained"), (
        ("UCBJycsmduvYEL83R_U4JriQ", "Marques Brownlee"),
        ("UCXuqSBlHAE6Xw-yeJA0Tunw", "Linus Tech Tips"),
        ("UCbfYPyITQ-7l4upoX8nvctg", "Two Minute Papers"),
    )),
    Topic("history", "History", ("history documentary", "ancient history explained"), (
        ("UCsaGKqPZnGp_7N80hcHySGQ", "Tasting History"),
        ("UCNIuvl7V8zACPpTmmNIqP2A", "OverSimplified"),
    )),
    Topic("explainers", "Geography and explainers", ("geography explained", "how the world works"), (
        ("UC2C_jShtL725hvbm1arSV9w", "CGP Grey"),
        ("UC9RM-iSvTu1uPJb8X5yp3EQ", "Wendover Productions"),
        ("UCBa659QWEk1AI4Tg--mrJ2A", "Tom Scott"),
    )),
    Topic("space", "Space", ("space exploration", "astronomy explained"), (
        ("UCLA_DiR1FfKNvjuUpBHmylQ", "NASA"),
    )),
    Topic("learning", "General learning", ("educational video", "lecture"), (
        ("UCsooa4yRKGN_zEE8iknghZA", "TED-Ed"),
        ("UCX6b17PVsYBQ0ip5gyeme-Q", "CrashCourse"),
        ("UCAuUUnT6oDeKwE6v1NGQxug", "TED"),
    )),
    Topic("cooking", "Cooking", ("cooking recipe", "cooking technique"), (
        ("UCJHA_jMfCvEnv-3kRjTCQXw", "Babish Culinary Universe"),
    )),
    Topic("making", "Making and DIY", ("woodworking project", "restoration"), (
        ("UCAL3JXZSzSm8AlZyD3nQdBA", "Primitive Technology"),
    )),
)}


def chosen(topics: list[str] | None) -> list[Topic]:
    """The topics that exist, in the order given. Unknown keys are ignored."""
    return [TOPICS[k] for k in (topics or []) if k in TOPICS]


def channels_for(topics: list[str] | None) -> list[tuple[str, str]]:
    """Every starter channel for the chosen topics, interleaved so that a
    small per-sync budget still reaches every topic rather than exhausting the
    first one."""
    lists = [list(t.channels) for t in chosen(topics)]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    while any(lists):
        for bucket in lists:
            if bucket:
                cid, name = bucket.pop(0)
                if cid not in seen:
                    seen.add(cid)
                    out.append((cid, name))
    return out


def searches_for(topics: list[str] | None, custom: list[str] | None = None) -> list[str]:
    """Search phrases: the first phrase of every topic, then the second, then
    your own phrases, so a small number of searches spreads across topics."""
    picked = chosen(topics)
    out: list[str] = []
    for round_ in range(max((len(t.searches) for t in picked), default=0)):
        for topic in picked:
            if round_ < len(topic.searches) and topic.searches[round_] not in out:
                out.append(topic.searches[round_])
    # Your own words come first, in the order you wrote them.
    mine: list[str] = []
    for phrase in custom or []:
        phrase = str(phrase).strip()
        if phrase and phrase not in mine:
            mine.append(phrase)
    return mine + [phrase for phrase in out if phrase not in mine]
