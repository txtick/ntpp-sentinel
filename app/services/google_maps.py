"""
Google Maps backend service — server-side only.

Uses GOOGLE_MAPS_SERVER_API_KEY for:
  - Routes API v2  (driving distance/time/polyline, waypoint optimization)
  - Geocoding API  (address → lat/lng)
  - Maps Static API (printable route images)

The server key is NEVER returned in API responses, logged, or exposed to the frontend.
Use GOOGLE_MAPS_BROWSER_API_KEY for any browser-side map rendering (separate key).
"""

import hashlib
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

_logger = logging.getLogger("google_maps")

# ── Key access ────────────────────────────────────────────────────────────────

ROUTES_BASE = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODING_BASE = "https://maps.googleapis.com/maps/api/geocode/json"
STATIC_MAP_BASE = "https://maps.googleapis.com/maps/api/staticmap"

COORD_PRECISION = 5   # decimal places for cache key normalisation
TRAFFIC_MODE_TO_ROUTING_PREFERENCE = {
    "unaware": "TRAFFIC_UNAWARE",
    "aware": "TRAFFIC_AWARE",
    "aware_optimal": "TRAFFIC_AWARE_OPTIMAL",
}


def cache_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("GOOGLE_MAPS_CACHE_TTL_DAYS", "30")))
    except ValueError:
        return 30


def optimization_enabled() -> bool:
    return os.getenv("GOOGLE_MAPS_ENABLE_OPTIMIZATION", "false").strip().lower() in {"1", "true", "yes", "on"}


def traffic_mode() -> str:
    value = os.getenv("GOOGLE_MAPS_TRAFFIC_MODE", "unaware").strip().lower()
    return value if value in TRAFFIC_MODE_TO_ROUTING_PREFERENCE else "unaware"


def routing_preference() -> str:
    return TRAFFIC_MODE_TO_ROUTING_PREFERENCE[traffic_mode()]


def server_configured() -> bool:
    return bool(_server_key())


def browser_configured() -> bool:
    return bool(_browser_key())


def browser_api_key() -> str:
    """Return the browser key. Safe to expose to the frontend."""
    return _browser_key()


def _server_key() -> str:
    """Return the server key. Never log or return to callers outside this module."""
    return os.getenv("GOOGLE_MAPS_SERVER_API_KEY", "").strip()


def _browser_key() -> str:
    """Return the browser key. Safe to expose only if the frontend actively needs it."""
    return os.getenv("GOOGLE_MAPS_BROWSER_API_KEY", "").strip()


# ── Coordinate helpers ────────────────────────────────────────────────────────

def coord_hash(lat: float, lng: float) -> str:
    lat_r = round(float(lat), COORD_PRECISION)
    lng_r = round(float(lng), COORD_PRECISION)
    return hashlib.sha256(f"{lat_r},{lng_r}".encode()).hexdigest()[:16]


def segment_hashes(
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
) -> Tuple[str, str]:
    return coord_hash(origin_lat, origin_lng), coord_hash(dest_lat, dest_lng)


# ── Duration parsing ──────────────────────────────────────────────────────────

def _parse_duration(val: Any) -> int:
    """Parse a proto Duration string like '3600s' or '3600.5s' to int seconds."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).rstrip("s").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(address: str) -> Optional[Dict[str, Any]]:
    """
    Geocode an address. Returns {lat, lng, formatted_address} or None on failure.
    Raises RuntimeError if the server key is not configured.
    """
    if not server_configured():
        raise RuntimeError("GOOGLE_MAPS_SERVER_API_KEY is not configured")
    try:
        resp = httpx.get(
            GEOCODING_BASE,
            params={"address": address, "key": _server_key()},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK" or not data.get("results"):
            _logger.warning("Geocode returned status=%s", data.get("status"))
            return None
        result = data["results"][0]
        loc = result["geometry"]["location"]
        return {
            "lat": loc["lat"],
            "lng": loc["lng"],
            "formatted_address": result.get("formatted_address", address),
        }
    except Exception:
        _logger.exception("Geocode error")
        return None


# ── Routes API ────────────────────────────────────────────────────────────────

def compute_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    intermediates: Optional[List[Dict]] = None,
    optimize_waypoint_order: bool = False,
    travel_mode: str = "DRIVE",
    route_traffic_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call Google Routes API v2.

    Returns:
        {
          distance_meters, duration_seconds, polyline,
          optimized_waypoint_order,  # list[int] indices into intermediates
          legs: [{distance_meters, duration_seconds, polyline}],
          error,  # present only on failure
        }
    """
    if not server_configured():
        raise RuntimeError("GOOGLE_MAPS_SERVER_API_KEY is not configured")
    configured_traffic_mode = route_traffic_mode if route_traffic_mode in TRAFFIC_MODE_TO_ROUTING_PREFERENCE else traffic_mode()

    body: Dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin_lat, "longitude": origin_lng}}},
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": travel_mode,
        "routingPreference": TRAFFIC_MODE_TO_ROUTING_PREFERENCE[configured_traffic_mode],
        "computeAlternativeRoutes": False,
        "languageCode": "en-US",
        "units": "IMPERIAL",
    }

    if intermediates:
        body["intermediates"] = intermediates
        if optimize_waypoint_order:
            body["optimizeWaypointOrder"] = True

    field_mask = (
        "routes.distanceMeters,"
        "routes.duration,"
        "routes.polyline.encodedPolyline,"
        "routes.optimizedIntermediateWaypointIndex,"
        "routes.legs.distanceMeters,"
        "routes.legs.duration,"
        "routes.legs.polyline.encodedPolyline"
    )

    try:
        resp = httpx.post(
            ROUTES_BASE,
            json=body,
            headers={
                "X-Goog-Api-Key": _server_key(),
                "X-Goog-FieldMask": field_mask,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        _logger.error("Routes API HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
        return {"error": f"HTTP {exc.response.status_code}", "distance_meters": 0, "duration_seconds": 0}
    except Exception:
        _logger.exception("Routes API request failed")
        return {"error": "Request failed", "distance_meters": 0, "duration_seconds": 0}

    routes = data.get("routes") or []
    if not routes:
        return {"error": "No route found", "distance_meters": 0, "duration_seconds": 0}

    route = routes[0]

    legs = []
    for leg in route.get("legs") or []:
        legs.append({
            "distance_meters": leg.get("distanceMeters") or 0,
            "duration_seconds": _parse_duration(leg.get("duration")),
            "polyline": (leg.get("polyline") or {}).get("encodedPolyline", ""),
        })

    return {
        "distance_meters": route.get("distanceMeters") or 0,
        "duration_seconds": _parse_duration(route.get("duration")),
        "polyline": (route.get("polyline") or {}).get("encodedPolyline", ""),
        "optimized_waypoint_order": route.get("optimizedIntermediateWaypointIndex") or [],
        "legs": legs,
        "traffic_mode": configured_traffic_mode,
        "routing_preference": TRAFFIC_MODE_TO_ROUTING_PREFERENCE[configured_traffic_mode],
    }


# ── Static map ────────────────────────────────────────────────────────────────

def static_map_url(
    markers: List[Dict],
    encoded_path: Optional[str] = None,
    size: str = "640x400",
    maptype: str = "roadmap",
) -> str:
    """
    Static Maps is intentionally disabled until Sentinel can proxy images without
    returning a server-key-bearing URL to the browser or packet HTML.
    """
    return ""
