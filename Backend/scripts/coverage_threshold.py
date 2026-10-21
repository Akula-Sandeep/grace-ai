CHANGED_LINE_COVERAGE_MINIMUM = 90


def meets_changed_line_coverage(covered: int, total: int) -> bool:
    return total > 0 and covered * 100 >= CHANGED_LINE_COVERAGE_MINIMUM * total
