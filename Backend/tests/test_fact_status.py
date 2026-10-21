from app.services.fact_status import classify, classify_facts, detect_conflicts


def _src(domain, text=""):
    return {"url": f"https://{domain}/a", "domain": domain, "excerpt": text}


def test_status_ladder():
    assert classify([]) == "unavailable"
    assert classify([_src("oscars.org")]) == "verified"
    assert classify([], structured=True, structured_primary=True) == "verified"
    assert classify([_src("variety.com"), _src("someblog.net")]) == "strongly_supported"
    assert classify([_src("a.net"), _src("b.net"), _src("c.net")]) == "strongly_supported"
    assert classify([_src("someblog.net")]) == "reported"
    assert classify([_src("variety.com")], hedged=True) == "uncertain"
    assert classify([_src("oscars.org")], conflicting=True) == "conflicting"


def test_numeric_conflicts_are_preserved_with_their_sources():
    conflicts = detect_conflicts(
        [
            _src("variety.com", "The film had a budget of $165 million."),
            _src("someblog.net", "It was made on a budget of $200 million."),
            _src("other.net", "Budgeted at $165 million, it was a gamble."),
        ]
    )
    assert [c["topic"] for c in conflicts] == ["budget"]
    values = conflicts[0]["values"]
    assert [v["sources"] for v in values] == [["variety.com", "other.net"], ["someblog.net"]]
    assert values[1]["claim"] == "budget of $200 million"


def test_matching_figures_are_not_a_conflict():
    assert (
        detect_conflicts([_src("a.net", "a budget of $165 million"), _src("b.net", "budget of $165 million")])
        == []
    )


def test_facts_cluster_independent_support_and_flag_hedges():
    result = classify_facts(
        [
            _src("variety.com", "Principal photography took place in Alberta, Canada during 2013."),
            _src("someblog.net", "Principal photography took place in Alberta Canada in 2013."),
            _src("rumours.net", "The director reportedly wanted a different ending."),
        ]
    )
    statuses = {fact["text"][:20]: fact["status"] for fact in result["facts"]}
    assert statuses["Principal photograph"] == "strongly_supported"
    assert statuses["The director reporte"] == "uncertain"
    supported = next(f for f in result["facts"] if f["status"] == "strongly_supported")
    assert supported["supporting_sources"] == ["someblog.net", "variety.com"]


def test_categorical_location_and_director_disagreements_are_surfaced():
    conflicts = detect_conflicts(
        [
            _src("variety.com", "The desert scenes were filmed in Wadi Rum, Jordan, over six weeks."),
            _src("locationsdb.net", "Most of the film was shot in Abu Dhabi."),
            _src("other.org", "It was shot on location in Jordan and Budapest."),
        ]
    )
    location = next(c for c in conflicts if c["topic"] == "filming location")
    claims = {tuple(v["sources"]): v["claim"] for v in location["values"]}
    assert claims[("locationsdb.net",)] == "Abu Dhabi"
    assert "Wadi Rum, Jordan" in claims.values()


def test_overlapping_or_extra_places_are_not_conflicts():
    assert not [
        c
        for c in detect_conflicts(
            [_src("a.com", "It was filmed in Jordan."), _src("b.com", "Shot in Jordan and Budapest.")]
        )
        if c["topic"] == "filming location"
    ]


def test_release_year_and_director_conflicts():
    topics = {
        c["topic"]
        for c in detect_conflicts(
            [
                _src("a.com", "The film was released in 2014 and directed by Christopher Nolan."),
                _src("b.com", "It was released worldwide in 2015, directed by Jonathan Nolan."),
            ]
        )
    }
    assert {"release year", "director"} <= topics
