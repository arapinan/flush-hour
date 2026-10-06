"""Distance helpers. Standard library only.

Manhattan's street grid is rotated about 29 degrees from true north, so the
real walking distance between two points is closer to the sum of the legs
measured along the avenues and the cross streets than to a straight line.
Outside Manhattan the grid differs, so every walking figure here is an estimate.
"""

import math

EARTH_RADIUS_M = 6_371_000
GRID_ANGLE_DEG = 29.0
WALK_M_PER_MIN = 80  # ~4.8 km/h

# Rough bounding box around the five boroughs.
NYC_BOUNDS = (40.49, 40.92, -74.27, -73.68)  # lat_min, lat_max, lon_min, lon_max

Point = tuple[float, float]  # (lat, lon)


def in_nyc(p: Point) -> bool:
    """Is p inside the bounding box? (The box also catches a little of New Jersey.)"""
    lat_min, lat_max, lon_min, lon_max = NYC_BOUNDS
    return lat_min <= p[0] <= lat_max and lon_min <= p[1] <= lon_max


def haversine_m(a: Point, b: Point) -> float:
    """Straight-line (great-circle) distance in meters."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def to_xy(origin: Point, p: Point) -> tuple[float, float]:
    """Local flat-earth meters (east, north) of p relative to origin."""
    x = math.radians(p[1] - origin[1]) * EARTH_RADIUS_M * math.cos(math.radians(origin[0]))
    y = math.radians(p[0] - origin[0]) * EARTH_RADIUS_M
    return x, y


def grid_distance_m(a: Point, b: Point) -> float:
    """Walking-distance estimate: legs along avenues plus legs along cross streets."""
    dx, dy = to_xy(a, b)
    # Rotate the east/north offsets onto the grid's own axes, then walk one leg along each
    t = math.radians(GRID_ANGLE_DEG)
    along_avenue = dx * math.sin(t) + dy * math.cos(t)
    along_street = dx * math.cos(t) - dy * math.sin(t)
    return abs(along_avenue) + abs(along_street)


def walk_minutes(meters: float) -> int:
    """Walking time rounded up to whole minutes, never less than 1."""
    return max(1, math.ceil(meters / WALK_M_PER_MIN))


def route_position(start: Point, end: Point, p: Point) -> float:
    """Fraction (0..1) of the way along start->end that p projects onto."""
    bx, by = to_xy(start, end)
    px, py = to_xy(start, p)
    length_sq = bx * bx + by * by
    if length_sq == 0:
        return 0.0
    # Dot product over length squared, clamped so points before the start or past the end still count
    return max(0.0, min(1.0, (px * bx + py * by) / length_sq))


def detour_m(start: Point, end: Point, stop: Point) -> float:
    """Extra walking distance for visiting `stop` on the way from start to end."""
    return max(0.0, grid_distance_m(start, stop) + grid_distance_m(stop, end) - grid_distance_m(start, end))
