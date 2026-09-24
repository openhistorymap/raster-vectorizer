from pathlib import Path


def test_list_worlds_finds_seeded(ofm_root: Path):
    from backend import worlds

    rows = worlds.list_worlds()
    slugs = {r["slug"] for r in rows}
    assert {"fakeworld", "anotherworld"} <= slugs
    # the empty-timeline world should NOT appear
    assert "broken" not in slugs


def test_list_worlds_records_mode_and_base(ofm_root: Path):
    from backend import worlds

    by_slug = {r["slug"]: r for r in worlds.list_worlds()}
    assert by_slug["fakeworld"]["mode"] == "geojson"
    assert by_slug["anotherworld"]["mode"] == "spatialite"
    assert by_slug["fakeworld"]["base"] == {"zoom": 4, "lat": 0, "lng": 0}


def test_get_world_includes_map_and_gaia(ofm_root: Path):
    from backend import worlds

    rec = worlds.get_world("fakeworld")
    assert rec["timeline"]["name"] == "Fakeworld"
    assert rec["gaia"]["sources"] == ["parks", "roads"]
    assert rec["map"]["version"] == 8


def test_world_dir_raises_for_unknown(ofm_root: Path):
    from backend import worlds

    import pytest

    with pytest.raises(FileNotFoundError):
        worlds.world_dir("nope")
