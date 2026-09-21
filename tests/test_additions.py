"""Tests for the pieces added after the first pass: channel quality scoring,
the filters that were configurable but unenforced, the pluggable embedding
backend, and schema migration."""

import sqlite3
import time

import pytest

from sieve import channels, embed, ranking, scoring
from sieve import textutil as T
from sieve.config import Config, deep_merge, resolve_settings
from sieve.db import Database


def make_video(vid, **overrides):
    base = {
        "id": vid, "title": "A video about kernels", "author": "Chan", "author_id": "UC1",
        "published": int(time.time()) - 86400, "duration": 1200, "views": 9000, "likes": 400,
        "description": "0:00 intro", "keywords": ["kernel"], "genre": "Science & Technology",
        "is_live": 0, "is_upcoming": 0, "family_safe": 1, "sub_count": 50000,
        "caption_langs": ["en"],
    }
    base.update(overrides)
    return base


def settings(**patch):
    return resolve_settings(deep_merge({"filters": {"hide_shorts": False}}, patch))


@pytest.fixture()
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def seed(db, videos):
    db.upsert_videos(videos)
    db.executemany(scoring.INSERT_SCORE,
                   [scoring.card_to_row(scoring.score_video(v)) for v in videos])


# -- profanity -------------------------------------------------------------


def test_profanity_density_scales_with_frequency():
    clean = T.content_tokens("a calm discussion of bearing tolerances and surface finish " * 4)
    sweary = T.content_tokens("this shit is fucking broken what the hell damn bullshit " * 4)
    assert T.lexicon_density(clean, T.PROFANITY) == 0.0
    assert T.lexicon_density(sweary, T.PROFANITY) > 0.3


def test_profanity_score_separates_the_two():
    calm = scoring.score_video(make_video("a", title="Bearing tolerances explained"))
    rude = scoring.score_video(
        make_video("b", title="This fucking thing is broken as shit"),
        "what the hell is this bullshit damn it this is fucked " * 5,
    )
    assert rude["profanity"] > calm["profanity"]
    assert calm["profanity"] < 30


def test_profanity_is_filterable(db):
    seed(db, [
        make_video("clean1", title="Bearing tolerances explained"),
        make_video("rude01", title="This fucking shit is broken, what the hell, damn"),
    ])
    result = ranking.recommend(db, settings(filters={"max_profanity": 35}))
    ids = {item.id for item in result.items}
    assert "clean1" in ids
    assert "rude01" not in ids


# -- subscriber and language filters ---------------------------------------


def test_subscriber_floor_and_ceiling(db):
    seed(db, [
        make_video("tiny01", sub_count=800),
        make_video("huge01", sub_count=9_000_000),
    ])
    small_only = ranking.recommend(db, settings(filters={"max_subs": 100_000}))
    assert {i.id for i in small_only.items} == {"tiny01"}
    big_only = ranking.recommend(db, settings(filters={"min_subs": 1_000_000}))
    assert {i.id for i in big_only.items} == {"huge01"}


def test_unknown_subscriber_count_abstains(db):
    """A filter that cannot see the data must not act as though it can."""
    seed(db, [make_video("nodata", sub_count=0)])
    result = ranking.recommend(db, settings(filters={"min_subs": 1_000_000}))
    assert {i.id for i in result.items} == {"nodata"}


def test_language_filter_uses_caption_tracks(db):
    seed(db, [
        make_video("eng001", caption_langs=["en"]),
        make_video("deu001", caption_langs=["de"]),
    ])
    result = ranking.recommend(db, settings(filters={"languages": ["en"]}))
    assert {i.id for i in result.items} == {"eng001"}


def test_language_filter_abstains_without_captions(db):
    seed(db, [make_video("nocap1", caption_langs=[])])
    result = ranking.recommend(db, settings(filters={"languages": ["en"]}))
    assert {i.id for i in result.items} == {"nocap1"}


