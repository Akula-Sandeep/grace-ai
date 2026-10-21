from app.services.music_versions import composition_key, group_versions, version_kind


def test_version_kinds():
    assert version_kind("Naatu Naatu") == "soundtrack"
    assert version_kind("Naatu Naatu (Reprise)") == "reprise"
    assert version_kind("Naatu Naatu - Club Remix") == "remix"
    assert version_kind("Dosti (Hindi Version)") == "language_version"
    assert version_kind("Main Theme (Instrumental)") == "instrumental"
    assert version_kind("Title Song Promo") == "promotional"


def test_composition_key_strips_qualifiers():
    assert composition_key('Naatu Naatu (From "RRR")') == "naatu naatu"
    assert composition_key("Naatu Naatu - Remix") == "naatu naatu"
    assert composition_key("Komuram Bheemudo ft. Kaala Bhairava") == "komuram bheemudo"


def test_grouping_only_merges_matching_titles():
    groups = group_versions(
        {
            "Telugu": [
                {"title": "Dosti", "singers": ["Hemachandra"]},
                {"title": "Naatu Naatu", "singers": ["Rahul Sipligunj"]},
                {"title": "Naatu Naatu (Reprise)", "singers": []},
            ],
            "Hindi": [
                {"title": "Dosti", "singers": ["Amit Trivedi"]},
                {"title": "Naacho Naacho", "singers": ["Vishal Mishra"]},
            ],
        }
    )
    by_name = {g["composition"]: g for g in groups}
    dosti = by_name["Dosti"]
    assert dosti["languages"] == ["Hindi", "Telugu"]
    assert [v["kind"] for v in dosti["versions"]] == ["soundtrack", "language_version"]
    naatu = by_name["Naatu Naatu"]
    assert [v["kind"] for v in naatu["versions"]] == ["soundtrack", "reprise"]
    # Different titles are not merged without evidence they are the same song.
    assert by_name["Naacho Naacho"]["grouping"] == "single"
