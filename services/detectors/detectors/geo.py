import math

EARTH_RADIUS_M = 6_371_000.0


# great circle distance in metres
def haversine_m(lat1, lng1, lat2, lng2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


# bearing from point 1 to point 2 with 0 as north
def bearing_deg(lat1, lng1, lat2, lng2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lng2 - lng1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return math.degrees(math.atan2(y, x)) % 360.0


# smallest gap between two angles from 0 to 180
def angle_diff_deg(a, b):
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


# distance from p to segment a b
def point_to_segment_m(p, a, b):
    return project_onto_segment(p, a, b)[1]


# t is 0 at a and 1 at b plus distance from that point
def project_onto_segment(p, a, b):
    # flat projection around p is fine at city scale
    cos_lat = math.cos(math.radians(p[0]))
    m_per_deg = math.radians(1) * EARTH_RADIUS_M

    def project(q):
        return (q[1] - p[1]) * m_per_deg * cos_lat, (q[0] - p[0]) * m_per_deg

    ax, ay = project(a)
    bx, by = project(b)
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return 0.0, math.hypot(ax, ay)
    # p is the origin and t is clamped to the segment
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
    return t, math.hypot(ax + t * dx, ay + t * dy)


# closest segment index distance and bearing
def nearest_segment(point, path):
    if len(path) < 2:
        raise ValueError("path needs at least 2 points")
    best_i, best_d = 0, math.inf
    for i in range(len(path) - 1):
        d = point_to_segment_m(point, path[i], path[i + 1])
        if d < best_d:
            best_i, best_d = i, d
    a, b = path[best_i], path[best_i + 1]
    return best_i, best_d, bearing_deg(a[0], a[1], b[0], b[1])


# distance along the path at each point and the last one is the path length
def cumulative_m(path):
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    return cum


# closest point as distance along the path and distance away
# lo and hi limit the search so a looping route matches the right pass
def project_onto_path(point, path, cum, lo_m=None, hi_m=None):
    if len(path) < 2:
        raise ValueError("path needs at least 2 points")
    lo = -math.inf if lo_m is None else lo_m
    hi = math.inf if hi_m is None else hi_m
    best_along, best_d = 0.0, math.inf
    for i in range(len(path) - 1):
        start, end = cum[i], cum[i + 1]
        if end < lo or start > hi:
            continue
        t, d = project_onto_segment(point, path[i], path[i + 1])
        along = start + t * (end - start)
        if along < lo or along > hi:
            # clamp to the part of the segment that is in range
            along = min(max(along, lo, start), hi, end)
            t = 0.0 if end == start else (along - start) / (end - start)
            a, b = path[i], path[i + 1]
            q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            d = haversine_m(point[0], point[1], q[0], q[1])
        if d < best_d:
            best_along, best_d = along, d
    if best_d == math.inf:  # range missed the path so search all of it
        return project_onto_path(point, path, cum)
    return best_along, best_d