def test_region_variants_still_match(db):
    seed(db, [make_video("enGB01", caption_langs=["en-GB"])])
    result = ranking.recommend(db, settings(filters={"languages": ["en"]}))
    assert result.items


# -- channel quality -------------------------------------------------------


def test_quality_rewards_substance_over_bait(db):
    good = [make_video(f"g{i}", author_id="UCgood", author="Lectures",
                       title="Lecture 4: measure theory and the Lebesgue integral",
                       description="Notes: https://arxiv.org/abs/2401.00000\n0:00 intro\n9:00 proof",
                       duration=3200) for i in range(6)]
    bad = [make_video(f"b{i}", author_id="UCbad", author="Clips",
                      title="SHOCKING thing you WON'T BELIEVE!!! 😱",
                      description="LIKE AND SUBSCRIBE #shorts", duration=45,
                      genre="Entertainment") for i in range(6)]
    seed(db, good + bad)
    assert channels.recompute_quality(db) == 2
    scored = {r["id"]: r["quality"] for r in db.query("SELECT id, quality FROM channels")}
    assert scored["UCgood"] > scored["UCbad"]


def test_consistency_separates_focused_from_scattershot(db):
    focused = [make_video(f"f{i}", author_id="UCfocus", author="Focus",
                          title=f"Kernel memory allocators, part {i}",
                          keywords=["kernel", "allocator", "memory"]) for i in range(6)]
    scattered = [
        make_video("s0", author_id="UCwide", author="Wide", title="Baking sourdough bread",
                   keywords=["baking", "bread"]),
        make_video("s1", author_id="UCwide", author="Wide", title="Restoring a vintage lathe",
                   keywords=["lathe", "restoration"]),
        make_video("s2", author_id="UCwide", author="Wide", title="Chess opening theory",
                   keywords=["chess", "openings"]),
        make_video("s3", author_id="UCwide", author="Wide", title="Roman aqueduct engineering",
                   keywords=["rome", "aqueduct"]),
    ]
    seed(db, focused + scattered)
    channels.recompute_quality(db)
    rows = {r["id"]: r["consistency"] for r in db.query("SELECT id, consistency FROM channels")}
    assert rows["UCfocus"] > rows["UCwide"]


def test_quality_does_not_disturb_manual_priority(db):
    seed(db, [make_video("v1", author_id="UC1")])
    channels.set_preference(db, "UC1", priority=4, listing="allow")
    channels.recompute_quality(db)
    policy = channels.load_policy(db, settings())
    assert policy.priority["UC1"] == 4
    assert policy.allow == {"UC1"}
    assert "UC1" in policy.quality


def test_quality_weight_moves_the_ranking(db):
    seed(db, [
        make_video("g1", author_id="UCgood", title="Lecture 2: proof of the spectral theorem",
                   description="https://arxiv.org/abs/1\n0:00 intro", duration=3000),
        make_video("b1", author_id="UCbad", title="INSANE clip you WON'T believe!!!",
                   duration=200, genre="Entertainment"),
    ])
    channels.recompute_quality(db)
    off = ranking.recommend(db, settings(weights={"channel_quality": 0.0}))
    on = ranking.recommend(db, settings(weights={"channel_quality": 3.0}))
    good_off = next(c for c in off.items if c.id == "g1").score
    good_on = next(c for c in on.items if c.id == "g1").score
    assert good_on > good_off


def test_quality_is_explained_when_it_counts(db):
    seed(db, [make_video("g1", author_id="UCgood",
                         title="Lecture 2: proof of the spectral theorem",
                         description="https://arxiv.org/abs/1\n0:00 intro", duration=3000)])
    channels.recompute_quality(db)
    result = ranking.recommend(db, settings(weights={"channel_quality": 3.0}))
    keys = {part["key"] for part in ranking.explain(result.items[0])}
    assert "channel_quality" in keys or result.items[0].components.get("channel_quality", 0) <= 0


# -- embedding backend -----------------------------------------------------


