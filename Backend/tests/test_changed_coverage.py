from scripts.coverage_threshold import meets_changed_line_coverage


def test_changed_line_coverage_requires_ninety_percent():
    assert not meets_changed_line_coverage(89, 100)
    assert meets_changed_line_coverage(90, 100)
    assert meets_changed_line_coverage(9, 10)
    assert not meets_changed_line_coverage(0, 0)
