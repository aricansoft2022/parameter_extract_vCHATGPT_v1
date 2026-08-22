from types import SimpleNamespace

from parameter_extract.exit_query import QueryReplayStats
from parameter_extract.work_profile import QueryWorkProfile


def test_query_work_profile_counts_logical_replay_work_deterministically():
    profile = QueryWorkProfile()
    windows = [
        {"trade_count": 2, "open_at_end": True},
        {"trade_count": 0, "open_at_end": False},
    ]
    query_windows = [
        SimpleNamespace(funding=(object(), object(), object())),
        SimpleNamespace(funding=(object(),)),
    ]

    profile.record(
        windows,
        query_windows,
        replay_stats=QueryReplayStats(funding_range_bisects=6, funding_event_checks=2),
    )
    assert profile.as_dict() == {
        "candidate_evaluations": 1,
        "candidate_window_replays": 2,
        "accepted_positions": 3,
        "closed_trades": 2,
        "open_positions": 1,
        "exit_lookup_requests": 3,
        "excursion_range_requests": 3,
        "funding_range_bisects": 6,
        "funding_event_checks": 2,
        "closed_trade_signal_bisects": 2,
    }

    profile.record(
        [{"trade_count": 1, "open_at_end": False}],
        [SimpleNamespace(funding=())],
        replay_stats=QueryReplayStats(),
    )
    assert profile.candidate_evaluations == 2
    assert profile.candidate_window_replays == 3
    assert profile.accepted_positions == 4
    assert profile.closed_trades == 3
    assert profile.open_positions == 1
    assert profile.funding_range_bisects == 6
    assert profile.funding_event_checks == 2
    assert profile.closed_trade_signal_bisects == 3
