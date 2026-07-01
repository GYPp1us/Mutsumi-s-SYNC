from src.mutsumi_sync.lab.reporting import RunReport


def test_run_report_tracks_tokens_and_cache():
    report = RunReport()
    report.add_usage(input_tokens=10, output_tokens=3, cache_hit=7, cache_miss=3)
    assert report.input_tokens == 10
    assert report.output_tokens == 3
    assert report.cache_hit_tokens == 7
    assert report.cache_miss_tokens == 3


def test_run_report_tracks_content_bridge_count():
    report = RunReport()
    report.add_bridged_content("hello")
    assert report.bridged_contents == 1
