"""routing.py: risk-aware walking route planner. A lattice over the area is scored from the SafetyGraph (zones, time of
day, recent alerts) plus OpenStreetMap signals (lighting, open venues, nightlife) and police proximity; Dijkstra finds
the lowest risk-weighted route."""
from __future__ import annotations

import math

import networkx as nx

from graph_store import haversine_m
from osm_data import OsmRisk

WALK_MPS = 1.4
M_PER_DEG = 111_320.0


def _densify(pts, step_m):
    out = [pts[0]]
    for (a, b), (c, d) in zip(pts, pts[1:]):
        n = max(1, int(haversine_m(a, b, c, d) // step_m))
        for i in range(1, n + 1):
            f = i / n
            out.append((a + (c - a) * f, b + (d - b) * f))
    return out


class _Scorer:
    def __init__(self, graph, hour, osm_risk):
        self.graph, self.hour, self.osm = graph, hour, osm_risk

    def point(self, lat, lng):
        lr = self.graph.location_risk(lat, lng, self.hour)
        delta, flags = self.osm.adjust(lat, lng) if self.osm else (0.0, set())
        return max(0.0, min(1.0, lr["score"] + delta)), lr["zone"], flags


def _near(items, dense, limit_m):
    out = []
    for s in items:
        dmin = min(haversine_m(lat, lng, s["lat"], s["lng"]) for lat, lng in dense)
        if dmin <= limit_m:
            out.append({"id": s["id"], "name": s["name"], "min_dist_m": round(dmin)})
    out.sort(key=lambda p: p["min_dist_m"])
    return out


def _metrics(scorer, pts, stations, hospitals, step_m=50.0) -> dict:
    dense = _densify(pts, step_m)
    risks, zones, unlit, isolated, busy = [], [], 0, 0, 0
    for lat, lng in dense:
        s, z, flags = scorer.point(lat, lng)
        risks.append(s)
        if z and (not zones or zones[-1]["id"] != z["id"]):
            zones.append({"id": z["id"], "name": z["name"]})
        unlit += "unlit" in flags
        isolated += "isolated" in flags
        busy += "busy" in flags
    dist = sum(haversine_m(a, b, c, d) for (a, b), (c, d) in zip(pts, pts[1:]))
    return {"distance_m": round(dist), "eta_min": round(dist / WALK_MPS / 60, 1),
            "exposure": round(sum(risks) / len(risks), 3),
            "high_risk_m": int(sum(1 for r in risks if r >= 0.6) * step_m),
            "unlit_m": int(unlit * step_m), "isolated_m": int(isolated * step_m), "busy_m": int(busy * step_m),
            "zones": zones, "police_posts": _near(stations, dense, 300), "hospitals": _near(hospitals, dense, 500)}


def plan_route(graph, origin, dest, local_hour=None, safety_bias: float = 0.7, max_cells: int = 34, osm=None) -> dict:
    """origin/dest: (lat, lng). safety_bias 0 = shortest path, 1 = strongest risk avoidance. osm: data from osm_data."""
    (lat0, lng0), (lat1, lng1) = origin, dest
    stations = graph.nearest("STATION", lat0, lng0, k=200)
    hospitals = graph.nearest("HOSPITAL", lat0, lng0, k=200)
    scorer = _Scorer(graph, local_hour, OsmRisk(osm, local_hour) if osm else None)
    kx = M_PER_DEG * math.cos(math.radians((lat0 + lat1) / 2))
    margin = 700.0
    minlat, maxlat = min(lat0, lat1) - margin / M_PER_DEG, max(lat0, lat1) + margin / M_PER_DEG
    minlng, maxlng = min(lng0, lng1) - margin / kx, max(lng0, lng1) + margin / kx
    dy, dx = (maxlat - minlat) * M_PER_DEG, (maxlng - minlng) * kx
    cell = max(60.0, max(dx, dy) / max_cells)
    rows, cols = int(dy // cell) + 1, int(dx // cell) + 1

    def center(r, c):
        return (minlat + r * cell / M_PER_DEG, minlng + c * cell / kx)

    def cell_of(lat, lng):
        return (min(rows - 1, max(0, round((lat - minlat) * M_PER_DEG / cell))),
                min(cols - 1, max(0, round((lng - minlng) * kx / cell))))

    risk, bonus = {}, {}
    for r in range(rows):
        for c in range(cols):
            lat, lng = center(r, c)
            risk[(r, c)] = scorer.point(lat, lng)[0]
            d = min((haversine_m(lat, lng, s["lat"], s["lng"]) for s in stations), default=1e9)
            bonus[(r, c)] = max(0.0, 1 - d / 400)  # cells within 400 m of a police station get cheaper
    w_risk = 1 + 7 * safety_bias
    G = nx.Graph()
    for r in range(rows):
        for c in range(cols):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                r2, c2 = r + dr, c + dc
                if 0 <= r2 < rows and 0 <= c2 < cols:
                    seg = haversine_m(*center(r, c), *center(r2, c2))
                    rk = (risk[(r, c)] + risk[(r2, c2)]) / 2
                    bn = (bonus[(r, c)] + bonus[(r2, c2)]) / 2
                    G.add_edge((r, c), (r2, c2), w=seg * (1 + w_risk * rk) * (1 - 0.4 * bn))
    path = nx.shortest_path(G, cell_of(lat0, lng0), cell_of(lat1, lng1), weight="w")
    safe_pts = [origin] + [center(*n) for n in path[1:-1]] + [dest]
    direct_pts = [origin, dest]
    safe = _metrics(scorer, safe_pts, stations, hospitals)
    direct = _metrics(scorer, direct_pts, stations, hospitals)
    extra = round(100 * (safe["distance_m"] - direct["distance_m"]) / max(1, direct["distance_m"]))
    avoided = max(0, direct["high_risk_m"] - safe["high_risk_m"])
    summary = (f"Safe route {safe['distance_m']} m (~{safe['eta_min']} min), {extra:+d}% vs direct. Average risk "
               f"{safe['exposure']:.2f} vs {direct['exposure']:.2f}; poorly lit {safe['unlit_m']} m vs {direct['unlit_m']} m; "
               f"isolated {safe['isolated_m']} m vs {direct['isolated_m']} m; avoids {avoided} m of high-risk streets; "
               f"passes {len(safe['police_posts'])} police post(s) within 300 m.")
    return {"origin": list(origin), "dest": list(dest), "waypoints": [list(p) for p in safe_pts],
            "direct_waypoints": [list(p) for p in direct_pts], "safe": safe, "direct": direct,
            "extra_pct": extra, "summary": summary, "safety_bias": safety_bias}