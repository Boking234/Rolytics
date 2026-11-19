def test_weekly_noise_scales_with_game_size():
    # A 4-player-a-day game swings ~40% week to week by chance: not "players worse".
    from roblox_agent import weekly
    assert weekly._weekly_noise("DailyActiveUsers", 4, None) > 0.39
    assert weekly._weekly_noise("DailyActiveUsers", 16000, None) < 0.15
    assert weekly._weekly_noise("ClientCrashRate15m", 4, None) == 0.0  # no public evidence: old rule
