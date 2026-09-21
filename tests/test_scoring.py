from sieve import scoring


def video(**overrides):
    base = {
        "id": "v1", "title": "A video", "author": "Someone", "author_id": "UC1",
        "published": 1_700_000_000, "duration": 900, "views": 10000, "likes": 400,
        "description": "", "keywords": [], "genre": "", "is_live": 0, "is_upcoming": 0,
        "family_safe": 1, "sub_count": 10000,
    }
    base.update(overrides)
    return base


def test_lecture_scores_higher_than_meme_on_education():
    lecture = scoring.score_video(
        video(title="Lecture 7: manifolds and charts",
              description="Problem sets: https://arxiv.org/abs/2401.00000\n0:00 intro\n8:20 proof",
              duration=3400, genre="Education"),
        "consider a smooth manifold with an atlas of charts the theorem follows from the "
        "spectral decomposition we derive the integral by parts",
    )
    meme = scoring.score_video(
        video(title="SHOCKING moments that broke the internet 😱😱",
              description="LIKE AND SUBSCRIBE #shorts", duration=42, genre="Entertainment"),
    )
    assert lecture["education"] > meme["education"]
    assert meme["brainrot"] > lecture["brainrot"]
    assert meme["clickbait"] > lecture["clickbait"]


def test_scores_are_bounded():
    for card in (scoring.score_video(video()), scoring.score_video(video(title="!" * 200))):
        for name, value in card.scores.items():
            assert 0.0 <= value <= 100.0, name


def test_dearrow_retitle_forces_clickbait_up():
    """A community retitle is direct evidence, so it must outrank the heuristic."""
    plain = video(title="An ordinary sober title about carpentry")
    without = scoring.score_video(plain)
    with_dearrow = scoring.score_video(plain, extra={"dearrow_retitled": 1.0})
    assert with_dearrow["clickbait"] >= 72.0
    assert with_dearrow["clickbait"] > without["clickbait"]


def test_sponsor_ratio_lowers_information_density():
    clean = scoring.score_video(video(duration=1200), "dense technical narration " * 60)
    heavy = scoring.score_video(video(duration=1200), "dense technical narration " * 60,
                                extra={"filler_ratio": 0.4})
    assert heavy["info_density"] < clean["info_density"]


def test_explanations_are_signed_and_named():
    card = scoring.score_video(
        video(title="INSANE trick you WON'T believe!!!", duration=50),
    )
    parts = card.explain("brainrot")
    assert parts, "a high-brainrot video should have something to say for itself"
    assert all({"label", "effect", "direction"} <= set(p) for p in parts)
    assert any(p["direction"] == "up" for p in parts)
    # labels must be human sentences, not feature identifiers
    assert all("_" not in p["label"] for p in parts)


def test_explanation_matches_the_score_direction():
    """Contributions are exact for a linear model, so their sum must agree with
    the score they explain."""
    card = scoring.score_video(video(title="Lecture 3: measure theory", duration=3000,
                                     description="0:00 intro\n5:00 proof"))
    signals = card.signals["education"]
    total = sum(s.contribution for s in signals)
    assert (total > 0) == (card["education"] > 50)


def test_roundtrip_through_the_database_row():
    card = scoring.score_video(video(title="Forging a chisel from a leaf spring"),
                               "i heated the steel to orange and drew it out on the anvil")
    row = scoring.card_to_row(card)
    columns = ["video_id", "version", "education", "entertainment", "stimulation", "brainrot", "clickbait", "info_density", "technical_depth", "production", "ai_generated", "nsfw", "music", "profanity", "topics", "vector", "signals", "computed_at"]
    assert len(columns) == len(row), "column list and row tuple must stay in step"
    restored = scoring.row_to_card(dict(zip(columns, row, strict=True)))
    assert restored.scores == card.scores
    assert restored.topics == card.topics
    assert restored.explain("education") == card.explain("education")


def test_music_topic_channel_is_detected():
    card = scoring.score_video(video(title="Night Drive", author="Some Artist - Topic",
                                     genre="Music", duration=230))
    assert card["music"] > 60
