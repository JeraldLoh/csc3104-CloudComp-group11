import pytest

from detectors.geo import (
    angle_diff_deg,
    bearing_deg,
    cumulative_m,
    haversine_m,
    nearest_segment,
    point_to_segment_m,
    project_onto_path,
)

# Punggol Interchange, Singapore
BUS = (1.4052, 103.9024)


def test_haversine_zero_and_known_distance():
    assert haversine_m(*BUS, *BUS) == 0
    # 0.001 deg of latitude is ~111.2 m anywhere
    assert haversine_m(1.404, 103.901, 1.405, 103.901) == pytest.approx(111.2, abs=0.5)
    # Orchard to Changi Airport T2, roughly 15-16 km
    assert 14_000 < haversine_m(1.3048, 103.8318, 1.3554, 103.9915) < 19_000


def test_haversine_is_symmetric():
    assert haversine_m(*BUS, 1.39, 103.89) == pytest.approx(haversine_m(1.39, 103.89, *BUS))


@pytest.mark.parametrize(
    "dest, expected",
    [
        ((1.5, 103.9), 0),  # due north
        ((1.4, 104.0), 90),  # due east
        ((1.3, 103.9), 180),  # due south
        ((1.4, 103.8), 270),  # due west
    ],
)
def test_bearing_cardinals(dest, expected):
    assert bearing_deg(1.4, 103.9, *dest) == pytest.approx(expected, abs=0.1)


def test_bearing_range_is_half_open():
    for lat, lng in [(1.5, 103.9), (1.3, 103.9), (1.4, 103.8), (1.4, 104.0), (1.5, 103.8)]:
        assert 0 <= bearing_deg(1.4, 103.9, lat, lng) < 360


@pytest.mark.parametrize(
    "a, b, expected",
    [
        (350, 10, 20),  # wrap-around
        (10, 350, 20),
        (0, 180, 180),
        (90, 90, 0),
        (0, 360, 0),
        (-10, 10, 20),
        (720, 5, 5),
    ],
)
def test_angle_diff(a, b, expected):
    assert angle_diff_deg(a, b) == pytest.approx(expected)


def test_point_on_segment_is_zero():
    mid = ((1.404 + 1.405) / 2, 103.901)
    assert point_to_segment_m(mid, (1.404, 103.901), (1.405, 103.901)) == pytest.approx(0, abs=1e-6)
    assert point_to_segment_m((1.404, 103.901), (1.404, 103.901), (1.405, 103.901)) == pytest.approx(0, abs=1e-6)


def test_point_perpendicular_to_segment():
    # Segment runs north-south; point is 0.001 deg east of its middle (~111 m at this latitude)
    d = point_to_segment_m((1.4045, 103.902), (1.404, 103.901), (1.405, 103.901))
    assert d == pytest.approx(haversine_m(1.4045, 103.902, 1.4045, 103.901), rel=0.01)


def test_point_past_segment_end_measures_to_endpoint():
    p = (1.406, 103.901)  # ~111 m north of the segment end
    d = point_to_segment_m(p, (1.404, 103.901), (1.405, 103.901))
    assert d == pytest.approx(haversine_m(*p, 1.405, 103.901), rel=0.01)


def test_zero_length_segment():
    d = point_to_segment_m((1.405, 103.901), (1.404, 103.901), (1.404, 103.901))
    assert d == pytest.approx(111.2, abs=0.5)


PATH = [(1.404, 103.901), (1.405, 103.901), (1.405, 103.902)]  # north, then east


def test_nearest_segment_picks_closest_and_reports_bearing():
    idx, dist, brg = nearest_segment((1.4051, 103.9015), PATH)
    assert idx == 1
    assert dist == pytest.approx(11.1, abs=0.5)
    assert brg == pytest.approx(90, abs=0.1)

    idx, dist, brg = nearest_segment((1.4045, 103.9011), PATH)
    assert idx == 0
    assert brg == pytest.approx(0, abs=0.1)


def test_nearest_segment_point_on_path():
    idx, dist, _ = nearest_segment((1.4045, 103.901), PATH)
    assert idx == 0
    assert dist == pytest.approx(0, abs=1e-6)


def test_nearest_segment_far_past_end_uses_last_segment():
    idx, dist, _ = nearest_segment((1.405, 103.903), PATH)
    assert idx == 1
    assert dist == pytest.approx(haversine_m(1.405, 103.903, 1.405, 103.902), rel=0.01)


def test_nearest_segment_tie_goes_to_earliest():
    # The shared vertex is the closest point to both segments
    idx, _, _ = nearest_segment((1.4055, 103.9005), PATH)
    assert idx == 0


@pytest.mark.parametrize("path", [[], [(1.4, 103.9)]])
def test_nearest_segment_short_path_raises(path):
    with pytest.raises(ValueError):
        nearest_segment((1.4, 103.9), path)


# 0.001 deg of latitude is ~111.2 m: a path 222 m north, then back south to 111 m
LOOP = [(1.400, 103.9), (1.402, 103.9), (1.401, 103.9)]


def test_cumulative_m():
    cum = cumulative_m(LOOP)
    assert cum[0] == 0
    assert cum[1] == pytest.approx(222.4, abs=0.5)
    assert cum[2] == pytest.approx(333.6, abs=0.5)


def test_project_onto_path_gives_distance_along_and_away():
    cum = cumulative_m(LOOP[:2])
    along, away = project_onto_path((1.401, 103.9009), LOOP[:2], cum)
    assert along == pytest.approx(111.2, abs=0.5)
    assert away == pytest.approx(100, abs=1)


def test_project_onto_path_range_picks_the_pass():
    cum = cumulative_m(LOOP)
    p = (1.4015, 103.9)  # on both the way out and the way back
    assert project_onto_path(p, LOOP, cum)[0] == pytest.approx(166.8, abs=0.5)  # ties go to the earliest
    along, away = project_onto_path(p, LOOP, cum, lo_m=230)
    assert along == pytest.approx(278.0, abs=0.5) and away == pytest.approx(0, abs=0.5)


def test_project_onto_path_range_clamps_and_falls_back():
    cum = cumulative_m(LOOP[:2])
    along, away = project_onto_path((1.402, 103.9), LOOP[:2], cum, hi_m=100)
    assert along == pytest.approx(100) and away == pytest.approx(122.4, abs=0.5)
    # a range off the end of the path searches the whole path
    assert project_onto_path((1.401, 103.9), LOOP[:2], cum, lo_m=5000)[0] == pytest.approx(111.2, abs=0.5)


def test_project_onto_path_needs_two_points():
    with pytest.raises(ValueError):
        project_onto_path((1.4, 103.9), [(1.4, 103.9)], [0.0])
