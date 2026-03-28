"""Quality ranking algorithm for discovered podcasts."""

import math
from datetime import datetime, timezone

from music_corn.discovery.models import PodcastResult


def compute_quality_rank(podcast: PodcastResult) -> float:
    """Compute a quality rank (0.0-1.0) from available signals.

    Weights:
        listen_score:    0.35  (most reliable aggregate signal)
        recency:         0.25  (penalize stale podcasts)
        episode_count:   0.15  (consistency/longevity signal)
        itunes_rating:   0.15  (user quality signal)
        review_volume:   0.10  (credibility of rating)

    Missing signals are skipped and weights renormalized.
    """
    signals: list[float] = []
    weights: list[float] = []

    # ListenNotes score (0-100, most reliable)
    if podcast.listen_score is not None and podcast.listen_score > 0:
        signals.append(podcast.listen_score / 100.0)
        weights.append(0.35)

    # Recency: linear decay over 12 months
    if podcast.latest_episode_at is not None:
        now = datetime.now(timezone.utc)
        days_since = (now - podcast.latest_episode_at).days
        months_since = days_since / 30.0
        recency = max(0.0, 1.0 - (months_since / 12.0))
        signals.append(recency)
        weights.append(0.25)

    # Episode count (log-scaled, consistency signal)
    if podcast.episode_count is not None and podcast.episode_count > 0:
        # log10(1000) = 3 → score of 1.0
        signals.append(min(math.log10(podcast.episode_count) / 3.0, 1.0))
        weights.append(0.15)

    # iTunes rating (0-5)
    if podcast.itunes_rating is not None and podcast.itunes_rating > 0:
        signals.append(podcast.itunes_rating / 5.0)
        weights.append(0.15)

    # Review volume (log-scaled credibility)
    if podcast.itunes_review_count is not None and podcast.itunes_review_count > 0:
        signals.append(min(math.log10(podcast.itunes_review_count) / 3.0, 1.0))
        weights.append(0.10)

    # Trending bonus (small boost)
    if podcast.podcastindex_trending is not None and podcast.podcastindex_trending > 0:
        signals.append(min(podcast.podcastindex_trending / 100.0, 1.0))
        weights.append(0.05)

    if not weights:
        return 0.0

    total_weight = sum(weights)
    return sum(s * w for s, w in zip(signals, weights)) / total_weight