def test_default_backend_is_the_hashed_one(tmp_path):
    embedder = embed.Embedder(Config(data_dir=str(tmp_path)))
    assert embedder.provider == "hashed"
    assert embedder.available()
    vector = embedder.vector([("kernel memory allocator", 1.0)])
    assert vector and not embed.is_dense(vector)


def test_densify_normalises_and_prunes():
    vector = embed.densify([1.0, 0.0, 0.5, 0.0001, -0.8])
    assert embed.is_dense(vector)
    assert "d1" not in vector and "d3" not in vector
    norm = sum(v * v for v in vector.values()) ** 0.5
    assert norm == pytest.approx(1.0)


def test_dense_and_sparse_are_never_silently_compared():
    dense = embed.densify([1.0, 0.2, 0.9])
    sparse = T.term_vector([("kernel allocator memory", 1.0)])
    assert embed.is_dense(dense)
    assert not embed.is_dense(sparse)
    assert not embed.compatible(dense, sparse)
    assert embed.compatible(sparse, sparse)
    assert embed.compatible({}, dense)


def test_a_dead_backend_falls_back_rather_than_failing(tmp_path):
    cfg = Config(data_dir=str(tmp_path), embed_provider="ollama",
                 embed_base_url="http://127.0.0.1:9", request_timeout=1)
    embedder = embed.Embedder(cfg)
    vector = embedder.vector([("kernel memory allocator", 1.0)])
    assert vector, "a missing model must not produce an empty vector"
    assert not embed.is_dense(vector)
    embedder.close()


def test_scoring_accepts_an_injected_embedder(tmp_path):
    class Fake:
        def vector(self, parts, top_k=64):
            return embed.densify([0.9, 0.1, 0.4])

    card = scoring.score_video(make_video("v"), embedder=Fake())
    assert embed.is_dense(card.vector)


# -- migration -------------------------------------------------------------


def test_missing_columns_are_added_on_open(tmp_path):
    path = str(tmp_path / "old.db")
    Database(path).close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE scores DROP COLUMN profanity")
    raw.execute("ALTER TABLE channels DROP COLUMN quality")
    raw.commit()
    raw.close()

    applied = Database(path).migrate()
    columns = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(scores)")}
    assert "profanity" in columns
    assert applied == [] or "scores.profanity" not in applied  # already added on open


def test_a_score_row_written_before_the_migration_still_loads(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    seed(db, [make_video("v1")])
    row = dict(db.one("SELECT * FROM scores WHERE video_id = 'v1'"))
    row.pop("profanity")
    card = scoring.row_to_card(row)
    assert card["profanity"] == 0.0
    assert card["education"] > 0


def test_migrate_is_idempotent(tmp_path):
    path = str(tmp_path / "t.db")
    Database(path).close()
    assert Database(path).migrate() == []


# -- the load path ---------------------------------------------------------


def test_every_score_survives_the_load_path(db):
    """Regression: the ranking query listed score columns by hand, so a score
    added later reached the database and never reached the filter — a setting
    that silently did nothing, with no error anywhere."""
    from sieve.config import SCORE_KEYS

    seed(db, [make_video("v1")])
    rows = ranking._load_rows(db, ["v1"])
    card = scoring.row_to_card(rows["v1"])
    stored = dict(db.one("SELECT * FROM scores WHERE video_id = 'v1'"))
    for key in SCORE_KEYS:
        assert key in rows["v1"], f"{key} missing from the ranking query"
        assert card[key] == pytest.approx(stored[key]), f"{key} did not round-trip"


def test_videos_and_scores_share_no_column_names(db):
    """`SELECT v.*, s.*` is only safe while this holds."""
    video_cols = {r["name"] for r in db.query("SELECT * FROM pragma_table_info('videos')")}
    score_cols = {r["name"] for r in db.query("SELECT * FROM pragma_table_info('scores')")}
    assert not (video_cols & score_cols)
