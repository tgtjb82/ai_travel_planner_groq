import json
import math
import os
import re
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
GROQ_MODEL_DEFAULT = "openai/gpt-oss-20b"
KOREA_LAT_RANGE = (32.0, 39.7)
KOREA_LNG_RANGE = (124.0, 132.8)
HTTP_HEADERS = {
    "User-Agent": "AI-Travel-Planner/2.0 (custom web app; contact: local-dev)"
}
GEOCODE_TIMEOUT_SECONDS = 2
OVERPASS_TIMEOUT_SECONDS = 3
WALKING_DISTANCE_KM = 1.6
WALKING_SPEED_KMH = 4.2
MAX_ROUTE_LEG_KM = 50.0
GEOCODE_HARD_REJECT_DISTANCE_KM = 90.0
GEOCODE_CACHE: dict[str, dict[str, Any]] = {}
OSM_PLACE_CACHE: dict[str, list[dict[str, Any]]] = {}
OSM_TEMPLATE_CACHE: dict[str, list[dict[str, Any]]] = {}
OSM_REGION_CENTER_CACHE: dict[str, tuple[float, float]] = {}
OSM_LODGING_CACHE: dict[str, dict[str, Any]] = {}
GROQ_LAST_RATE_LIMIT_INFO: dict[str, Any] = {}
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"
INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')
TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
MISSING_COMMA_BETWEEN_OBJECTS_RE = re.compile(r"}\s*(?=\s*{)")
MISSING_COMMA_BEFORE_KEY_RE = re.compile(r'([}\]"0-9]|true|false|null)\s+("[A-Za-z_][A-Za-z0-9_]*"\s*:)')


def load_environment() -> None:
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")


load_environment()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL_REQUESTED = os.getenv("GROQ_MODEL", GROQ_MODEL_DEFAULT).strip() or GROQ_MODEL_DEFAULT
ENABLE_OSM_PLACES = os.getenv("ENABLE_OSM_PLACES", "1").strip().lower() not in {"0", "false", "no", "off"}
ENABLE_OSM_HUB = os.getenv("ENABLE_OSM_HUB", "0").strip().lower() not in {"0", "false", "no", "off"}
ENABLE_OSM_LODGING = os.getenv("ENABLE_OSM_LODGING", "1").strip().lower() not in {"0", "false", "no", "off"}


def is_gpt_oss_model(model_name: str) -> bool:
    return model_name.lower().startswith("openai/gpt-oss")


def active_model_name() -> str:
    return GROQ_MODEL_REQUESTED


def to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "")
    if "," in text:
        digits = re.sub(r"\D", "", text)
        return int(digits) if digits else default
    match = re.search(r"\d+", text)
    return int(match.group()) if match else default


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_korea_coordinate(lat: float, lng: float) -> bool:
    return KOREA_LAT_RANGE[0] <= lat <= KOREA_LAT_RANGE[1] and KOREA_LNG_RANGE[0] <= lng <= KOREA_LNG_RANGE[1]


def dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def geo_distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalized_text_key(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(value or "").lower())


def geocode_region_hint(region: str) -> tuple[float, float] | None:
    if not region:
        return None
    centers = globals().get("REGION_CENTERS", {})
    canonical_func = globals().get("canonical_region_key")
    canonical = canonical_func(region) if callable(canonical_func) else str(region).strip()
    center = centers.get(canonical)
    return center if isinstance(center, tuple) else None


def geocode_candidate_score(
    candidate: dict[str, Any],
    place: str,
    region: str,
    center_hint: tuple[float, float] | None,
) -> tuple[float, float] | None:
    lat = to_float(candidate.get("lat"))
    lng = to_float(candidate.get("lon"))
    if lat is None or lng is None or not is_korea_coordinate(lat, lng):
        return None

    display = str(candidate.get("display_name", ""))
    display_key = normalized_text_key(display)
    place_key = normalized_text_key(place)
    region_key = normalized_text_key(region)
    score = 0.0

    if place_key and place_key in display_key:
        score += 24
    if region_key and region_key in display_key:
        score += 20

    candidate_class = str(candidate.get("class", ""))
    candidate_type = str(candidate.get("type", ""))
    if candidate_class in {"tourism", "historic", "amenity", "leisure", "natural", "railway"}:
        score += 8
    if candidate_type in {"station", "museum", "attraction", "viewpoint", "marketplace", "park"}:
        score += 6

    distance = 9999.0
    if center_hint:
        distance = geo_distance_km(center_hint[0], center_hint[1], lat, lng)
        if distance > GEOCODE_HARD_REJECT_DISTANCE_KM:
            return None
        if distance <= 3:
            score += 20
        elif distance <= 15:
            score += 14
        elif distance <= 45:
            score += 8
        elif distance > 120:
            score -= 30

    return score, distance


def geocode_place(place: str, region: str, address: str = "") -> dict[str, Any]:
    place = place.strip()
    region = region.strip()
    address = address.strip()
    if not place and not address:
        return {}

    queries = dedupe(
        [
            f"{address}, 대한민국" if address else "",
            f"{place}, {region}, 대한민국" if region else "",
            f"{region} {place} 대한민국" if region else "",
            f"{place} 대한민국",
        ]
    )

    all_candidates: list[tuple[float, float, dict[str, Any]]] = []
    center_hint = geocode_region_hint(region)
    cached_result: dict[str, Any] | None = None
    for query in queries:
        cache_key = f"geo::{query}"
        if cache_key in GEOCODE_CACHE:
            cached_result = GEOCODE_CACHE[cache_key]
            continue

        try:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 2,
                    "countrycodes": "kr",
                    "addressdetails": 1,
                },
                headers=HTTP_HEADERS,
                timeout=GEOCODE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            candidates = response.json()
        except (requests.RequestException, ValueError):
            continue

        for candidate in candidates:
            scored = geocode_candidate_score(candidate, place, region, center_hint)
            if not scored:
                continue
            score, distance = scored
            lat = to_float(candidate.get("lat"))
            lng = to_float(candidate.get("lon"))
            result = {
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "name": candidate.get("name") or "",
                "address": candidate.get("display_name", address),
                "source": "OpenStreetMap",
                "osm_class": candidate.get("class") or "",
                "osm_type": candidate.get("type") or "",
            }
            all_candidates.append((score, distance, result))

    if all_candidates:
        all_candidates.sort(key=lambda item: (-item[0], item[1]))
        best = all_candidates[0][2]
        for query in queries:
            GEOCODE_CACHE[f"geo::{query}"] = best
        return best

    if cached_result:
        return cached_result

    return {}


def is_lodging_place_name(place: Any) -> bool:
    text = str(place or "").strip()
    return text.startswith("숙소[") or normalized_text_key(text) in {"숙소", "호텔", "리조트"}


def extract_lodging_name(place: Any) -> str:
    text = str(place or "").strip()
    match = re.match(r"숙소\[(.+?)\]", text)
    if match:
        return match.group(1).strip()
    return text.replace("숙소", "").strip("[] ")


def verified_lodging_geocode(geo: dict[str, Any], lodging_name: str, region: str) -> bool:
    if not geo:
        return False
    lat = to_float(geo.get("lat"))
    lng = to_float(geo.get("lng"))
    if lat is None or lng is None:
        return False
    center_lat, center_lng = dynamic_region_center(region)
    if geo_distance_km(center_lat, center_lng, lat, lng) > route_region_radius_km(region):
        return False

    osm_class = str(geo.get("osm_class", "")).lower()
    osm_type = str(geo.get("osm_type", "")).lower()
    lodging_types = {"hotel", "motel", "guest_house", "hostel", "apartment", "resort"}
    if osm_class == "tourism" and osm_type in lodging_types:
        return True

    name_key = normalized_text_key(lodging_name)
    geo_key = normalized_text_key(f"{geo.get('name', '')} {geo.get('address', '')}")
    return bool(name_key and name_key in geo_key and any(word in geo_key for word in ("호텔", "모텔", "리조트", "펜션", "게스트하우스", "숙소")))


def verified_place_geocode(geo: dict[str, Any], place: str, region: str) -> bool:
    if not geo:
        return False
    lat = to_float(geo.get("lat"))
    lng = to_float(geo.get("lng"))
    if lat is None or lng is None:
        return False
    center_lat, center_lng = dynamic_region_center(region)
    if geo_distance_km(center_lat, center_lng, lat, lng) > route_region_radius_km(region):
        return False

    place_key = normalized_text_key(place)
    geo_key = normalized_text_key(f"{geo.get('name', '')} {geo.get('address', '')}")
    if not geo_key:
        return False

    generic_words = ("대표", "로컬", "거리", "코스", "명소", "중심", "미정", "추천")
    if any(word in place_key for word in generic_words) and place_key not in geo_key:
        return False

    return True


def fallback_coordinate(point: dict[str, Any], region: str) -> dict[str, Any]:
    lat = to_float(point.get("lat"))
    lng = to_float(point.get("lng"))
    if lat is not None and lng is not None and is_korea_coordinate(lat, lng):
        return {
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "address": point.get("address") or "주소 확인 필요",
            "source": "AI 제공 좌표",
        }

    center_lat, center_lng = region_center(region)
    canonical = canonical_region_key(region)
    if canonical not in REGION_CENTERS:
        center = geocode_place(region, "", "")
        center_lat = to_float(center.get("lat")) or center_lat
        center_lng = to_float(center.get("lng")) or center_lng
    day = max(1, to_int(point.get("day"), 1))
    order = max(1, to_int(point.get("order"), 1))
    offset = ((day - 1) * 8 + order - 1) * 0.0025
    return {
        "lat": round(center_lat + offset, 6),
        "lng": round(center_lng + offset, 6),
        "address": point.get("address") or "정확한 주소 확인 필요",
        "source": "지역 중심 보조 좌표",
    }


def normalize_point(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    place = str(item.get("place", "")).strip()
    if not place:
        return None

    lat = to_float(item.get("lat"))
    lng = to_float(item.get("lng"))
    return {
        "id": f"p-{max(1, to_int(item.get('day'), 1))}-{max(1, to_int(item.get('order'), index + 1))}-{index}",
        "day": max(1, to_int(item.get("day"), 1)),
        "order": max(1, to_int(item.get("order"), index + 1)),
        "time": str(item.get("time", "")).strip() or f"{index + 1}번째",
        "place": place,
        "address": str(item.get("address", "")).strip(),
        "lat": round(lat, 6) if lat is not None and is_korea_coordinate(lat, lng or 0) else None,
        "lng": round(lng, 6) if lng is not None and is_korea_coordinate(lat or 0, lng) else None,
        "category": str(item.get("category", "관광지")).strip() or "관광지",
        "summary": (str(item.get("summary", "")).strip() or "장소 특징 확인 필요")[:90],
        "tip": str(item.get("tip", "")).strip()[:90],
    }


def normalize_route_points(raw_points: Any, region: str) -> list[dict[str, Any]]:
    if not isinstance(raw_points, list):
        return []

    points: list[dict[str, Any]] = []
    for index, item in enumerate(raw_points):
        if not isinstance(item, dict):
            continue

        point = normalize_point(item, index)
        if not point:
            continue

        is_lodging = is_lodging_place_name(point["place"]) or str(point.get("category", "")).strip() == "숙소"
        search_name = extract_lodging_name(point["place"]) if is_lodging else point["place"]
        geo = geocode_place(search_name, region, point["address"])
        if is_lodging and not verified_lodging_geocode(geo, search_name, region):
            continue

        if not is_lodging and not verified_place_geocode(geo, search_name, region):
            continue

        point["lat"] = geo["lat"]
        point["lng"] = geo["lng"]
        actual_name = str(geo.get("name") or "").strip()
        if actual_name:
            point["place"] = actual_name
        point["address"] = point["address"] or geo["address"]
        point["location_source"] = geo["source"]
        points.append(point)

    return sorted(points, key=lambda value: (value["day"], value["order"]))


def haversine_km(start: dict[str, Any], end: dict[str, Any]) -> float:
    radius = 6371.0
    lat1 = math.radians(float(start["lat"]))
    lat2 = math.radians(float(end["lat"]))
    delta_lat = lat2 - lat1
    delta_lng = math.radians(float(end["lng"]) - float(start["lng"]))
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def taxi_estimate(distance_km: float) -> tuple[int, int]:
    minutes = max(5, round((distance_km / 27) * 60 + 4))
    fare = 4800 + max(0, distance_km - 1.6) * 850 + minutes * 120
    return minutes, int(round(fare / 100) * 100)


def transit_estimate(distance_km: float) -> tuple[int, int]:
    minutes = max(7, round((distance_km / 18) * 60 + 8))
    fare = 1550 if distance_km <= 10 else 1750 + int((distance_km - 10) // 5) * 100
    return minutes, fare


def walking_estimate(distance_km: float) -> int:
    return max(3, round((distance_km / WALKING_SPEED_KMH) * 60 + 2))


def is_car_transport(transport: str) -> bool:
    text = normalized_text_key(transport)
    return any(keyword in text for keyword in ("자차", "자동차", "차량", "렌터카", "렌트카"))


def is_walk_transport(transport: str) -> bool:
    text = normalized_text_key(transport)
    return "도보" in text


def car_estimate(distance_km: float) -> tuple[int, int]:
    minutes = max(4, round((distance_km / 35) * 60 + 5))
    fuel_cost = int(round((max(0.0, distance_km) * 190) / 100) * 100)
    return minutes, fuel_cost


def normalize_transport_steps(raw_steps: Any, points: list[dict[str, Any]], transport: str) -> list[dict[str, Any]]:
    raw_lookup: dict[tuple[int, int, int], dict[str, Any]] = {}
    if isinstance(raw_steps, list):
        for item in raw_steps:
            if not isinstance(item, dict):
                continue
            key = (
                max(1, to_int(item.get("day"), 1)),
                max(1, to_int(item.get("from_order"), 1)),
                max(1, to_int(item.get("to_order"), 1)),
            )
            raw_lookup[key] = item

    by_day: dict[int, list[dict[str, Any]]] = {}
    for point in points:
        by_day.setdefault(point["day"], []).append(point)

    steps: list[dict[str, Any]] = []
    for day, day_points in by_day.items():
        day_points = sorted(day_points, key=lambda value: value["order"])
        for start, end in zip(day_points, day_points[1:]):
            key = (day, start["order"], end["order"])
            raw = raw_lookup.get(key, {})
            distance = haversine_km(start, end)
            taxi_minutes, taxi_fare = taxi_estimate(distance)
            transit_minutes, transit_fare = transit_estimate(distance)
            walk_minutes = walking_estimate(distance)
            car_minutes, car_cost = car_estimate(distance)
            prefer_car = is_car_transport(transport)
            recommend_walk = distance <= WALKING_DISTANCE_KM or (is_walk_transport(transport) and distance <= 2.4)

            public = raw.get("public_transport") if isinstance(raw.get("public_transport"), dict) else {}
            taxi = raw.get("taxi") if isinstance(raw.get("taxi"), dict) else {}
            public_instruction = str(public.get("instruction", "")).strip()
            if recommend_walk:
                public_instruction = f"도보 이용 추천. 약 {walk_minutes}분 소요"
            elif prefer_car:
                public_instruction = f"자차 이동. 약 {car_minutes}분 소요"
            elif not public_instruction:
                public_instruction = "가까운 정류장/역에서 목적지 방향 버스 또는 지하철 이용"

            if recommend_walk:
                public_minutes = walk_minutes
                public_fare = 0
                recommended_mode = "walk"
            elif prefer_car:
                public_minutes = car_minutes
                public_fare = 0
                recommended_mode = "car"
            else:
                public_minutes = to_int(public.get("duration_minutes"), transit_minutes)
                public_fare = to_int(public.get("fare_krw"), transit_fare)
                recommended_mode = "public"

            steps.append(
                {
                    "day": day,
                    "from_order": start["order"],
                    "to_order": end["order"],
                    "from_place": start["place"],
                    "to_place": end["place"],
                    "distance_km": round(distance, 1),
                    "transport_preference": transport,
                    "recommended_mode": recommended_mode,
                    "walk": {
                        "duration_minutes": walk_minutes,
                        "instruction": f"도보 이용 추천. 약 {walk_minutes}분 소요",
                    },
                    "car": {
                        "duration_minutes": car_minutes,
                        "estimated_cost_krw": car_cost,
                        "instruction": f"자차 이동. 약 {car_minutes}분 소요",
                        "note": "주차비와 통행료는 현장 조건에 따라 달라질 수 있습니다.",
                    },
                    "public_transport": {
                        "instruction": public_instruction,
                        "duration_minutes": public_minutes,
                        "fare_krw": public_fare,
                        "note": str(public.get("note", "정확한 운행 시간은 출발 전 지도앱에서 확인하세요.")).strip(),
                    },
                    "taxi": {
                        "duration_minutes": to_int(taxi.get("duration_minutes"), taxi_minutes),
                        "fare_krw": to_int(taxi.get("fare_krw"), taxi_fare),
                        "note": str(taxi.get("note", "교통 상황에 따라 시간과 요금이 달라질 수 있습니다.")).strip(),
                    },
                }
            )

    return steps


def build_prompt(payload: dict[str, Any]) -> str:
    styles = payload.get("style") or []
    if isinstance(styles, str):
        styles = [styles]
    style_text = ", ".join(styles) if styles else "균형형"

    return f"""
너는 국내여행 전문 AI 여행 플래너야.
반드시 유효한 JSON 객체만 출력해. 코드블록과 설명 문장은 금지야.

[여행 조건]
- 여행 지역: {payload.get("region")}
- 여행 기간: {payload.get("days")}
- 여행 시작일: {payload.get("start_date") or "미지정"}
- 여행 종료일: {payload.get("end_date") or "미지정"}
- 총 여행일수: {payload.get("trip_days") or trip_day_count(payload)}일
- 예산: {payload.get("budget")}
- 여행 스타일: {style_text}
- 동행: {payload.get("companion")}
- 이동수단: {payload.get("transport")}

[최상위 JSON 키]
- trip_theme: 여행 컨셉 한 줄
- route_points: 지도에 표시할 모든 방문지 배열
- transport_steps: 방문지 사이 이동 정보 배열. 모르면 빈 배열

[중요 규칙]
- JSON 문자열 안에 역슬래시(\\), Markdown 표, 긴 문단을 넣지 마.
- 하루에 3~5개의 방문지를 넣고, 실제 이동 순서대로 order를 매겨.
- 주소를 알면 address에 넣어. 모르면 빈 문자열로 둬.
- 실제 장소명/상호명만 사용하고 가상의 장소는 만들지 마.
- 확실하지 않은 장소명은 만들지 말고 생략해.
- 숙소가 들어갈 때는 place를 반드시 "숙소[정확한 숙소명]" 형식으로 써. 예: 숙소[나인트리 프리미어 호텔 명동2]
- 일정은 실제 여행처럼 이어져야 해. 모든 날짜의 마지막 장소는 반드시 숙소여야 해.
- 2일차 이후는 보통 같은 숙소에서 출발하게 구성해.
- 사용자가 해당 섬을 직접 입력한 경우가 아니라면 배/페리로만 갈 수 있는 옆섬 장소는 넣지 마.
- 대중교통이나 택시로 이동하기 어려운 장소는 제외해.
- 대중교통 선호라면 가능한 버스 번호, 지하철 노선, 환승 정보를 써. 확실하지 않으면 "지도앱 확인"이라고 표시해.
- 가까운 구간은 버스를 억지로 넣지 말고 도보 이동을 추천해.
- 택시 대안은 모든 이동 구간에 대해 예상 시간과 예상 요금을 넣어.
- transport_steps는 가능한 경우만 같은 날짜의 order 1→2, 2→3처럼 인접 장소 이동 구간을 포함해.
- summary와 tip은 각각 35자 이하로 짧게 써.

[route_points 항목]
day, order, time, place, address, lat, lng, category, summary, tip

[transport_steps 항목]
day, from_order, to_order, from_place, to_place,
public_transport: {{ instruction, duration_minutes, fare_krw, note }},
taxi: {{ duration_minutes, fare_krw, note }}

출력 예:
{{
  "trip_theme": "바다와 맛집을 가볍게 도는 여행",
  "route_points": [],
  "transport_steps": []
}}
"""


def build_groq_prompt(payload: dict[str, Any]) -> str:
    styles = payload.get("style") or []
    if isinstance(styles, str):
        styles = [styles]
    style_text = ", ".join(styles) if styles else "균형형"
    total_days = trip_day_count(payload)
    region = str(payload.get("region") or "")
    seed_places: list[str] = []
    try:
        seed_places = [
            str(point.get("place", "")).strip()
            for point in fallback_template_for_region(region)
            if point.get("place") and str(point.get("category", "")) not in {"숙소"}
        ]
    except Exception:
        seed_places = []
    seed_text = ", ".join(seed_places[:8]) if seed_places else "Use well-known public landmarks, markets, parks, stations, beaches, museums, or streets."

    return f"""
Create a Korean domestic travel itinerary.
Return ONLY valid JSON. Do not use markdown.

Conditions:
- region: {payload.get("region")}
- start_date: {payload.get("start_date") or "unknown"}
- end_date: {payload.get("end_date") or "unknown"}
- total_days: {total_days}
- budget: {payload.get("budget")}
- companion: {payload.get("companion")}
- transport: {payload.get("transport")}
- style: {style_text}
- recommended_real_places: {seed_text}

Rules:
- Make route_points for every day 1..{total_days}.
- Use 3 or 4 stops per day.
- Use real Korean landmarks, streets, markets, stations, beaches, museums, or public places.
- Do not invent fictional business names.
- If coordinates are uncertain, use null for lat and lng.
- Use ONE lodging only for the whole trip. Do not switch lodging names during the trip.
- Do not invent lodging, hotel, restaurant, cafe, beach, market, museum, or landmark names.
- Use only real verifiable place names. If a place is uncertain, omit it instead of inventing it.
- Every day must end at that same real lodging. The last route_point of each day must be 숙소[exact real lodging name].
- Day 2 and later should usually start at that same lodging.
- Consecutive places in a day must be within 50km. Prefer tighter routes under 25km.
- Do not include offshore islands or boat/ferry-only places unless the requested region itself is that island.
- If transport is car/private vehicle, choose car-accessible places and do not describe public transit as the main route.
- If transport is public transit, use places reachable by walking, public transit, or taxi from the requested region.
- summary and tip must be short Korean phrases under 24 Korean characters each.
- Do not include transport_steps. The server will calculate movement data.

JSON shape:
{{
  "trip_theme": "짧은 한국어 여행 주제",
  "route_points": [
    {{"day": 1, "order": 1, "time": "10:00", "place": "장소명", "address": "주소", "lat": null, "lng": null, "category": "분류", "summary": "짧은 특징", "tip": "짧은 팁"}}
  ]
}}
"""


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s*```$", "", cleaned).strip()


def extract_json_text(text: str) -> str:
    cleaned = strip_code_fence(text)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first == -1 or last == -1 or first > last:
        return cleaned
    return cleaned[first : last + 1]


def repair_json_text(text: str) -> str:
    repaired = text.replace("\ufeff", "").replace("\u200b", "")
    repaired = INVALID_JSON_ESCAPE_RE.sub(r"\\\\", repaired)
    repaired = MISSING_COMMA_BETWEEN_OBJECTS_RE.sub("},", repaired)
    repaired = MISSING_COMMA_BEFORE_KEY_RE.sub(r"\1,\n\2", repaired)
    repaired = TRAILING_COMMA_RE.sub(r"\1", repaired)
    return repaired


def iterative_comma_repair(text: str, max_repairs: int = 12) -> str:
    candidate = text
    for _ in range(max_repairs):
        try:
            json.loads(candidate, strict=False)
            return candidate
        except json.JSONDecodeError as exc:
            if "Expecting ',' delimiter" not in exc.msg:
                return candidate
            pos = max(0, min(len(candidate), exc.pos))
            if pos > 0 and candidate[pos - 1] == ",":
                return candidate
            candidate = f"{candidate[:pos]},{candidate[pos:]}"
            candidate = repair_json_text(candidate)
    return candidate


def load_json_object(text: str) -> dict[str, Any]:
    candidates = [text, extract_json_text(text)]
    repaired = repair_json_text(candidates[-1])
    if repaired not in candidates:
        candidates.append(repaired)
    comma_repaired = iterative_comma_repair(repaired)
    if comma_repaired not in candidates:
        candidates.append(comma_repaired)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(parsed, dict):
            raise ValueError("AI 응답이 JSON 객체가 아닙니다.")
        return parsed

    if last_error:
        raise ValueError(
            "AI 응답 JSON 파싱에 실패했습니다. "
            f"{last_error.msg} (line {last_error.lineno}, column {last_error.colno})"
        ) from last_error
    raise ValueError("AI 응답을 읽을 수 없습니다.")


def parse_ai_response(text: str) -> dict[str, Any]:
    return load_json_object(text)



def friendly_groq_response_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}

    error = body.get("error") if isinstance(body, dict) else {}
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("code") or "").strip()
    else:
        message = str(error or "").strip()
    if not message:
        message = response.text[:300].strip() or response.reason

    if response.status_code == 429:
        reset_requests = response.headers.get("x-ratelimit-reset-requests")
        reset_tokens = response.headers.get("x-ratelimit-reset-tokens")
        reset_text = reset_requests or reset_tokens
        if reset_text:
            return f"Groq 무료 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요. reset={reset_text}"
        return "Groq 무료 요청 한도에 걸렸습니다. 잠시 후 다시 시도하세요."
    return f"Groq API 오류({response.status_code}): {message}"


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part or ""))
        return "\n".join(part for part in parts if part.strip()).strip()
    return str(content or "").strip()


def extract_groq_choice_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise RuntimeError("Groq가 일정 응답을 반환하지 않았습니다.")

    finish_reasons: list[str] = []
    refusals: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        finish = str(choice.get("finish_reason") or "").strip()
        if finish:
            finish_reasons.append(finish)

        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        for key in ("content", "output_text", "reasoning_content", "reasoning"):
            text = content_to_text(message.get(key))
            if text:
                return text

        refusal = content_to_text(message.get("refusal"))
        if refusal:
            refusals.append(refusal)

        choice_text = content_to_text(choice.get("text"))
        if choice_text:
            return choice_text

    if refusals:
        raise RuntimeError(refusals[0])
    if "length" in finish_reasons:
        raise RuntimeError("Groq 응답이 출력 길이 제한에 걸려 최종 일정이 비었습니다.")
    reason_text = ", ".join(finish_reasons) or "unknown"
    raise RuntimeError(f"Groq가 비어 있는 일정 응답을 반환했습니다. finish_reason={reason_text}")


def groq_rate_limit_info(response: requests.Response) -> dict[str, str]:
    wanted = {
        "x-ratelimit-limit-requests": "limit_requests",
        "x-ratelimit-remaining-requests": "remaining_requests",
        "x-ratelimit-reset-requests": "reset_requests",
        "x-ratelimit-limit-tokens": "limit_tokens",
        "x-ratelimit-remaining-tokens": "remaining_tokens",
        "x-ratelimit-reset-tokens": "reset_tokens",
        "retry-after": "retry_after",
    }
    info: dict[str, str] = {}
    for header, key in wanted.items():
        value = response.headers.get(header)
        if value:
            info[key] = value
    return info


def groq_rate_limit_notice(info: dict[str, Any]) -> str:
    if not info:
        return ""

    parts: list[str] = []
    remaining_requests = info.get("remaining_requests")
    limit_requests = info.get("limit_requests")
    remaining_tokens = info.get("remaining_tokens")
    limit_tokens = info.get("limit_tokens")
    reset_requests = info.get("reset_requests")
    reset_tokens = info.get("reset_tokens")

    if remaining_requests:
        parts.append(f"요청 {remaining_requests}/{limit_requests or '?'}")
    if remaining_tokens:
        parts.append(f"토큰 {remaining_tokens}/{limit_tokens or '?'}")
    if reset_requests:
        parts.append(f"요청 리셋 {reset_requests}")
    if reset_tokens:
        parts.append(f"토큰 리셋 {reset_tokens}")

    return "Groq 한도 상태: " + " · ".join(parts) if parts else ""


def log_groq_rate_limit_info(info: dict[str, Any]) -> None:
    notice = groq_rate_limit_notice(info)
    if notice:
        print(f"[groq-rate-limit] {notice}", flush=True)


def generate_groq_plan_text(payload: dict[str, Any]) -> str:
    global GROQ_LAST_RATE_LIMIT_INFO
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY가 설정되어 있지 않습니다.")

    max_tokens = min(5000, max(2400, 1500 + trip_day_count(payload) * 550))
    instruction = (
        "You are a Korean domestic travel planner. "
        "Return one JSON object only. "
        "No markdown, no code fences, no comments, and no prose outside the JSON object. "
        "Never return an empty answer. If details are uncertain, use real well-known landmarks and set unknown coordinates to null. "
        "The JSON must contain trip_theme and route_points."
    )
    request_body = {
        "model": active_model_name(),
        "messages": [{"role": "user", "content": f"{instruction}\n\n{build_groq_prompt(payload)}"}],
        "temperature": 0.1,
        "max_completion_tokens": max_tokens,
    }
    if is_gpt_oss_model(active_model_name()):
        request_body["reasoning_effort"] = "low"
        request_body["reasoning_format"] = "hidden"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=request_body,
            timeout=45,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Groq API 연결에 실패했습니다: {exc}") from exc

    GROQ_LAST_RATE_LIMIT_INFO = groq_rate_limit_info(response)
    log_groq_rate_limit_info(GROQ_LAST_RATE_LIMIT_INFO)
    if response.status_code >= 400:
        raise RuntimeError(friendly_groq_response_error(response))

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Groq API 응답을 JSON으로 읽지 못했습니다.") from exc

    return extract_groq_choice_text(data)


def generate_plan_text(payload: dict[str, Any]) -> str:
    return generate_groq_plan_text(payload)


def generate_plan_data(payload: dict[str, Any]) -> dict[str, Any]:
    text = generate_plan_text(payload)
    try:
        return parse_ai_response(text)
    except ValueError as exc:
        raise ValueError(
            "AI 응답 JSON 형식이 깨졌습니다. 구조화 JSON 스키마 응답을 다시 확인해야 합니다."
        ) from exc


def table_text(value: Any) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def format_krw(value: Any) -> str:
    amount = to_int(value, 0)
    return "무료" if amount <= 0 else f"{amount:,}원"


def group_points_by_day(points: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for point in points:
        grouped.setdefault(max(1, to_int(point.get("day"), 1)), []).append(point)
    for day_points in grouped.values():
        day_points.sort(key=lambda item: to_int(item.get("order"), 1))
    return dict(sorted(grouped.items()))


def transport_lookup(steps: list[dict[str, Any]]) -> dict[tuple[int, int, int], dict[str, Any]]:
    return {
        (
            max(1, to_int(step.get("day"), 1)),
            max(1, to_int(step.get("from_order"), 1)),
            max(1, to_int(step.get("to_order"), 1)),
        ): step
        for step in steps
    }


def move_summary(step: dict[str, Any] | None) -> str:
    if not step:
        return "마지막 장소"

    public = step.get("public_transport") if isinstance(step.get("public_transport"), dict) else {}
    taxi = step.get("taxi") if isinstance(step.get("taxi"), dict) else {}
    car = step.get("car") if isinstance(step.get("car"), dict) else {}
    public_minutes = to_int(public.get("duration_minutes"), 0)
    taxi_minutes = to_int(taxi.get("duration_minutes"), 0)
    car_minutes = to_int(car.get("duration_minutes"), public_minutes)

    if step.get("recommended_mode") == "walk":
        return f"도보 {public_minutes}분 추천 / 택시 {taxi_minutes}분"

    if step.get("recommended_mode") == "car":
        return f"자차 {car_minutes}분 / 유류비 참고 {format_krw(car.get('estimated_cost_krw'))}"

    return (
        f"대중교통 {public_minutes}분({format_krw(public.get('fare_krw'))}) / "
        f"택시 {taxi_minutes}분({format_krw(taxi.get('fare_krw'))})"
    )


def category_matches(point: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    text = f"{point.get('category', '')} {point.get('place', '')} {point.get('summary', '')}"
    return any(keyword in text for keyword in keywords)


def bullet_points(points: list[dict[str, Any]], keywords: tuple[str, ...], limit: int = 5) -> list[str]:
    selected = [point for point in points if category_matches(point, keywords)]
    if not selected:
        selected = points[:limit]
    return [
        f"- {table_text(point.get('place'))}: {table_text(point.get('summary')) or '방문 추천 장소'}"
        for point in selected[:limit]
    ]


def estimate_trip_cost(transport_steps: list[dict[str, Any]], route_points: list[dict[str, Any]], budget: str) -> str:
    public_total = 0
    taxi_total = 0
    car_total = 0
    has_car_steps = False
    for step in transport_steps:
        public = step.get("public_transport") if isinstance(step.get("public_transport"), dict) else {}
        taxi = step.get("taxi") if isinstance(step.get("taxi"), dict) else {}
        car = step.get("car") if isinstance(step.get("car"), dict) else {}
        public_total += to_int(public.get("fare_krw"), 0)
        taxi_total += to_int(taxi.get("fare_krw"), 0)
        car_total += to_int(car.get("estimated_cost_krw"), 0)
        has_car_steps = has_car_steps or step.get("recommended_mode") == "car"

    food_count = max(1, len([point for point in route_points if category_matches(point, ("맛집", "카페", "식당"))]))
    food_estimate = food_count * 18000
    lines = [
        "| 항목 | 예상 비용 |",
        "|---|---:|",
        f"| 입력 예산 | {table_text(budget) or '미입력'} |",
    ]
    if has_car_steps:
        lines.append(f"| 자차 유류비 참고 | {format_krw(car_total)} |")
    else:
        lines.append(f"| 대중교통 합계 | {format_krw(public_total)} |")
        lines.append(f"| 택시 이동 시 합계 | {format_krw(taxi_total)} |")
    lines.append(f"| 식비/카페 참고치 | {format_krw(food_estimate)} |")
    return "\n".join(lines)


def build_plan_markdown(
    payload: dict[str, Any],
    route_points: list[dict[str, Any]],
    transport_steps: list[dict[str, Any]],
    theme: str = "",
) -> str:
    region = table_text(payload.get("region")) or "국내"
    budget = table_text(payload.get("budget"))
    days_label = table_text(payload.get("days"))
    styles = payload.get("style") or []
    if isinstance(styles, str):
        styles = [styles]
    style_text = ", ".join(str(style) for style in styles) or "균형형"
    theme = table_text(theme) or f"{region}의 대표 동선을 가볍게 따라가는 {style_text} 여행"

    grouped = group_points_by_day(route_points)
    step_map = transport_lookup(transport_steps)
    lines: list[str] = []

    for day, day_points in grouped.items():
        lines.extend([f"## {day}일차 일정", "| 시간 | 장소 | 주소/특징 | 다음 이동 |", "|---|---|---|---|"])
        for index, point in enumerate(day_points):
            next_point = day_points[index + 1] if index + 1 < len(day_points) else None
            step = step_map.get((day, to_int(point.get("order"), 1), to_int(next_point.get("order"), 1))) if next_point else None
            detail = table_text(point.get("address")) or table_text(point.get("summary")) or "현장 확인"
            if point.get("summary") and point.get("address"):
                detail = f"{table_text(point.get('address'))}<br>{table_text(point.get('summary'))}"
            lines.append(
                "| "
                f"{table_text(point.get('time')) or '-'} | "
                f"{table_text(point.get('place'))} | "
                f"{detail} | "
                f"{table_text(move_summary(step))} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 추천 관광지",
            *bullet_points(route_points, ("관광", "문화", "자연", "해변", "공원", "마을")),
            "",
            "## 추천 맛집/카페",
            *bullet_points(route_points, ("맛집", "카페", "식당", "시장")),
            "",
        ]
    )

    lines.extend(
        [
            "## 예상 비용표",
            estimate_trip_cost(transport_steps, route_points, budget),
            "",
            "## 준비물 체크리스트",
            "- ☑️ 세안도구",
            "- ☑️ 보조배터리",
            "- ☑️ 충전기",
            "- ☑️ 접이식 양우산",
            "- ☑️ 편한 신발",
            "- ☑️ 교통카드",
            "- ☑️ 예약 확인 내역",
        ]
    )
    return "\n".join(lines)


FALLBACK_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "서울": [
        {"day": 1, "order": 1, "time": "10:00", "place": "서울역", "address": "서울특별시 용산구 한강대로 405", "lat": 37.5547, "lng": 126.9706, "category": "이동", "summary": "교통 접근성이 좋은 출발 지점", "tip": "짐 보관 후 이동하면 편합니다."},
        {"day": 1, "order": 2, "time": "11:00", "place": "덕수궁", "address": "서울특별시 중구 세종대로 99", "lat": 37.5658, "lng": 126.9751, "category": "문화", "summary": "도심 속 고궁 산책 코스", "tip": "돌담길까지 이어서 걷기 좋습니다."},
        {"day": 1, "order": 3, "time": "13:00", "place": "명동거리", "address": "서울특별시 중구 명동", "lat": 37.5636, "lng": 126.9826, "category": "맛집", "summary": "식사와 쇼핑을 함께 하기 좋은 구역", "tip": "점심 시간대는 붐빌 수 있습니다."},
        {"day": 1, "order": 4, "time": "15:30", "place": "청계천", "address": "서울특별시 종로구 청계천로", "lat": 37.5690, "lng": 126.9787, "category": "산책", "summary": "도심 산책과 휴식에 좋은 물길", "tip": "해 질 무렵 분위기가 좋습니다."},
        {"day": 2, "order": 1, "time": "10:00", "place": "북촌한옥마을", "address": "서울특별시 종로구 계동길 37", "lat": 37.5826, "lng": 126.9830, "category": "관광지", "summary": "한옥 골목을 걷는 대표 코스", "tip": "거주 지역이라 조용히 관람하세요."},
        {"day": 2, "order": 2, "time": "12:00", "place": "인사동", "address": "서울특별시 종로구 인사동길", "lat": 37.5740, "lng": 126.9850, "category": "문화", "summary": "전통 소품과 찻집이 많은 거리", "tip": "골목 안쪽 카페도 살펴보세요."},
        {"day": 2, "order": 3, "time": "14:00", "place": "광장시장", "address": "서울특별시 종로구 창경궁로 88", "lat": 37.5700, "lng": 126.9996, "category": "맛집", "summary": "먹거리 중심의 전통시장", "tip": "현금과 작은 가방이 편합니다."},
        {"day": 3, "order": 1, "time": "10:00", "place": "N서울타워", "address": "서울특별시 용산구 남산공원길 105", "lat": 37.5512, "lng": 126.9882, "category": "관광지", "summary": "서울 전망을 보는 대표 명소", "tip": "날씨가 맑은 시간대를 추천합니다."},
        {"day": 3, "order": 2, "time": "12:30", "place": "홍대거리", "address": "서울특별시 마포구 홍익로", "lat": 37.5558, "lng": 126.9237, "category": "문화", "summary": "상점과 카페가 많은 활기찬 거리", "tip": "골목별 분위기가 달라 천천히 보세요."},
        {"day": 3, "order": 3, "time": "15:00", "place": "여의도한강공원", "address": "서울특별시 영등포구 여의동로 330", "lat": 37.5285, "lng": 126.9349, "category": "자연", "summary": "마무리 산책에 좋은 한강 코스", "tip": "바람이 강하면 겉옷을 챙기세요."},
    ],
    "부산": [
        {"day": 1, "order": 1, "time": "10:00", "place": "부산역", "address": "부산광역시 동구 중앙대로 206", "lat": 35.1151, "lng": 129.0415, "category": "이동", "summary": "부산 여행 시작 교통 거점", "tip": "도착 후 교통카드를 확인하세요."},
        {"day": 1, "order": 2, "time": "11:00", "place": "감천문화마을", "address": "부산광역시 사하구 감내2로 203", "lat": 35.0975, "lng": 129.0106, "category": "관광지", "summary": "색감 좋은 골목 산책 코스", "tip": "오르막이 많아 편한 신발이 좋습니다."},
        {"day": 1, "order": 3, "time": "13:00", "place": "자갈치시장", "address": "부산광역시 중구 자갈치해안로 52", "lat": 35.0969, "lng": 129.0305, "category": "맛집", "summary": "해산물 분위기를 느끼는 시장", "tip": "식사 전 가격을 확인하세요."},
        {"day": 1, "order": 4, "time": "15:30", "place": "흰여울문화마을", "address": "부산광역시 영도구 영선동4가", "lat": 35.0789, "lng": 129.0447, "category": "카페", "summary": "바다 전망 골목과 카페 코스", "tip": "바람이 강할 수 있습니다."},
        {"day": 2, "order": 1, "time": "10:00", "place": "해운대해수욕장", "address": "부산광역시 해운대구 우동", "lat": 35.1587, "lng": 129.1604, "category": "자연", "summary": "부산 대표 바다 산책지", "tip": "아침 시간이 비교적 한산합니다."},
        {"day": 2, "order": 2, "time": "11:30", "place": "동백섬", "address": "부산광역시 해운대구 우동 710-1", "lat": 35.1532, "lng": 129.1516, "category": "자연", "summary": "해안 산책과 전망을 보는 코스", "tip": "해운대에서 도보 이동이 편합니다."},
        {"day": 2, "order": 3, "time": "14:00", "place": "센텀시티", "address": "부산광역시 해운대구 센텀남대로 35", "lat": 35.1693, "lng": 129.1308, "category": "문화", "summary": "실내 휴식과 쇼핑을 넣기 좋은 곳", "tip": "비 오는 날 대체 코스로 좋습니다."},
        {"day": 3, "order": 1, "time": "10:00", "place": "광안리해수욕장", "address": "부산광역시 수영구 광안해변로", "lat": 35.1532, "lng": 129.1187, "category": "자연", "summary": "광안대교 전망 산책 코스", "tip": "사진은 해변 중앙 쪽이 좋습니다."},
        {"day": 3, "order": 2, "time": "12:30", "place": "해동용궁사", "address": "부산광역시 기장군 기장읍 용궁길 86", "lat": 35.1883, "lng": 129.2232, "category": "문화", "summary": "바다와 사찰을 함께 보는 코스", "tip": "이동 시간이 길어 여유를 두세요."},
        {"day": 3, "order": 3, "time": "15:30", "place": "국제시장", "address": "부산광역시 중구 신창동4가", "lat": 35.1017, "lng": 129.0291, "category": "맛집", "summary": "기념품과 먹거리를 보는 시장", "tip": "골목이 많아 만날 위치를 정하세요."},
    ],
    "제주": [
        {"day": 1, "order": 1, "time": "10:00", "place": "제주국제공항", "address": "제주특별자치도 제주시 공항로 2", "lat": 33.5067, "lng": 126.4930, "category": "이동", "summary": "제주 여행 시작 지점", "tip": "렌터카/버스 동선을 먼저 확인하세요."},
        {"day": 1, "order": 2, "time": "11:00", "place": "용두암", "address": "제주특별자치도 제주시 용담이동", "lat": 33.5150, "lng": 126.5120, "category": "자연", "summary": "공항 근처 짧은 해안 코스", "tip": "바람이 강한 날은 외투가 필요합니다."},
        {"day": 1, "order": 3, "time": "13:00", "place": "동문시장", "address": "제주특별자치도 제주시 관덕로14길 20", "lat": 33.5116, "lng": 126.5260, "category": "맛집", "summary": "먹거리와 기념품을 보기 좋은 시장", "tip": "야시장 시간도 확인해보세요."},
        {"day": 2, "order": 1, "time": "10:00", "place": "이호테우해변", "address": "제주특별자치도 제주시 이호일동", "lat": 33.4996, "lng": 126.4527, "category": "자연", "summary": "말 등대가 보이는 해변 코스", "tip": "노을 시간대가 특히 좋습니다."},
        {"day": 2, "order": 2, "time": "13:00", "place": "한림공원", "address": "제주특별자치도 제주시 한림읍 한림로 300", "lat": 33.3898, "lng": 126.2397, "category": "관광지", "summary": "식물원과 동굴을 함께 보는 코스", "tip": "관람 시간을 넉넉히 잡으세요."},
        {"day": 2, "order": 3, "time": "15:30", "place": "협재해수욕장", "address": "제주특별자치도 제주시 한림읍 협재리 2497-1", "lat": 33.3949, "lng": 126.2396, "category": "자연", "summary": "맑은 바다색이 좋은 해변", "tip": "바람과 물때를 확인하세요."},
        {"day": 3, "order": 1, "time": "10:00", "place": "오설록 티뮤지엄", "address": "제주특별자치도 서귀포시 안덕면 신화역사로 15", "lat": 33.3059, "lng": 126.2895, "category": "카페", "summary": "차밭과 디저트를 즐기는 코스", "tip": "주말에는 대기 시간이 있습니다."},
        {"day": 3, "order": 2, "time": "13:00", "place": "이중섭거리", "address": "제주특별자치도 서귀포시 이중섭로", "lat": 33.2459, "lng": 126.5644, "category": "문화", "summary": "서귀포 예술 산책 거리", "tip": "근처 카페와 묶기 좋습니다."},
        {"day": 3, "order": 3, "time": "15:30", "place": "성산일출봉", "address": "제주특별자치도 서귀포시 성산읍 일출로 284-12", "lat": 33.4581, "lng": 126.9425, "category": "자연", "summary": "제주 동쪽 대표 전망 명소", "tip": "동선이 길어 출발 시간을 확인하세요."},
    ],
    "창원": [
        {"day": 1, "order": 1, "time": "10:00", "place": "창원중앙역", "address": "경상남도 창원시 의창구 상남로 381", "lat": 35.2585, "lng": 128.6285, "category": "이동", "summary": "창원 여행 시작 교통 거점", "tip": "버스 환승 시간을 먼저 확인하세요."},
        {"day": 1, "order": 2, "time": "11:00", "place": "용지호수공원", "address": "경상남도 창원시 성산구 용지동", "lat": 35.2280, "lng": 128.6816, "category": "자연", "summary": "호수와 산책로가 이어지는 도심 공원", "tip": "점심 전 가볍게 걷기 좋습니다."},
        {"day": 1, "order": 3, "time": "13:00", "place": "창원의집", "address": "경상남도 창원시 의창구 사림로16번길 59", "lat": 35.2369, "lng": 128.6763, "category": "문화", "summary": "전통 가옥과 정원을 보는 코스", "tip": "용지호수와 묶으면 동선이 좋습니다."},
        {"day": 1, "order": 4, "time": "15:30", "place": "창원수목원", "address": "경상남도 창원시 성산구 삼동동", "lat": 35.2289, "lng": 128.7078, "category": "자연", "summary": "꽃과 온실을 천천히 둘러보는 곳", "tip": "날씨가 좋을 때 사진 남기기 좋습니다."},
        {"day": 2, "order": 1, "time": "10:00", "place": "창동예술촌", "address": "경상남도 창원시 마산합포구 오동서6길 24", "lat": 35.2076, "lng": 128.5773, "category": "문화", "summary": "골목 전시와 작은 상점이 모인 거리", "tip": "근처 카페까지 천천히 둘러보세요."},
        {"day": 2, "order": 2, "time": "12:30", "place": "마산어시장", "address": "경상남도 창원시 마산합포구 복요리로 7", "lat": 35.2049, "lng": 128.5776, "category": "맛집", "summary": "해산물과 시장 분위기를 즐기는 코스", "tip": "식사 전 가격과 영업 시간을 확인하세요."},
        {"day": 2, "order": 3, "time": "15:00", "place": "창원NC파크", "address": "경상남도 창원시 마산회원구 삼호로 63", "lat": 35.2227, "lng": 128.5822, "category": "관광지", "summary": "야구장 주변 산책과 사진 코스", "tip": "경기일에는 주변이 붐빌 수 있습니다."},
        {"day": 3, "order": 1, "time": "10:00", "place": "여좌천 로망스다리", "address": "경상남도 창원시 진해구 여좌동", "lat": 35.1578, "lng": 128.6614, "category": "관광지", "summary": "진해의 대표 산책 명소", "tip": "벚꽃철 외에도 조용히 걷기 좋습니다."},
        {"day": 3, "order": 2, "time": "12:30", "place": "경화역공원", "address": "경상남도 창원시 진해구 진해대로 649", "lat": 35.1546, "lng": 128.6883, "category": "관광지", "summary": "철길 감성이 있는 공원 코스", "tip": "여좌천과 같은 날 묶기 좋습니다."},
        {"day": 3, "order": 3, "time": "15:00", "place": "진해루", "address": "경상남도 창원시 진해구 진희로 142", "lat": 35.1495, "lng": 128.6602, "category": "자연", "summary": "바다를 보며 쉬어가는 해안 산책지", "tip": "해 질 무렵 바람을 고려하세요."},
        {"day": 4, "order": 1, "time": "10:00", "place": "주남저수지", "address": "경상남도 창원시 의창구 동읍 주남로101번길", "lat": 35.3146, "lng": 128.6729, "category": "자연", "summary": "철새와 넓은 풍경을 보는 자연 코스", "tip": "이동 시간이 있어 오전 방문이 편합니다."},
        {"day": 4, "order": 2, "time": "13:00", "place": "저도 콰이강의 다리", "address": "경상남도 창원시 마산합포구 구산면 해양관광로 1872-60", "lat": 35.0676, "lng": 128.5824, "category": "관광지", "summary": "바다 전망과 스카이워크를 즐기는 곳", "tip": "바람이 강한 날은 체감 온도를 확인하세요."},
        {"day": 4, "order": 3, "time": "15:30", "place": "진해해양공원", "address": "경상남도 창원시 진해구 명동로 62", "lat": 35.0934, "lng": 128.7156, "category": "관광지", "summary": "해양 전시와 전망을 함께 보는 코스", "tip": "대중교통 배차를 미리 확인하세요."},
    ],
    "강릉": [
        {"day": 1, "order": 1, "time": "10:00", "place": "강릉역", "address": "강원특별자치도 강릉시 용지로 176", "lat": 37.7641, "lng": 128.8994, "category": "이동", "summary": "강릉 여행 시작 교통 거점", "tip": "버스 배차를 먼저 확인하세요."},
        {"day": 1, "order": 2, "time": "11:00", "place": "오죽헌", "address": "강원특별자치도 강릉시 율곡로3139번길 24", "lat": 37.7791, "lng": 128.8786, "category": "문화", "summary": "역사와 정원을 함께 보는 코스", "tip": "실내 전시도 함께 둘러보세요."},
        {"day": 1, "order": 3, "time": "13:30", "place": "경포대", "address": "강원특별자치도 강릉시 경포로 365", "lat": 37.7956, "lng": 128.8966, "category": "관광지", "summary": "호수와 바다 동선을 잇는 명소", "tip": "경포호 산책을 곁들이기 좋습니다."},
        {"day": 1, "order": 4, "time": "15:30", "place": "안목해변", "address": "강원특별자치도 강릉시 창해로14번길", "lat": 37.7720, "lng": 128.9480, "category": "카페", "summary": "커피거리와 바다를 함께 즐기는 곳", "tip": "창가 좌석은 빨리 차는 편입니다."},
        {"day": 2, "order": 1, "time": "10:00", "place": "초당순두부마을", "address": "강원특별자치도 강릉시 초당순두부길", "lat": 37.7917, "lng": 128.9148, "category": "맛집", "summary": "강릉 대표 먹거리 코스", "tip": "점심 전 방문하면 덜 붐빕니다."},
        {"day": 2, "order": 2, "time": "12:30", "place": "선교장", "address": "강원특별자치도 강릉시 운정길 63", "lat": 37.7860, "lng": 128.8851, "category": "문화", "summary": "고택과 정원을 보는 코스", "tip": "조용히 산책하기 좋습니다."},
        {"day": 2, "order": 3, "time": "15:00", "place": "강문해변", "address": "강원특별자치도 강릉시 강문동", "lat": 37.7975, "lng": 128.9172, "category": "자연", "summary": "바다 사진을 남기기 좋은 해변", "tip": "강문 솟대다리도 함께 보세요."},
        {"day": 3, "order": 1, "time": "10:00", "place": "주문진항", "address": "강원특별자치도 강릉시 주문진읍 해안로", "lat": 37.8924, "lng": 128.8252, "category": "맛집", "summary": "해산물과 항구 분위기 코스", "tip": "이동 시간이 길어 일찍 출발하세요."},
        {"day": 3, "order": 2, "time": "13:30", "place": "강릉중앙시장", "address": "강원특별자치도 강릉시 금성로 21", "lat": 37.7555, "lng": 128.8991, "category": "맛집", "summary": "기념품과 간식을 사기 좋은 시장", "tip": "마지막 이동 전 들르기 좋습니다."},
    ],
}


def build_catalog_template(region: str, hub: tuple[Any, ...], places: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    hub_place, hub_address, hub_lat, hub_lng = hub[:4]
    hub_summary = hub[4] if len(hub) > 4 else f"{region} 여행 시작 교통 거점"
    template = [
        {
            "day": 1,
            "order": 1,
            "time": "10:00",
            "place": hub_place,
            "address": hub_address,
            "lat": hub_lat,
            "lng": hub_lng,
            "category": "이동",
            "summary": hub_summary,
            "tip": "도착 후 짐 보관과 교통카드를 먼저 확인하세요.",
        }
    ]
    times = ["11:00", "13:00", "15:30"]
    for index, item in enumerate(places):
        place, address, lat, lng, category = item[:5]
        summary = item[5] if len(item) > 5 else f"{region}에서 많이 찾는 {category} 코스"
        tip = item[6] if len(item) > 6 else "운영 시간과 이동 시간을 출발 전 확인하세요."
        template.append(
            {
                "day": index // 3 + 1,
                "order": index % 3 + 2,
                "time": times[index % 3],
                "place": place,
                "address": address,
                "lat": lat,
                "lng": lng,
                "category": category,
                "summary": summary,
                "tip": tip,
            }
        )
    return template


REGION_PLACE_CATALOG: dict[str, dict[str, Any]] = {
    "대구": {
        "hub": ("동대구역", "대구광역시 동구 동대구로 550", 35.8797, 128.6288),
        "places": [
            ("김광석 다시그리기길", "대구광역시 중구 대봉동", 35.8585, 128.6061, "문화"),
            ("서문시장", "대구광역시 중구 큰장로26길 45", 35.8695, 128.5807, "맛집"),
            ("대구 근대골목", "대구광역시 중구 계산동2가", 35.8663, 128.5900, "문화"),
            ("동성로", "대구광역시 중구 동성로", 35.8693, 128.5957, "쇼핑"),
            ("국채보상운동기념공원", "대구광역시 중구 국채보상로 670", 35.8685, 128.6017, "공원"),
            ("수성못", "대구광역시 수성구 두산동", 35.8294, 128.6175, "자연"),
            ("앞산전망대", "대구광역시 남구 앞산순환로 574-87", 35.8328, 128.5848, "전망"),
            ("이월드", "대구광역시 달서구 두류공원로 200", 35.8531, 128.5635, "관광지"),
            ("대구수목원", "대구광역시 달서구 화암로 342", 35.8016, 128.5201, "자연"),
            ("팔공산케이블카", "대구광역시 동구 팔공산로185길 51", 35.9914, 128.6950, "자연"),
            ("대구예술발전소", "대구광역시 중구 달성로22길 31-12", 35.8752, 128.5886, "문화"),
        ],
    },
    "대전": {
        "hub": ("대전역", "대전광역시 동구 중앙로 215", 36.3320, 127.4343),
        "places": [
            ("성심당 본점", "대전광역시 중구 대종로480번길 15", 36.3277, 127.4271, "맛집"),
            ("으능정이문화의거리", "대전광역시 중구 은행동", 36.3274, 127.4289, "문화"),
            ("한밭수목원", "대전광역시 서구 둔산대로 169", 36.3693, 127.3887, "자연"),
            ("국립중앙과학관", "대전광역시 유성구 대덕대로 481", 36.3765, 127.3751, "문화"),
            ("엑스포과학공원", "대전광역시 유성구 대덕대로 480", 36.3743, 127.3893, "관광지"),
            ("대동하늘공원", "대전광역시 동구 동대전로110번길 182", 36.3292, 127.4558, "전망"),
            ("유성온천공원", "대전광역시 유성구 봉명동", 36.3546, 127.3412, "휴식"),
            ("장태산자연휴양림", "대전광역시 서구 장안로 461", 36.2184, 127.3395, "자연"),
            ("뿌리공원", "대전광역시 중구 뿌리공원로 79", 36.2853, 127.3867, "공원"),
            ("계족산황톳길", "대전광역시 대덕구 장동 산85", 36.4182, 127.4415, "자연"),
            ("대전오월드", "대전광역시 중구 사정공원로 70", 36.2898, 127.3976, "관광지"),
        ],
    },
    "광주": {
        "hub": ("광주송정역", "광주광역시 광산구 상무대로 201", 35.1378, 126.7937),
        "places": [
            ("국립아시아문화전당", "광주광역시 동구 문화전당로 38", 35.1469, 126.9197, "문화"),
            ("양림동역사문화마을", "광주광역시 남구 양림동", 35.1397, 126.9141, "문화"),
            ("1913송정역시장", "광주광역시 광산구 송정로8번길 13", 35.1384, 126.7933, "맛집"),
            ("펭귄마을", "광주광역시 남구 천변좌로446번길 7", 35.1410, 126.9140, "문화"),
            ("무등산국립공원", "광주광역시 동구 증심사길 71", 35.1341, 126.9888, "자연"),
            ("충장로", "광주광역시 동구 충장로", 35.1496, 126.9146, "쇼핑"),
            ("대인예술시장", "광주광역시 동구 제봉로184번길 9-10", 35.1536, 126.9190, "시장"),
            ("광주호호수생태원", "광주광역시 북구 충효샘길 7", 35.1770, 127.0000, "자연"),
            ("중외공원", "광주광역시 북구 하서로 52", 35.1830, 126.8830, "공원"),
            ("김대중컨벤션센터", "광주광역시 서구 상무누리로 30", 35.1467, 126.8406, "문화"),
            ("우치공원", "광주광역시 북구 우치로 677", 35.2217, 126.8938, "공원"),
        ],
    },
    "인천": {
        "hub": ("인천역", "인천광역시 중구 제물량로 269", 37.4764, 126.6169),
        "places": [
            ("인천 차이나타운", "인천광역시 중구 차이나타운로59번길", 37.4755, 126.6194, "맛집"),
            ("송월동 동화마을", "인천광역시 중구 동화마을길 38", 37.4776, 126.6222, "관광지"),
            ("월미도", "인천광역시 중구 월미문화로", 37.4716, 126.5965, "관광지"),
            ("개항장거리", "인천광역시 중구 신포로27번길", 37.4738, 126.6210, "문화"),
            ("신포국제시장", "인천광역시 중구 우현로49번길 11-5", 37.4719, 126.6287, "맛집"),
            ("송도센트럴파크", "인천광역시 연수구 컨벤시아대로 160", 37.3927, 126.6395, "공원"),
            ("인천대공원", "인천광역시 남동구 무네미로 236", 37.4562, 126.7520, "자연"),
            ("소래포구", "인천광역시 남동구 소래역로", 37.4007, 126.7332, "맛집"),
            ("을왕리해수욕장", "인천광역시 중구 을왕동", 37.4497, 126.3710, "해변"),
            ("아라뱃길 아라마루전망대", "인천광역시 계양구 둑실동", 37.5738, 126.7412, "전망"),
            ("강화평화전망대", "인천광역시 강화군 양사면 전망대로 797", 37.8271, 126.4321, "전망"),
        ],
    },
    "수원": {
        "hub": ("수원역", "경기도 수원시 팔달구 덕영대로 924", 37.2661, 127.0000),
        "places": [
            ("수원화성", "경기도 수원시 장안구 영화동 320-2", 37.2871, 127.0113, "문화"),
            ("화성행궁", "경기도 수원시 팔달구 정조로 825", 37.2819, 127.0142, "문화"),
            ("행리단길", "경기도 수원시 팔달구 화서문로", 37.2844, 127.0131, "카페"),
            ("팔달문시장", "경기도 수원시 팔달구 팔달문로 9", 37.2773, 127.0173, "맛집"),
            ("광교호수공원", "경기도 수원시 영통구 광교호수로 57", 37.2830, 127.0650, "자연"),
            ("방화수류정", "경기도 수원시 팔달구 수원천로392번길 44-6", 37.2888, 127.0173, "관광지"),
            ("수원통닭거리", "경기도 수원시 팔달구 팔달로1가", 37.2786, 127.0172, "맛집"),
            ("수원시립아이파크미술관", "경기도 수원시 팔달구 정조로 833", 37.2826, 127.0148, "문화"),
            ("수원월드컵경기장", "경기도 수원시 팔달구 월드컵로 310", 37.2864, 127.0369, "관광지"),
            ("일월수목원", "경기도 수원시 장안구 일월로 61", 37.2896, 126.9719, "자연"),
            ("수원박물관", "경기도 수원시 영통구 창룡대로 265", 37.2972, 127.0476, "문화"),
        ],
    },
    "전주": {
        "hub": ("전주역", "전북특별자치도 전주시 덕진구 동부대로 680", 35.8498, 127.1617),
        "places": [
            ("전주한옥마을", "전북특별자치도 전주시 완산구 기린대로 99", 35.8152, 127.1537, "문화"),
            ("경기전", "전북특별자치도 전주시 완산구 태조로 44", 35.8150, 127.1499, "문화"),
            ("전동성당", "전북특별자치도 전주시 완산구 태조로 51", 35.8135, 127.1490, "문화"),
            ("남부시장", "전북특별자치도 전주시 완산구 풍남문1길 19-3", 35.8125, 127.1450, "맛집"),
            ("자만벽화마을", "전북특별자치도 전주시 완산구 교동", 35.8178, 127.1574, "관광지"),
            ("오목대", "전북특별자치도 전주시 완산구 기린대로 55", 35.8158, 127.1560, "전망"),
            ("전주향교", "전북특별자치도 전주시 완산구 향교길 139", 35.8112, 127.1579, "문화"),
            ("덕진공원", "전북특별자치도 전주시 덕진구 권삼득로 390", 35.8467, 127.1219, "자연"),
            ("팔복예술공장", "전북특별자치도 전주시 덕진구 구렛들1길 46", 35.8523, 127.0973, "문화"),
            ("전주수목원", "전북특별자치도 전주시 덕진구 번영로 462-45", 35.8722, 127.0645, "자연"),
            ("객리단길", "전북특별자치도 전주시 완산구 전주객사길", 35.8190, 127.1437, "카페"),
        ],
    },
    "여수": {
        "hub": ("여수엑스포역", "전라남도 여수시 망양로 2", 34.7527, 127.7479),
        "places": [
            ("여수해상케이블카", "전라남도 여수시 돌산읍 돌산로 3600-1", 34.7304, 127.7447, "관광지"),
            ("오동도", "전라남도 여수시 수정동 산1-11", 34.7446, 127.7662, "자연"),
            ("이순신광장", "전라남도 여수시 중앙동", 34.7396, 127.7376, "관광지"),
            ("여수낭만포차거리", "전라남도 여수시 하멜로 102", 34.7381, 127.7428, "맛집"),
            ("여수수산시장", "전라남도 여수시 여객선터미널길 24", 34.7368, 127.7332, "맛집"),
            ("고소동 천사벽화마을", "전라남도 여수시 고소동", 34.7404, 127.7420, "문화"),
            ("돌산공원", "전라남도 여수시 돌산읍 우두리", 34.7275, 127.7387, "전망"),
            ("향일암", "전라남도 여수시 돌산읍 향일암로 60", 34.5939, 127.8022, "문화"),
            ("만성리검은모래해변", "전라남도 여수시 만흥동", 34.7775, 127.7451, "해변"),
            ("아쿠아플라넷 여수", "전라남도 여수시 오동도로 61-11", 34.7450, 127.7468, "관광지"),
            ("여수예술랜드", "전라남도 여수시 돌산읍 무술목길 142-1", 34.7000, 127.7780, "관광지"),
        ],
    },
    "경주": {
        "hub": ("경주역", "경상북도 경주시 건천읍 신경주역로 80", 35.7983, 129.1393),
        "places": [
            ("대릉원", "경상북도 경주시 황남동 31-1", 35.8377, 129.2137, "문화"),
            ("첨성대", "경상북도 경주시 인왕동 839-1", 35.8347, 129.2187, "문화"),
            ("동궁과 월지", "경상북도 경주시 원화로 102", 35.8349, 129.2266, "문화"),
            ("황리단길", "경상북도 경주시 포석로 1080", 35.8371, 129.2093, "카페"),
            ("월정교", "경상북도 경주시 교동 274", 35.8295, 129.2180, "관광지"),
            ("교촌마을", "경상북도 경주시 교촌길 39-2", 35.8290, 129.2164, "문화"),
            ("국립경주박물관", "경상북도 경주시 일정로 186", 35.8293, 129.2281, "문화"),
            ("보문호", "경상북도 경주시 보문로", 35.8452, 129.2823, "자연"),
            ("경주월드", "경상북도 경주시 보문로 544", 35.8360, 129.2825, "관광지"),
            ("불국사", "경상북도 경주시 불국로 385", 35.7900, 129.3320, "문화"),
            ("석굴암", "경상북도 경주시 불국로 873-243", 35.7947, 129.3490, "문화"),
        ],
    },
    "울산": {
        "hub": ("울산역", "울산광역시 울주군 삼남읍 울산역로 177", 35.5514, 129.1387),
        "places": [
            ("태화강 국가정원", "울산광역시 중구 태화강국가정원길 154", 35.5510, 129.2970, "자연"),
            ("장생포고래문화마을", "울산광역시 남구 장생포고래로 244", 35.5036, 129.3804, "문화"),
            ("대왕암공원", "울산광역시 동구 등대로 95", 35.4927, 129.4374, "자연"),
            ("간절곶", "울산광역시 울주군 서생면 대송리", 35.3592, 129.3603, "전망"),
            ("울산대공원", "울산광역시 남구 대공원로 94", 35.5324, 129.2935, "공원"),
            ("일산해수욕장", "울산광역시 동구 일산동", 35.4954, 129.4305, "해변"),
            ("슬도", "울산광역시 동구 방어동", 35.4848, 129.4396, "자연"),
            ("성남동 젊음의거리", "울산광역시 중구 성남동", 35.5543, 129.3207, "쇼핑"),
            ("울산박물관", "울산광역시 남구 두왕로 277", 35.5272, 129.3085, "문화"),
            ("반구대암각화", "울산광역시 울주군 언양읍 대곡리", 35.6115, 129.1747, "문화"),
            ("장생포고래박물관", "울산광역시 남구 장생포고래로 244", 35.5037, 129.3806, "문화"),
        ],
    },
    "청주": {
        "hub": ("청주고속버스터미널", "충청북도 청주시 흥덕구 2순환로 1229", 36.6267, 127.4310),
        "places": [
            ("상당산성", "충청북도 청주시 상당구 산성동", 36.6618, 127.5365, "문화"),
            ("수암골", "충청북도 청주시 상당구 수동", 36.6426, 127.4944, "문화"),
            ("육거리종합시장", "충청북도 청주시 상당구 청남로2197번길 46", 36.6304, 127.4903, "맛집"),
            ("국립청주박물관", "충청북도 청주시 상당구 명암로 143", 36.6522, 127.5124, "문화"),
            ("청주고인쇄박물관", "충청북도 청주시 흥덕구 직지대로 713", 36.6466, 127.4710, "문화"),
            ("청남대", "충청북도 청주시 상당구 문의면 청남대길 646", 36.4612, 127.4924, "관광지"),
            ("문의문화재단지", "충청북도 청주시 상당구 문의면 대청호반로 721", 36.5147, 127.4933, "문화"),
            ("무심천", "충청북도 청주시 서원구 사직동", 36.6352, 127.4767, "자연"),
            ("성안길", "충청북도 청주시 상당구 성안로", 36.6358, 127.4892, "쇼핑"),
            ("오창호수공원", "충청북도 청주시 청원구 오창읍 오창공원로 311", 36.7136, 127.4284, "공원"),
            ("청주랜드", "충청북도 청주시 상당구 명암로 171", 36.6502, 127.5154, "관광지"),
        ],
    },
    "춘천": {
        "hub": ("춘천역", "강원특별자치도 춘천시 공지로 591", 37.8845, 127.7167),
        "places": [
            ("소양강스카이워크", "강원특별자치도 춘천시 영서로 2663", 37.8937, 127.7245, "전망"),
            ("춘천명동닭갈비골목", "강원특별자치도 춘천시 금강로62번길", 37.8800, 127.7270, "맛집"),
            ("공지천유원지", "강원특별자치도 춘천시 이디오피아길 25", 37.8739, 127.7022, "자연"),
            ("김유정문학촌", "강원특별자치도 춘천시 신동면 김유정로 1430-14", 37.8181, 127.7140, "문화"),
            ("강촌레일파크", "강원특별자치도 춘천시 신동면 김유정로 1383", 37.8155, 127.7142, "관광지"),
            ("삼악산호수케이블카", "강원특별자치도 춘천시 스포츠타운길 245", 37.8672, 127.6919, "전망"),
            ("애니메이션박물관", "강원특별자치도 춘천시 서면 박사로 854", 37.8937, 127.6916, "문화"),
            ("구봉산카페거리", "강원특별자치도 춘천시 동면 순환대로", 37.9027, 127.7738, "카페"),
            ("소양강댐", "강원특별자치도 춘천시 신북읍 신샘밭로 1128", 37.9458, 127.8144, "자연"),
            ("남이섬", "강원특별자치도 춘천시 남산면 남이섬길 1", 37.7914, 127.5256, "자연"),
            ("의암호스카이워크", "강원특별자치도 춘천시 칠전동", 37.8484, 127.6824, "전망"),
        ],
    },
    "속초": {
        "hub": ("속초시외버스터미널", "강원특별자치도 속초시 장안로 16", 38.2070, 128.5918),
        "places": [
            ("속초관광수산시장", "강원특별자치도 속초시 중앙로147번길 12", 38.2040, 128.5905, "맛집"),
            ("영금정", "강원특별자치도 속초시 영금정로 43", 38.2121, 128.6015, "전망"),
            ("속초해수욕장", "강원특별자치도 속초시 해오름로 190", 38.1906, 128.6031, "해변"),
            ("아바이마을", "강원특별자치도 속초시 청호로 122", 38.1988, 128.5965, "문화"),
            ("청초호", "강원특별자치도 속초시 청초호반로", 38.1954, 128.5867, "자연"),
            ("설악산국립공원", "강원특별자치도 속초시 설악산로 833", 38.1679, 128.4844, "자연"),
            ("신흥사", "강원특별자치도 속초시 설악산로 1137", 38.1701, 128.4877, "문화"),
            ("대포항", "강원특별자치도 속초시 대포항길 64", 38.1745, 128.6070, "맛집"),
            ("외옹치 바다향기로", "강원특별자치도 속초시 대포동", 38.1847, 128.6097, "해변"),
            ("속초엑스포타워", "강원특별자치도 속초시 엑스포로 72", 38.1924, 128.5862, "전망"),
            ("칠성조선소", "강원특별자치도 속초시 중앙로46번길 45", 38.1991, 128.5872, "카페"),
        ],
    },
    "포항": {
        "hub": ("포항역", "경상북도 포항시 북구 흥해읍 포항역로 1", 36.0714, 129.3425),
        "places": [
            ("영일대해수욕장", "경상북도 포항시 북구 해안로 95", 36.0563, 129.3786, "해변"),
            ("죽도시장", "경상북도 포항시 북구 죽도시장13길 13", 36.0339, 129.3650, "맛집"),
            ("호미곶", "경상북도 포항시 남구 호미곶면 대보리", 36.0771, 129.5664, "전망"),
            ("스페이스워크", "경상북도 포항시 북구 환호공원길 30", 36.0707, 129.3937, "관광지"),
            ("구룡포 일본인가옥거리", "경상북도 포항시 남구 구룡포읍 구룡포길 153-1", 35.9907, 129.5593, "문화"),
            ("포항운하", "경상북도 포항시 남구 희망대로 1040", 36.0321, 129.3784, "자연"),
            ("환호공원", "경상북도 포항시 북구 환호공원길 30", 36.0715, 129.3926, "공원"),
            ("오어사", "경상북도 포항시 남구 오천읍 오어로 1", 35.9256, 129.3160, "문화"),
            ("연오랑세오녀테마공원", "경상북도 포항시 남구 동해면 호미로 3012", 35.9764, 129.4754, "관광지"),
            ("포항시립미술관", "경상북도 포항시 북구 환호공원길 10", 36.0692, 129.3910, "문화"),
            ("이가리 닻 전망대", "경상북도 포항시 북구 청하면 이가리", 36.1888, 129.3829, "전망"),
        ],
    },
    "목포": {
        "hub": ("목포역", "전라남도 목포시 영산로 98", 34.7912, 126.3868),
        "places": [
            ("목포근대역사관", "전라남도 목포시 영산로29번길 6", 34.7871, 126.3813, "문화"),
            ("유달산", "전라남도 목포시 죽교동 산27-3", 34.7907, 126.3737, "자연"),
            ("목포해상케이블카", "전라남도 목포시 해양대학로 240", 34.7934, 126.3662, "전망"),
            ("갓바위", "전라남도 목포시 용해동", 34.7913, 126.4234, "자연"),
            ("평화광장", "전라남도 목포시 상동", 34.7988, 126.4342, "관광지"),
            ("목포종합수산시장", "전라남도 목포시 해안로 265-4", 34.7840, 126.3847, "맛집"),
            ("서산동 시화골목", "전라남도 목포시 서산동", 34.7820, 126.3758, "문화"),
            ("삼학도", "전라남도 목포시 산정동", 34.7827, 126.3947, "공원"),
            ("국립해양문화재연구소", "전라남도 목포시 남농로 136", 34.7911, 126.4237, "문화"),
            ("목포자연사박물관", "전라남도 목포시 남농로 135", 34.7920, 126.4242, "문화"),
            ("목포 춤추는 바다분수", "전라남도 목포시 미항로 115", 34.7985, 126.4312, "관광지"),
        ],
    },
    "통영": {
        "hub": ("통영종합버스터미널", "경상남도 통영시 광도면 죽림4로 24", 34.8826, 128.4162),
        "places": [
            ("동피랑벽화마을", "경상남도 통영시 동피랑1길 6-18", 34.8446, 128.4247, "문화"),
            ("통영중앙시장", "경상남도 통영시 중앙시장1길 14-16", 34.8443, 128.4228, "맛집"),
            ("통영케이블카", "경상남도 통영시 발개로 205", 34.8270, 128.4266, "전망"),
            ("미륵산", "경상남도 통영시 산양읍", 34.8114, 128.4214, "자연"),
            ("이순신공원", "경상남도 통영시 멘데해안길 205", 34.8390, 128.4472, "공원"),
            ("서피랑", "경상남도 통영시 서호동", 34.8420, 128.4175, "문화"),
            ("남망산조각공원", "경상남도 통영시 남망공원길 29", 34.8411, 128.4282, "공원"),
            ("삼도수군통제영", "경상남도 통영시 세병로 27", 34.8459, 128.4202, "문화"),
            ("통영해저터널", "경상남도 통영시 도천길 1", 34.8314, 128.4075, "관광지"),
            ("달아공원", "경상남도 통영시 산양읍 산양일주로 1115", 34.7757, 128.3978, "전망"),
            ("박경리기념관", "경상남도 통영시 산양읍 산양중앙로 173", 34.7898, 128.3943, "문화"),
        ],
    },
    "순천": {
        "hub": ("순천역", "전라남도 순천시 팔마로 135", 34.9468, 127.5033),
        "places": [
            ("순천만국가정원", "전라남도 순천시 국가정원1호길 47", 34.9304, 127.5100, "자연"),
            ("순천만습지", "전라남도 순천시 순천만길 513-25", 34.8854, 127.5096, "자연"),
            ("낙안읍성", "전라남도 순천시 낙안면 충민길 30", 34.9067, 127.3423, "문화"),
            ("순천드라마촬영장", "전라남도 순천시 비례골길 24", 34.9656, 127.5370, "문화"),
            ("순천웃장", "전라남도 순천시 북부시장3길 67", 34.9551, 127.4877, "맛집"),
            ("순천문화의거리", "전라남도 순천시 행동", 34.9537, 127.4843, "문화"),
            ("조례호수공원", "전라남도 순천시 조례동", 34.9549, 127.5224, "공원"),
            ("선암사", "전라남도 순천시 승주읍 선암사길 450", 35.0013, 127.3276, "문화"),
            ("송광사", "전라남도 순천시 송광면 송광사안길 100", 35.0025, 127.2753, "문화"),
            ("와온해변", "전라남도 순천시 해룡면 상내리", 34.8640, 127.5379, "해변"),
            ("순천향교", "전라남도 순천시 금곡길 30", 34.9572, 127.4836, "문화"),
        ],
    },
    "안동": {
        "hub": ("안동역", "경상북도 안동시 경동로 122-16", 36.5590, 128.7305),
        "places": [
            ("안동하회마을", "경상북도 안동시 풍천면 하회종가길 40", 36.5394, 128.5187, "문화"),
            ("월영교", "경상북도 안동시 상아동", 36.5768, 128.7607, "전망"),
            ("안동찜닭골목", "경상북도 안동시 번영길 30", 36.5652, 128.7314, "맛집"),
            ("도산서원", "경상북도 안동시 도산면 도산서원길 154", 36.7275, 128.8433, "문화"),
            ("병산서원", "경상북도 안동시 풍천면 병산길 386", 36.5740, 128.5819, "문화"),
            ("봉정사", "경상북도 안동시 서후면 봉정사길 222", 36.6536, 128.6638, "문화"),
            ("안동민속촌", "경상북도 안동시 민속촌길 13", 36.5741, 128.7596, "문화"),
            ("낙강물길공원", "경상북도 안동시 상아동", 36.5804, 128.7638, "공원"),
            ("안동구시장", "경상북도 안동시 번영길 30", 36.5654, 128.7315, "시장"),
            ("유교랜드", "경상북도 안동시 관광단지로 346-30", 36.5678, 128.7855, "문화"),
            ("만휴정", "경상북도 안동시 길안면 묵계하리길 42", 36.4566, 128.8837, "문화"),
        ],
    },
    "원주": {
        "hub": ("원주역", "강원특별자치도 원주시 북원로 1860", 37.3368, 127.9480),
        "places": [
            ("소금산그랜드밸리", "강원특별자치도 원주시 지정면 소금산길 12", 37.3648, 127.8341, "전망"),
            ("뮤지엄 산", "강원특별자치도 원주시 지정면 오크밸리2길 260", 37.4180, 127.8278, "문화"),
            ("원주중앙시장", "강원특별자치도 원주시 중앙시장길 6", 37.3504, 127.9498, "맛집"),
            ("강원감영", "강원특별자치도 원주시 원일로 85", 37.3486, 127.9507, "문화"),
            ("박경리문학공원", "강원특별자치도 원주시 토지길 1", 37.3329, 127.9487, "문화"),
            ("반곡역", "강원특별자치도 원주시 달마중3길 30", 37.3282, 127.9789, "관광지"),
            ("원주한지테마파크", "강원특별자치도 원주시 한지공원길 151", 37.3422, 127.9325, "문화"),
            ("행구수변공원", "강원특별자치도 원주시 행구동", 37.3420, 128.0034, "공원"),
            ("치악산국립공원", "강원특별자치도 원주시 소초면 무쇠점2길 26", 37.3715, 128.0500, "자연"),
            ("구룡사", "강원특별자치도 원주시 소초면 구룡사로 500", 37.4107, 128.0507, "문화"),
            ("간현관광지", "강원특별자치도 원주시 지정면 소금산길 12", 37.3653, 127.8339, "관광지"),
        ],
    },
    "군산": {
        "hub": ("군산역", "전북특별자치도 군산시 내흥2길 197", 35.9997, 126.7617),
        "places": [
            ("군산근대역사박물관", "전북특별자치도 군산시 해망로 240", 35.9878, 126.7112, "문화"),
            ("초원사진관", "전북특별자치도 군산시 구영2길 12-1", 35.9877, 126.7087, "문화"),
            ("동국사", "전북특별자치도 군산시 동국사길 16", 35.9828, 126.7082, "문화"),
            ("신흥동 일본식가옥", "전북특별자치도 군산시 구영1길 17", 35.9870, 126.7069, "문화"),
            ("경암동 철길마을", "전북특별자치도 군산시 경촌4길 14", 35.9816, 126.7363, "관광지"),
            ("이성당", "전북특별자치도 군산시 중앙로 177", 35.9871, 126.7115, "맛집"),
            ("은파호수공원", "전북특별자치도 군산시 은파순환길 9", 35.9456, 126.6893, "자연"),
            ("선유도해수욕장", "전북특별자치도 군산시 옥도면 선유도리", 35.8100, 126.4110, "해변"),
            ("비응항", "전북특별자치도 군산시 비응도동", 35.9487, 126.5290, "맛집"),
            ("군산시간여행마을", "전북특별자치도 군산시 장미동", 35.9878, 126.7110, "문화"),
            ("채만식문학관", "전북특별자치도 군산시 강변로 449", 35.9947, 126.7461, "문화"),
        ],
    },
    "세종": {
        "hub": ("세종고속시외버스터미널", "세종특별자치시 갈매로 37-12", 36.5048, 127.2605),
        "places": [
            ("국립세종수목원", "세종특별자치시 수목원로 136", 36.4978, 127.2806, "자연"),
            ("세종호수공원", "세종특별자치시 호수공원길 155", 36.4987, 127.2706, "공원"),
            ("대통령기록관", "세종특별자치시 다솜로 250", 36.5002, 127.2676, "문화"),
            ("세종중앙공원", "세종특별자치시 중앙공원로 60", 36.5020, 127.2805, "공원"),
            ("금강보행교", "세종특별자치시 세종동", 36.4928, 127.2826, "관광지"),
            ("국립세종도서관", "세종특별자치시 다솜3로 48", 36.4990, 127.2639, "문화"),
            ("조치원전통시장", "세종특별자치시 조치원읍 조치원8길 42", 36.6019, 127.2990, "시장"),
            ("베어트리파크", "세종특별자치시 전동면 신송로 217", 36.6710, 127.2060, "자연"),
            ("밀마루전망대", "세종특별자치시 도움3로 58", 36.5056, 127.2604, "전망"),
            ("세종전통시장", "세종특별자치시 조치원읍", 36.6017, 127.2991, "시장"),
            ("영평사", "세종특별자치시 장군면 영평사길 124", 36.4779, 127.2123, "문화"),
        ],
    },
}

REGION_PLACE_CATALOG.update(
    {
        "평택": {
            "hub": ("평택역", "경기도 평택시 평택로 51", 36.9906, 127.0853),
            "places": [
                ("평택호관광단지", "경기도 평택시 현덕면 평택호길 159", 36.9210, 126.9166, "자연"),
                ("안정리 로데오거리", "경기도 평택시 팽성읍 안정리", 36.9612, 127.0471, "맛집"),
                ("평택국제중앙시장", "경기도 평택시 신장동", 37.0816, 127.0528, "시장"),
                ("배다리생태공원", "경기도 평택시 죽백동", 36.9910, 127.1154, "공원"),
                ("소풍정원", "경기도 평택시 고덕면 궁리", 37.0320, 127.0322, "자연"),
                ("진위천유원지", "경기도 평택시 진위면 진위서로 264-15", 37.1052, 127.0906, "자연"),
                ("통복시장", "경기도 평택시 통복시장로25번길 10", 36.9945, 127.0907, "시장"),
                ("평택항 마린센터", "경기도 평택시 포승읍 평택항만길 73", 36.9676, 126.8453, "전망"),
                ("평택농업생태원", "경기도 평택시 오성면 청오로 33-34", 37.0040, 126.9807, "자연"),
                ("내리문화공원", "경기도 평택시 팽성읍 내리", 36.9524, 127.0479, "공원"),
                ("원평나루 갈대숲", "경기도 평택시 팽성읍 원정리", 36.9634, 127.0277, "자연"),
            ],
        },
        "성남": {
            "hub": ("판교역", "경기도 성남시 분당구 판교역로 160", 37.3948, 127.1112),
            "places": [
                ("율동공원", "경기도 성남시 분당구 문정로 145", 37.3808, 127.1494, "공원"),
                ("남한산성", "경기도 광주시 남한산성면 산성리", 37.4787, 127.1816, "문화"),
                ("정자동 카페거리", "경기도 성남시 분당구 정자동", 37.3675, 127.1069, "카페"),
                ("모란민속5일장", "경기도 성남시 중원구 성남동", 37.4325, 127.1296, "시장"),
                ("성남아트센터", "경기도 성남시 분당구 성남대로 808", 37.4031, 127.1292, "문화"),
                ("분당중앙공원", "경기도 성남시 분당구 성남대로 550", 37.3777, 127.1201, "공원"),
                ("한국잡월드", "경기도 성남시 분당구 분당수서로 501", 37.3770, 127.1050, "문화"),
                ("판교 현대백화점", "경기도 성남시 분당구 판교역로146번길 20", 37.3928, 127.1121, "쇼핑"),
                ("신구대학교식물원", "경기도 성남시 수정구 적푸리로 9", 37.4487, 127.0803, "자연"),
                ("판교테크노밸리", "경기도 성남시 분당구 판교로 289", 37.4010, 127.1087, "문화"),
                ("탄천", "경기도 성남시 분당구 야탑동", 37.4090, 127.1286, "자연"),
            ],
        },
        "안산": {
            "hub": ("안산중앙역", "경기도 안산시 단원구 중앙대로 918", 37.3160, 126.8385),
            "places": [
                ("대부도 방아머리해수욕장", "경기도 안산시 단원구 대부북동", 37.2924, 126.5758, "해변"),
                ("안산갈대습지", "경기도 안산시 상록구 갈대습지로 76", 37.2807, 126.8392, "자연"),
                ("시화나래조력공원", "경기도 안산시 단원구 대부황금로 1927", 37.3134, 126.6102, "전망"),
                ("대부해솔길", "경기도 안산시 단원구 대부도", 37.2433, 126.5865, "자연"),
                ("안산문화광장", "경기도 안산시 단원구 광덕대로 157", 37.3123, 126.8291, "문화"),
                ("화랑유원지", "경기도 안산시 단원구 동산로 268", 37.3219, 126.8145, "공원"),
                ("구봉도 낙조전망대", "경기도 안산시 단원구 대부북동", 37.2800, 126.5530, "전망"),
                ("안산다문화거리", "경기도 안산시 단원구 원곡동", 37.3307, 126.7907, "맛집"),
                ("탄도항", "경기도 안산시 단원구 대부황금로 17-34", 37.1894, 126.6458, "전망"),
                ("안산별빛마을포토랜드", "경기도 안산시 상록구 수인로 1723", 37.3002, 126.8721, "관광지"),
                ("노적봉공원", "경기도 안산시 상록구 성포동", 37.3238, 126.8486, "공원"),
            ],
        },
        "부천": {
            "hub": ("부천역", "경기도 부천시 부천로 1", 37.4840, 126.7827),
            "places": [
                ("한국만화박물관", "경기도 부천시 길주로 1", 37.5123, 126.7421, "문화"),
                ("상동호수공원", "경기도 부천시 조마루로 15", 37.5052, 126.7446, "공원"),
                ("부천중앙공원", "경기도 부천시 소향로 162", 37.5035, 126.7651, "공원"),
                ("부천아트벙커B39", "경기도 부천시 삼작로 53", 37.5235, 126.7671, "문화"),
                ("부천자연생태공원", "경기도 부천시 길주로 660", 37.5058, 126.8153, "자연"),
                ("원미산진달래동산", "경기도 부천시 춘의동", 37.5034, 126.7875, "자연"),
                ("부천역곡상상시장", "경기도 부천시 역곡로14번길", 37.4855, 126.8114, "시장"),
                ("웅진플레이도시", "경기도 부천시 조마루로 2", 37.5025, 126.7440, "관광지"),
                ("아인스월드", "경기도 부천시 도약로 1", 37.5095, 126.7447, "관광지"),
                ("부천로보파크", "경기도 부천시 평천로 655", 37.5163, 126.7634, "문화"),
                ("부천호수식물원 수피아", "경기도 부천시 조마루로 15", 37.5056, 126.7454, "자연"),
            ],
        },
        "파주": {
            "hub": ("금촌역", "경기도 파주시 새꽃로 193", 37.7665, 126.7747),
            "places": [
                ("헤이리예술마을", "경기도 파주시 탄현면 헤이리마을길 70-21", 37.7902, 126.6991, "문화"),
                ("파주출판도시", "경기도 파주시 회동길 145", 37.7086, 126.6877, "문화"),
                ("임진각평화누리", "경기도 파주시 문산읍 임진각로 148-40", 37.8892, 126.7400, "공원"),
                ("프로방스마을", "경기도 파주시 탄현면 새오리로 69", 37.7905, 126.6846, "관광지"),
                ("마장호수출렁다리", "경기도 파주시 광탄면 기산로 313", 37.7754, 126.9326, "전망"),
                ("감악산출렁다리", "경기도 파주시 적성면 설마천로 238", 37.9410, 126.9627, "전망"),
                ("파주프리미엄아울렛", "경기도 파주시 탄현면 필승로 200", 37.7696, 126.6963, "쇼핑"),
                ("오두산통일전망대", "경기도 파주시 탄현면 필승로 369", 37.7735, 126.6785, "전망"),
                ("벽초지수목원", "경기도 파주시 광탄면 부흥로 242", 37.7802, 126.9197, "자연"),
                ("운정호수공원", "경기도 파주시 경의로 1151", 37.7242, 126.7543, "공원"),
                ("율곡수목원", "경기도 파주시 파평면 장승배기로 392", 37.9038, 126.8387, "자연"),
            ],
        },
        "남양주": {
            "hub": ("평내호평역", "경기도 남양주시 경춘로 1375", 37.6534, 127.2443),
            "places": [
                ("물의정원", "경기도 남양주시 조안면 북한강로 398", 37.5457, 127.3146, "자연"),
                ("다산정약용유적지", "경기도 남양주시 조안면 다산로747번길 11", 37.5162, 127.3008, "문화"),
                ("수종사", "경기도 남양주시 조안면 북한강로433번길 186", 37.5383, 127.3158, "문화"),
                ("봉선사", "경기도 남양주시 진접읍 봉선사길 32", 37.7468, 127.1828, "문화"),
                ("피아노폭포", "경기도 남양주시 화도읍 폭포로 562", 37.6406, 127.3405, "자연"),
                ("북한강 카페거리", "경기도 남양주시 조안면 북한강로", 37.5486, 127.3039, "카페"),
                ("현대프리미엄아울렛 스페이스원", "경기도 남양주시 다산순환로 50", 37.6167, 127.1538, "쇼핑"),
                ("정약용도서관", "경기도 남양주시 다산중앙로82번안길 138", 37.6171, 127.1584, "문화"),
                ("남양주유기농테마파크", "경기도 남양주시 조안면 북한강로 881", 37.5512, 127.3212, "자연"),
                ("광릉숲길", "경기도 남양주시 진접읍 부평리", 37.7450, 127.1756, "자연"),
                ("별내카페거리", "경기도 남양주시 별내동", 37.6460, 127.1238, "카페"),
            ],
        },
        "의정부": {
            "hub": ("의정부역", "경기도 의정부시 평화로 525", 37.7385, 127.0459),
            "places": [
                ("의정부제일시장", "경기도 의정부시 태평로73번길 20", 37.7417, 127.0509, "시장"),
                ("의정부부대찌개거리", "경기도 의정부시 호국로1309번길", 37.7424, 127.0492, "맛집"),
                ("의정부예술의전당", "경기도 의정부시 의정로 1", 37.7337, 127.0332, "문화"),
                ("직동근린공원", "경기도 의정부시 의정로 1", 37.7310, 127.0339, "공원"),
                ("의정부미술도서관", "경기도 의정부시 민락로 248", 37.7456, 127.1012, "문화"),
                ("행복로", "경기도 의정부시 의정부동", 37.7399, 127.0476, "쇼핑"),
                ("회룡사", "경기도 의정부시 전좌로155번길 262", 37.7246, 127.0303, "문화"),
                ("송산사지근린공원", "경기도 의정부시 민락동", 37.7461, 127.1048, "공원"),
                ("가능동성당", "경기도 의정부시 신흥로 365", 37.7488, 127.0394, "문화"),
                ("추동근린공원", "경기도 의정부시 신곡동", 37.7372, 127.0661, "공원"),
                ("발곡근린공원", "경기도 의정부시 신곡동", 37.7274, 127.0527, "공원"),
            ],
        },
        "안양": {
            "hub": ("안양역", "경기도 안양시 만안구 만안로 244", 37.4018, 126.9227),
            "places": [
                ("안양예술공원", "경기도 안양시 만안구 예술공원로 131", 37.4190, 126.9174, "문화"),
                ("안양중앙시장", "경기도 안양시 만안구 냉천로 196", 37.3975, 126.9231, "시장"),
                ("평촌중앙공원", "경기도 안양시 동안구 관평로 149", 37.3905, 126.9608, "공원"),
                ("안양천", "경기도 안양시 만안구 안양동", 37.4000, 126.9157, "자연"),
                ("삼막사", "경기도 안양시 만안구 삼막로 478", 37.4358, 126.9310, "문화"),
                ("병목안시민공원", "경기도 안양시 만안구 병목안로 215", 37.3895, 126.8995, "공원"),
                ("김중업건축박물관", "경기도 안양시 만안구 예술공원로103번길 4", 37.4194, 126.9186, "문화"),
                ("안양1번가", "경기도 안양시 만안구 안양로292번길", 37.3995, 126.9226, "쇼핑"),
                ("안양박물관", "경기도 안양시 만안구 예술공원로103번길 4", 37.4196, 126.9187, "문화"),
                ("자유공원", "경기도 안양시 동안구 평촌대로 76", 37.3844, 126.9598, "공원"),
                ("관악산산림욕장", "경기도 안양시 만안구 석수동", 37.4344, 126.9344, "자연"),
            ],
        },
        "화성": {
            "hub": ("동탄역", "경기도 화성시 동탄역로 151", 37.2005, 127.0951),
            "places": [
                ("제부도", "경기도 화성시 서신면 제부리", 37.1709, 126.6210, "해변"),
                ("궁평항", "경기도 화성시 서신면 궁평항로 1049-24", 37.1167, 126.6812, "맛집"),
                ("전곡항", "경기도 화성시 서신면 전곡항로 5", 37.1863, 126.6526, "관광지"),
                ("융건릉", "경기도 화성시 효행로481번길 21", 37.2125, 126.9881, "문화"),
                ("동탄호수공원", "경기도 화성시 동탄순환대로 69", 37.1654, 127.1069, "공원"),
                ("화성시우리꽃식물원", "경기도 화성시 팔탄면 3.1만세로 777-17", 37.1525, 126.9046, "자연"),
                ("우음도", "경기도 화성시 송산면 고정리", 37.2520, 126.7356, "자연"),
                ("공룡알화석산지", "경기도 화성시 송산면 공룡로 659", 37.2531, 126.7476, "문화"),
                ("매향리평화생태공원", "경기도 화성시 우정읍 매향리", 37.0374, 126.7661, "공원"),
                ("서해랑 제부도해상케이블카", "경기도 화성시 서신면 전곡항로 1-10", 37.1854, 126.6528, "전망"),
                ("화성시역사박물관", "경기도 화성시 향남읍 행정동로 96", 37.1316, 126.9206, "문화"),
            ],
        },
        "제천": {
            "hub": ("제천역", "충청북도 제천시 의림대로 1", 37.1288, 128.2057),
            "places": [
                ("청풍호반케이블카", "충청북도 제천시 청풍면 문화재길 166", 37.0002, 128.1668, "전망"),
                ("의림지", "충청북도 제천시 의림지로 33", 37.1745, 128.2103, "자연"),
                ("청풍문화재단지", "충청북도 제천시 청풍면 청풍호로 2048", 37.0009, 128.1680, "문화"),
                ("배론성지", "충청북도 제천시 봉양읍 배론성지길 296", 37.1318, 128.0493, "문화"),
                ("옥순봉출렁다리", "충청북도 제천시 수산면 옥순봉로 342", 36.9337, 128.2091, "전망"),
                ("제천중앙시장", "충청북도 제천시 풍양로 108", 37.1372, 128.2122, "시장"),
                ("제천한방엑스포공원", "충청북도 제천시 한방엑스포로 19", 37.1599, 128.2014, "공원"),
                ("월악산국립공원", "충청북도 제천시 한수면 미륵송계로", 36.8756, 128.1060, "자연"),
                ("박달재", "충청북도 제천시 백운면 평동리", 37.1320, 128.0272, "전망"),
                ("비봉산", "충청북도 제천시 청풍면", 37.0094, 128.1704, "자연"),
                ("덕동계곡", "충청북도 제천시 백운면 덕동리", 37.1510, 128.0067, "자연"),
            ],
        },
        "양산": {
            "hub": ("물금역", "경상남도 양산시 물금읍 황산로 347", 35.3117, 129.0105),
            "places": [
                ("통도사", "경상남도 양산시 하북면 통도사로 108", 35.4883, 129.0645, "문화"),
                ("황산공원", "경상남도 양산시 물금읍 물금리", 35.3121, 129.0081, "공원"),
                ("양산타워", "경상남도 양산시 동면 강변로 264", 35.3379, 129.0281, "전망"),
                ("내원사", "경상남도 양산시 하북면 내원로 207", 35.4214, 129.0774, "문화"),
                ("법기수원지", "경상남도 양산시 동면 법기로 198-13", 35.3749, 129.1143, "자연"),
                ("홍룡사", "경상남도 양산시 상북면 홍룡로 372", 35.4164, 129.0508, "문화"),
                ("임경대", "경상남도 양산시 원동면 원동로 285", 35.3510, 128.9721, "전망"),
                ("양산시립박물관", "경상남도 양산시 북정로 78", 35.3590, 129.0441, "문화"),
                ("물금벚꽃길", "경상남도 양산시 물금읍 황산로", 35.3153, 129.0126, "자연"),
                ("원동매화마을", "경상남도 양산시 원동면 원동로", 35.3787, 128.9196, "자연"),
                ("에덴밸리리조트", "경상남도 양산시 원동면 어실로 1206", 35.4223, 128.9852, "관광지"),
            ],
        },
    }
)

REGION_PLACE_CATALOG.update(
    {
        "동해": {
            "hub": ("묵호역", "강원특별자치도 동해시 발한동", 37.546149, 129.107664),
            "places": [
                ("도째비골스카이밸리", "강원특별자치도 동해시 묵호진동", 37.555219, 129.119121, "전망"),
                ("묵호등대", "강원특별자치도 동해시 해맞이길 300", 37.5537, 129.1193, "전망"),
                ("묵호항", "강원특별자치도 동해시 묵호진동", 37.5504, 129.1165, "항구"),
                ("논골담길", "강원특별자치도 동해시 논골1길", 37.5534, 129.1167, "문화"),
                ("한섬해변", "강원특별자치도 동해시 천곡동", 37.5161, 129.1240, "해변"),
                ("천곡황금박쥐동굴", "강원특별자치도 동해시 동굴로 50", 37.5207, 129.1130, "자연"),
                ("동해무릉건강숲", "강원특별자치도 동해시 삼화로 455", 37.4664, 129.0311, "자연"),
                ("무릉계곡", "강원특별자치도 동해시 삼화동", 37.4620, 129.0129, "자연"),
                ("추암촛대바위", "강원특별자치도 동해시 추암동", 37.4791, 129.1600, "자연"),
                ("추암해변", "강원특별자치도 동해시 추암동", 37.4778, 129.1594, "해변"),
                ("망상해수욕장", "강원특별자치도 동해시 망상동", 37.5922, 129.0900, "해변"),
            ],
        },
        "묵호": {
            "hub": ("묵호역", "강원특별자치도 동해시 발한동", 37.546149, 129.107664),
            "places": [
                ("도째비골스카이밸리", "강원특별자치도 동해시 묵호진동", 37.555219, 129.119121, "전망"),
                ("묵호등대", "강원특별자치도 동해시 해맞이길 300", 37.5537, 129.1193, "전망"),
                ("묵호항", "강원특별자치도 동해시 묵호진동", 37.5504, 129.1165, "항구"),
                ("논골담길", "강원특별자치도 동해시 논골1길", 37.5534, 129.1167, "문화"),
                ("어달해변", "강원특별자치도 동해시 어달동", 37.5652, 129.1197, "해변"),
                ("대진해수욕장", "강원특별자치도 동해시 대진동", 37.5772, 129.1158, "해변"),
                ("망상해수욕장", "강원특별자치도 동해시 망상동", 37.5922, 129.0900, "해변"),
                ("한섬해변", "강원특별자치도 동해시 천곡동", 37.5161, 129.1240, "해변"),
                ("천곡황금박쥐동굴", "강원특별자치도 동해시 동굴로 50", 37.5207, 129.1130, "자연"),
                ("추암촛대바위", "강원특별자치도 동해시 추암동", 37.4791, 129.1600, "자연"),
                ("추암해변", "강원특별자치도 동해시 추암동", 37.4778, 129.1594, "해변"),
            ],
        },
        "서귀포": {
            "hub": ("서귀포버스터미널", "제주특별자치도 서귀포시 일주동로 9217", 33.2555, 126.5090),
            "places": [
                ("천지연폭포", "제주특별자치도 서귀포시 천지동 667-7", 33.2469, 126.5544, "자연"),
                ("정방폭포", "제주특별자치도 서귀포시 칠십리로214번길 37", 33.2448, 126.5724, "자연"),
                ("서귀포매일올레시장", "제주특별자치도 서귀포시 중앙로62번길 18", 33.2501, 126.5631, "맛집"),
                ("이중섭거리", "제주특별자치도 서귀포시 이중섭로", 33.2459, 126.5644, "문화"),
                ("새연교", "제주특별자치도 서귀포시 서홍동", 33.2386, 126.5581, "전망"),
                ("외돌개", "제주특별자치도 서귀포시 서홍동 791", 33.2400, 126.5452, "자연"),
                ("쇠소깍", "제주특별자치도 서귀포시 쇠소깍로 104", 33.2520, 126.6222, "자연"),
                ("주상절리대", "제주특별자치도 서귀포시 이어도로 36-30", 33.2378, 126.4251, "자연"),
                ("산방산", "제주특별자치도 서귀포시 안덕면 사계리", 33.2410, 126.3135, "자연"),
                ("카멜리아힐", "제주특별자치도 서귀포시 안덕면 병악로 166", 33.2897, 126.3707, "자연"),
                ("서귀포치유의숲", "제주특별자치도 서귀포시 산록남로 2271", 33.2915, 126.5326, "자연"),
            ],
        },
        "천안": {
            "hub": ("천안아산역", "충청남도 아산시 배방읍 희망로 100", 36.7948, 127.1045),
            "places": [
                ("독립기념관", "충청남도 천안시 동남구 목천읍 독립기념관로 1", 36.7825, 127.2248, "문화"),
                ("천안삼거리공원", "충청남도 천안시 동남구 삼룡동", 36.7786, 127.1725, "공원"),
                ("아라리오갤러리", "충청남도 천안시 동남구 만남로 43", 36.8195, 127.1575, "문화"),
                ("병천순대거리", "충청남도 천안시 동남구 병천면", 36.7614, 127.2992, "맛집"),
                ("각원사", "충청남도 천안시 동남구 각원사길 245", 36.8329, 127.2063, "문화"),
                ("태조산공원", "충청남도 천안시 동남구 유량동", 36.8238, 127.2043, "자연"),
                ("유관순열사기념관", "충청남도 천안시 동남구 병천면 유관순길 38", 36.7597, 127.3037, "문화"),
                ("천안중앙시장", "충청남도 천안시 동남구 사직로 7", 36.8065, 127.1524, "시장"),
                ("천호지", "충청남도 천안시 동남구 안서동", 36.8330, 127.1710, "자연"),
                ("아름다운정원 화수목", "충청남도 천안시 동남구 목천읍 교천지산길 175", 36.8450, 127.1790, "자연"),
                ("소노벨 천안", "충청남도 천안시 동남구 성남면 종합휴양지로 200", 36.7790, 127.2900, "관광지"),
            ],
        },
        "아산": {
            "hub": ("온양온천역", "충청남도 아산시 온천대로 1496", 36.7805, 127.0036),
            "places": [
                ("현충사", "충청남도 아산시 염치읍 현충사길 126", 36.8064, 126.9905, "문화"),
                ("외암민속마을", "충청남도 아산시 송악면 외암민속길 5", 36.7309, 127.0171, "문화"),
                ("신정호", "충청남도 아산시 신정로 616", 36.7690, 126.9846, "자연"),
                ("온양온천시장", "충청남도 아산시 시장길 13", 36.7820, 127.0027, "맛집"),
                ("공세리성당", "충청남도 아산시 인주면 공세리성당길 10", 36.8871, 126.9127, "문화"),
                ("피나클랜드", "충청남도 아산시 영인면 월선길 20-42", 36.8765, 126.9382, "자연"),
                ("아산 지중해마을", "충청남도 아산시 탕정면 탕정면로8번길", 36.8006, 127.0550, "카페"),
                ("곡교천 은행나무길", "충청남도 아산시 염치읍 백암리", 36.7974, 126.9866, "자연"),
                ("아산레일바이크", "충청남도 아산시 도고면 아산만로 199-7", 36.7550, 126.8799, "관광지"),
                ("영인산자연휴양림", "충청남도 아산시 영인면 아산온천로 16-26", 36.8570, 126.9540, "자연"),
                ("아산환경과학공원", "충청남도 아산시 실옥로 220", 36.7868, 126.9891, "공원"),
            ],
        },
        "가평": {
            "hub": ("가평역", "경기도 가평군 가평읍 문화로 13-42", 37.8148, 127.5107),
            "places": [
                ("남이섬", "강원특별자치도 춘천시 남산면 남이섬길 1", 37.7914, 127.5256, "자연"),
                ("자라섬", "경기도 가평군 가평읍 자라섬로 60", 37.8188, 127.5205, "자연"),
                ("아침고요수목원", "경기도 가평군 상면 수목원로 432", 37.7436, 127.3526, "자연"),
                ("쁘띠프랑스", "경기도 가평군 청평면 호반로 1063", 37.7156, 127.4905, "관광지"),
                ("이탈리아마을 피노키오와다빈치", "경기도 가평군 청평면 호반로 1073-56", 37.7145, 127.4909, "관광지"),
                ("청평호", "경기도 가평군 청평면", 37.7190, 127.4425, "자연"),
                ("가평레일파크", "경기도 가평군 가평읍 장터길 14", 37.8293, 127.5147, "관광지"),
                ("연인산도립공원", "경기도 가평군 가평읍 승안리", 37.8984, 127.4503, "자연"),
                ("유명산자연휴양림", "경기도 가평군 설악면 유명산길 79-53", 37.5755, 127.4892, "자연"),
                ("용추계곡", "경기도 가평군 가평읍 승안리", 37.8678, 127.4778, "자연"),
                ("가평잣고을시장", "경기도 가평군 가평읍 장터2길 10", 37.8301, 127.5163, "시장"),
            ],
        },
        "진주": {
            "hub": ("진주역", "경상남도 진주시 진주역로 130", 35.1502, 128.1187),
            "places": [
                ("진주성", "경상남도 진주시 남강로 626", 35.1896, 128.0806, "문화"),
                ("촉석루", "경상남도 진주시 남강로 626", 35.1888, 128.0809, "문화"),
                ("진주중앙시장", "경상남도 진주시 진양호로547번길 8-1", 35.1937, 128.0838, "맛집"),
                ("국립진주박물관", "경상남도 진주시 남강로 626-35", 35.1880, 128.0794, "문화"),
                ("진양호공원", "경상남도 진주시 판문동", 35.1693, 128.0348, "자연"),
                ("경상남도수목원", "경상남도 진주시 이반성면 수목원로 386", 35.1638, 128.2927, "자연"),
                ("진주남강유등전시관", "경상남도 진주시 망경동", 35.1879, 128.0876, "문화"),
                ("진주논개시장", "경상남도 진주시 장대동", 35.1940, 128.0872, "시장"),
                ("월아산 숲속의 진주", "경상남도 진주시 진성면 달음산로 313", 35.1982, 128.2241, "자연"),
                ("진주익룡발자국전시관", "경상남도 진주시 영천강로68번길 22", 35.1695, 128.1443, "문화"),
                ("진주레일바이크", "경상남도 진주시 내동면 망경로 13", 35.1758, 128.0806, "관광지"),
            ],
        },
        "김해": {
            "hub": ("김해여객터미널", "경상남도 김해시 김해대로 2232", 35.2280, 128.8716),
            "places": [
                ("김해가야테마파크", "경상남도 김해시 가야테마길 161", 35.2497, 128.8720, "관광지"),
                ("수로왕릉", "경상남도 김해시 가락로93번길 26", 35.2341, 128.8815, "문화"),
                ("국립김해박물관", "경상남도 김해시 가야의길 190", 35.2379, 128.8731, "문화"),
                ("봉하마을", "경상남도 김해시 진영읍 봉하로 103-1", 35.3144, 128.7096, "문화"),
                ("연지공원", "경상남도 김해시 금관대로1368번길 7", 35.2389, 128.8668, "공원"),
                ("김해롯데워터파크", "경상남도 김해시 장유로 555", 35.1856, 128.8296, "관광지"),
                ("클레이아크김해미술관", "경상남도 김해시 진례면 진례로 275-51", 35.2482, 128.7484, "문화"),
                ("김해낙동강레일파크", "경상남도 김해시 생림면 마사로473번길 41", 35.3761, 128.8463, "관광지"),
                ("가야의거리", "경상남도 김해시 가야의길", 35.2373, 128.8760, "문화"),
                ("분성산", "경상남도 김해시 어방동", 35.2485, 128.8907, "자연"),
                ("김해천문대", "경상남도 김해시 가야테마길 254", 35.2538, 128.8764, "전망"),
            ],
        },
        "거제": {
            "hub": ("고현버스터미널", "경상남도 거제시 고현천로 10", 34.8885, 128.6248),
            "places": [
                ("바람의언덕", "경상남도 거제시 남부면 갈곶리 산14-47", 34.7447, 128.6637, "전망"),
                ("외도 보타니아", "경상남도 거제시 일운면 외도길 17", 34.7693, 128.7127, "자연"),
                ("거제 해금강", "경상남도 거제시 남부면 갈곶리", 34.7335, 128.6919, "자연"),
                ("학동흑진주몽돌해변", "경상남도 거제시 동부면 학동리", 34.7715, 128.6416, "해변"),
                ("매미성", "경상남도 거제시 장목면 복항길", 34.9952, 128.6935, "관광지"),
                ("거제포로수용소유적공원", "경상남도 거제시 계룡로 61", 34.8801, 128.6201, "문화"),
                ("구조라해수욕장", "경상남도 거제시 일운면 구조라리", 34.8082, 128.6908, "해변"),
                ("신선대", "경상남도 거제시 남부면 갈곶리", 34.7380, 128.6602, "전망"),
                ("지세포항", "경상남도 거제시 일운면 지세포리", 34.8365, 128.7043, "맛집"),
                ("옥포대첩기념공원", "경상남도 거제시 팔랑포2길 87", 34.8952, 128.6961, "문화"),
                ("거제식물원", "경상남도 거제시 거제면 거제남서로 3595", 34.8535, 128.5928, "자연"),
            ],
        },
        "고양": {
            "hub": ("일산역", "경기도 고양시 일산서구 경의로 672", 37.6821, 126.7697),
            "places": [
                ("일산호수공원", "경기도 고양시 일산동구 호수로 595", 37.6546, 126.7680, "공원"),
                ("킨텍스", "경기도 고양시 일산서구 킨텍스로 217-60", 37.6689, 126.7450, "문화"),
                ("스타필드 고양", "경기도 고양시 덕양구 고양대로 1955", 37.6469, 126.8948, "쇼핑"),
                ("행주산성", "경기도 고양시 덕양구 행주로15번길 89", 37.6008, 126.8265, "문화"),
                ("고양아람누리", "경기도 고양시 일산동구 중앙로 1286", 37.6619, 126.7729, "문화"),
                ("라페스타", "경기도 고양시 일산동구 무궁화로 32-21", 37.6604, 126.7712, "맛집"),
                ("웨스턴돔", "경기도 고양시 일산동구 정발산로 24", 37.6545, 126.7732, "쇼핑"),
                ("현대모터스튜디오 고양", "경기도 고양시 일산서구 킨텍스로 217-6", 37.6662, 126.7480, "문화"),
                ("서오릉", "경기도 고양시 덕양구 서오릉로 334-32", 37.6254, 126.9003, "문화"),
                ("원마운트", "경기도 고양시 일산서구 한류월드로 300", 37.6648, 126.7550, "관광지"),
                ("고양어린이박물관", "경기도 고양시 덕양구 화중로 26", 37.6359, 126.8327, "문화"),
            ],
        },
        "용인": {
            "hub": ("기흥역", "경기도 용인시 기흥구 중부대로 460", 37.2754, 127.1159),
            "places": [
                ("에버랜드", "경기도 용인시 처인구 포곡읍 에버랜드로 199", 37.2941, 127.2026, "관광지"),
                ("한국민속촌", "경기도 용인시 기흥구 민속촌로 90", 37.2597, 127.1215, "문화"),
                ("백남준아트센터", "경기도 용인시 기흥구 백남준로 10", 37.2691, 127.1098, "문화"),
                ("보정동카페거리", "경기도 용인시 기흥구 죽전로15번길", 37.3209, 127.1103, "카페"),
                ("기흥호수공원", "경기도 용인시 기흥구 하갈로 79", 37.2353, 127.1078, "자연"),
                ("호암미술관", "경기도 용인시 처인구 포곡읍 에버랜드로562번길 38", 37.2936, 127.1915, "문화"),
                ("와우정사", "경기도 용인시 처인구 해곡로 25-15", 37.1831, 127.2610, "문화"),
                ("용인중앙시장", "경기도 용인시 처인구 금령로99번길 14", 37.2355, 127.2067, "시장"),
                ("용인대장금파크", "경기도 용인시 처인구 백암면 용천드라마길 25", 37.1145, 127.3378, "관광지"),
                ("경기도박물관", "경기도 용인시 기흥구 상갈로 6", 37.2679, 127.1080, "문화"),
                ("용인자연휴양림", "경기도 용인시 처인구 모현읍 초부로 220", 37.3181, 127.2460, "자연"),
            ],
        },
    }
)

for catalog_region, catalog in REGION_PLACE_CATALOG.items():
    FALLBACK_TEMPLATES.setdefault(
        catalog_region,
        build_catalog_template(catalog_region, catalog["hub"], catalog["places"]),
    )

REGION_CENTERS: dict[str, tuple[float, float]] = {
    "국내": (36.5000, 127.8000),
    "서울": (37.5665, 126.9780),
    "부산": (35.1796, 129.0756),
    "제주": (33.4996, 126.5312),
    "서귀포": (33.2530, 126.5600),
    "강릉": (37.7519, 128.8761),
    "창원": (35.2285, 128.6811),
    "청주": (36.6424, 127.4890),
    "대구": (35.8714, 128.6014),
    "대전": (36.3504, 127.3845),
    "광주": (35.1595, 126.8526),
    "울산": (35.5384, 129.3114),
    "수원": (37.2636, 127.0286),
    "여수": (34.7604, 127.6622),
    "전주": (35.8242, 127.1480),
    "경주": (35.8562, 129.2247),
    "인천": (37.4563, 126.7052),
    "속초": (38.2070, 128.5918),
    "춘천": (37.8813, 127.7298),
    "포항": (36.0190, 129.3435),
    "목포": (34.8118, 126.3922),
    "통영": (34.8544, 128.4332),
    "진주": (35.1800, 128.1076),
    "김해": (35.2285, 128.8894),
    "양산": (35.3350, 129.0370),
    "거제": (34.8806, 128.6210),
    "순천": (34.9506, 127.4872),
    "안동": (36.5684, 128.7294),
    "원주": (37.3422, 127.9202),
    "군산": (35.9676, 126.7369),
    "천안": (36.8151, 127.1139),
    "아산": (36.7898, 127.0024),
    "세종": (36.4800, 127.2890),
    "평택": (36.9921, 127.1127),
    "고양": (37.6584, 126.8320),
    "용인": (37.2411, 127.1776),
    "성남": (37.4200, 127.1265),
    "안산": (37.3219, 126.8309),
    "부천": (37.5035, 126.7660),
    "파주": (37.7599, 126.7802),
    "남양주": (37.6360, 127.2165),
    "의정부": (37.7381, 127.0338),
    "가평": (37.8315, 127.5099),
    "안양": (37.3943, 126.9568),
    "화성": (37.1995, 126.8312),
    "제천": (37.1326, 128.1910),
    "태안": (36.7456, 126.2980),
    "안면도": (36.5229, 126.3445),
    "동해": (37.5247, 129.1143),
    "묵호": (37.5504, 129.1165),
}

REGION_ALIASES: dict[str, str] = {
    "대한민국": "국내",
    "국내": "국내",
    "서울특별시": "서울",
    "부산광역시": "부산",
    "제주시": "제주",
    "제주도": "제주",
    "제주특별자치도": "제주",
    "서귀포시": "서귀포",
    "강릉시": "강릉",
    "창원시": "창원",
    "경남 창원": "창원",
    "경상남도 창원": "창원",
    "마산": "창원",
    "진해": "창원",
    "청주시": "청주",
    "대구광역시": "대구",
    "대전광역시": "대전",
    "광주광역시": "광주",
    "울산광역시": "울산",
    "인천광역시": "인천",
    "수원시": "수원",
    "여수시": "여수",
    "전주시": "전주",
    "경주시": "경주",
    "속초시": "속초",
    "춘천시": "춘천",
    "포항시": "포항",
    "목포시": "목포",
    "통영시": "통영",
    "진주시": "진주",
    "김해시": "김해",
    "양산시": "양산",
    "거제시": "거제",
    "순천시": "순천",
    "안동시": "안동",
    "원주시": "원주",
    "군산시": "군산",
    "천안시": "천안",
    "아산시": "아산",
    "세종시": "세종",
    "평택시": "평택",
    "고양시": "고양",
    "용인시": "용인",
    "성남시": "성남",
    "안산시": "안산",
    "부천시": "부천",
    "파주시": "파주",
    "남양주시": "남양주",
    "의정부시": "의정부",
    "가평군": "가평",
    "안양시": "안양",
    "화성시": "화성",
    "제천시": "제천",
    "태안군": "태안",
    "안면도": "안면도",
    "안면읍": "안면도",
    "동해시": "동해",
    "강원 동해": "동해",
    "강원도 동해": "동해",
    "강원특별자치도 동해": "동해",
    "묵호": "묵호",
    "묵호항": "묵호",
    "묵호해변": "묵호",
}

REGION_LODGINGS: dict[str, dict[str, Any]] = {
    "서울": {
        "name": "나인트리 프리미어 호텔 명동2",
        "address": "서울특별시 중구 마른내로 28",
        "lat": 37.5642,
        "lng": 126.9901,
    },
    "부산": {
        "name": "토요코인 부산역1",
        "address": "부산광역시 동구 중앙대로196번길 12",
        "lat": 35.1157,
        "lng": 129.0425,
    },
    "제주": {
        "name": "롯데시티호텔 제주",
        "address": "제주특별자치도 제주시 도령로 83",
        "lat": 33.4906,
        "lng": 126.4865,
    },
    "강릉": {
        "name": "스카이베이 호텔 경포",
        "address": "강원특별자치도 강릉시 해안로 476",
        "lat": 37.8051,
        "lng": 128.9080,
    },
    "동해": {
        "name": "망상오토캠핑리조트",
        "address": "강원특별자치도 동해시 동해대로 6370",
        "lat": 37.600757,
        "lng": 129.078316,
    },
    "묵호": {
        "name": "망상오토캠핑리조트",
        "address": "강원특별자치도 동해시 동해대로 6370",
        "lat": 37.600757,
        "lng": 129.078316,
    },
    "창원": {
        "name": "그랜드 머큐어 앰배서더 창원",
        "address": "경상남도 창원시 의창구 원이대로 332",
        "lat": 35.2352,
        "lng": 128.6536,
    },
    "대구": {
        "name": "토요코인 대구동성로",
        "address": "대구광역시 중구 동성로1길 15",
        "lat": 35.8690,
        "lng": 128.5937,
    },
    "대전": {
        "name": "롯데시티호텔 대전",
        "address": "대전광역시 유성구 엑스포로123번길 33",
        "lat": 36.3755,
        "lng": 127.3921,
    },
    "광주": {
        "name": "홀리데이 인 광주",
        "address": "광주광역시 서구 상무누리로 55",
        "lat": 35.1469,
        "lng": 126.8408,
    },
    "인천": {
        "name": "홀리데이 인 인천 송도",
        "address": "인천광역시 연수구 인천타워대로 251",
        "lat": 37.3927,
        "lng": 126.6444,
    },
    "수원": {
        "name": "노보텔 앰배서더 수원",
        "address": "경기도 수원시 팔달구 덕영대로 902",
        "lat": 37.2671,
        "lng": 126.9997,
    },
    "전주": {
        "name": "라한호텔 전주",
        "address": "전북특별자치도 전주시 완산구 기린대로 85",
        "lat": 35.8159,
        "lng": 127.1533,
    },
    "여수": {
        "name": "소노캄 여수",
        "address": "전라남도 여수시 오동도로 111",
        "lat": 34.7462,
        "lng": 127.7528,
    },
    "경주": {
        "name": "라한셀렉트 경주",
        "address": "경상북도 경주시 보문로 338",
        "lat": 35.8494,
        "lng": 129.2787,
    },
    "울산": {
        "name": "롯데호텔 울산",
        "address": "울산광역시 남구 삼산로 282",
        "lat": 35.5386,
        "lng": 129.3384,
    },
    "청주": {
        "name": "그랜드플라자 청주호텔",
        "address": "충청북도 청주시 청원구 충청대로 114",
        "lat": 36.6659,
        "lng": 127.4895,
    },
    "춘천": {
        "name": "춘천 세종호텔",
        "address": "강원특별자치도 춘천시 봉의산길 31",
        "lat": 37.8844,
        "lng": 127.7317,
    },
    "속초": {
        "name": "롯데리조트 속초",
        "address": "강원특별자치도 속초시 대포항길 186",
        "lat": 38.1818,
        "lng": 128.6091,
    },
    "포항": {
        "name": "라한호텔 포항",
        "address": "경상북도 포항시 북구 삼호로265번길 1",
        "lat": 36.0562,
        "lng": 129.3776,
    },
    "목포": {
        "name": "폰타나비치호텔",
        "address": "전라남도 목포시 평화로 69",
        "lat": 34.7998,
        "lng": 126.4335,
    },
    "통영": {
        "name": "스탠포드호텔앤리조트 통영",
        "address": "경상남도 통영시 도남로 347",
        "lat": 34.8271,
        "lng": 128.4488,
    },
    "순천": {
        "name": "호텔지뜨",
        "address": "전라남도 순천시 팔마2길 11",
        "lat": 34.9324,
        "lng": 127.5187,
    },
    "안동": {
        "name": "안동 그랜드호텔",
        "address": "경상북도 안동시 관광단지로 346-84",
        "lat": 36.5683,
        "lng": 128.7862,
    },
    "원주": {
        "name": "호텔인터불고 원주",
        "address": "강원특별자치도 원주시 동부순환로 200",
        "lat": 37.3369,
        "lng": 127.9778,
    },
    "군산": {
        "name": "라마다 군산호텔",
        "address": "전북특별자치도 군산시 대학로 400",
        "lat": 35.9676,
        "lng": 126.7119,
    },
    "세종": {
        "name": "베스트웨스턴 플러스 호텔 세종",
        "address": "세종특별자치시 도움1로 7",
        "lat": 36.5045,
        "lng": 127.2593,
    },
    "서귀포": {
        "name": "파크선샤인 제주",
        "address": "제주특별자치도 서귀포시 남성중로 135",
        "lat": 33.2448,
        "lng": 126.5553,
    },
    "천안": {
        "name": "신라스테이 천안",
        "address": "충청남도 천안시 서북구 동서대로 177",
        "lat": 36.8210,
        "lng": 127.1546,
    },
    "아산": {
        "name": "온양관광호텔",
        "address": "충청남도 아산시 온천대로 1459",
        "lat": 36.7815,
        "lng": 127.0021,
    },
    "가평": {
        "name": "켄싱턴리조트 가평",
        "address": "경기도 가평군 상면 청군로 430",
        "lat": 37.7716,
        "lng": 127.3626,
    },
    "진주": {
        "name": "골든튤립호텔 남강",
        "address": "경상남도 진주시 남강로673번길 16",
        "lat": 35.1905,
        "lng": 128.0890,
    },
    "김해": {
        "name": "아이스퀘어호텔",
        "address": "경상남도 김해시 김해대로 2360",
        "lat": 35.2294,
        "lng": 128.8725,
    },
    "거제": {
        "name": "삼성호텔 거제",
        "address": "경상남도 거제시 장평3로 80-37",
        "lat": 34.8894,
        "lng": 128.6074,
    },
    "고양": {
        "name": "소노캄 고양",
        "address": "경기도 고양시 일산동구 태극로 20",
        "lat": 37.6626,
        "lng": 126.7508,
    },
    "용인": {
        "name": "라마다 용인호텔",
        "address": "경기도 용인시 처인구 포곡읍 마성로 420",
        "lat": 37.2860,
        "lng": 127.2197,
    },
    "평택": {
        "name": "라마다 앙코르 바이 윈덤 평택",
        "address": "경기도 평택시 포승읍 평택항로184번길 3-10",
        "lat": 36.9707,
        "lng": 126.8461,
    },
    "성남": {
        "name": "그래비티 서울 판교",
        "address": "경기도 성남시 분당구 판교역로146번길 2",
        "lat": 37.3940,
        "lng": 127.1109,
    },
    "안산": {
        "name": "호텔스퀘어 안산",
        "address": "경기도 안산시 단원구 동산로 81",
        "lat": 37.3092,
        "lng": 126.7988,
    },
    "부천": {
        "name": "고려호텔",
        "address": "경기도 부천시 길주로 66",
        "lat": 37.5031,
        "lng": 126.7562,
    },
    "파주": {
        "name": "파주 칼튼호텔",
        "address": "경기도 파주시 탄현면 성동로 34",
        "lat": 37.7819,
        "lng": 126.6878,
    },
    "남양주": {
        "name": "호텔 더 메이",
        "address": "경기도 남양주시 별내2로 70",
        "lat": 37.6462,
        "lng": 127.1235,
    },
    "의정부": {
        "name": "베스트웨스턴 플러스 아일랜드캐슬",
        "address": "경기도 의정부시 장곡로 22",
        "lat": 37.7260,
        "lng": 127.0537,
    },
    "안양": {
        "name": "어반부티크호텔",
        "address": "경기도 안양시 동안구 흥안대로 497",
        "lat": 37.3992,
        "lng": 126.9768,
    },
    "화성": {
        "name": "신라스테이 동탄",
        "address": "경기도 화성시 노작로 161",
        "lat": 37.2044,
        "lng": 127.0731,
    },
    "제천": {
        "name": "청풍리조트",
        "address": "충청북도 제천시 청풍면 청풍호로 1798",
        "lat": 37.0054,
        "lng": 128.1712,
    },
    "양산": {
        "name": "베니키아 양산호텔",
        "address": "경상남도 양산시 물금읍 금오10길 51",
        "lat": 35.3118,
        "lng": 129.0094,
    },
}


def canonical_region_key(region: str) -> str:
    text = str(region or "").strip()
    if not text:
        return "국내"

    compact = re.sub(r"\s+", "", text)
    for alias, key in REGION_ALIASES.items():
        alias_compact = re.sub(r"\s+", "", alias)
        if alias in text or alias_compact in compact:
            return key

    for key in REGION_CENTERS:
        if key in text or key in compact or compact in key:
            return key

    return text


def display_region_name(region: str) -> str:
    original = str(region or "").strip()
    canonical = canonical_region_key(original)
    if canonical in REGION_CENTERS and canonical != "국내":
        return canonical
    return original or canonical


def dynamic_region_center(region: str) -> tuple[float, float]:
    region_name = display_region_name(region)
    cache_key = normalized_text_key(region_name)
    if cache_key in OSM_REGION_CENTER_CACHE:
        return OSM_REGION_CENTER_CACHE[cache_key]

    canonical = canonical_region_key(region)
    if canonical in REGION_CENTERS and canonical != "국내":
        return REGION_CENTERS[canonical]

    geo = geocode_place(region_name, "", "")
    if geo:
        center = (float(geo["lat"]), float(geo["lng"]))
        OSM_REGION_CENTER_CACHE[cache_key] = center
        return center

    return REGION_CENTERS["국내"]


def osm_element_coordinate(element: dict[str, Any]) -> tuple[float, float] | None:
    lat = to_float(element.get("lat"))
    lng = to_float(element.get("lon"))
    if lat is None or lng is None:
        center = element.get("center") if isinstance(element.get("center"), dict) else {}
        lat = to_float(center.get("lat"))
        lng = to_float(center.get("lon"))
    if lat is None or lng is None or not is_korea_coordinate(lat, lng):
        return None
    return round(lat, 6), round(lng, 6)


def osm_category(tags: dict[str, Any]) -> tuple[str, int]:
    tourism = str(tags.get("tourism", ""))
    historic = str(tags.get("historic", ""))
    leisure = str(tags.get("leisure", ""))
    amenity = str(tags.get("amenity", ""))
    natural = str(tags.get("natural", ""))

    if tourism == "viewpoint":
        return "전망", 42
    if tourism in {"museum", "gallery"}:
        return "문화", 40
    if tourism in {"theme_park", "zoo", "aquarium"}:
        return "관광지", 39
    if tourism == "attraction":
        return "관광지", 38
    if historic:
        return "문화", 36
    if amenity == "marketplace":
        return "시장", 34
    if amenity == "arts_centre":
        return "문화", 32
    if leisure in {"park", "garden", "nature_reserve"}:
        return "자연", 30
    if natural == "beach":
        return "해변", 30
    if natural in {"peak", "water", "wood"}:
        return "자연", 28
    return "관광지", 20


def should_skip_osm_place(name: str, tags: dict[str, Any]) -> bool:
    name_key = normalized_text_key(name)
    blocked = (
        "주차장",
        "화장실",
        "관리사무소",
        "매표소",
        "안내소",
        "정류장",
        "어린이집",
        "아파트",
        "편의점",
        "주유소",
        "약국",
        "은행",
        "우체국",
    )
    if not name_key or any(normalized_text_key(word) in name_key for word in blocked):
        return True

    tags_text = " ".join(str(value) for value in tags.values())
    if "private" in tags_text.lower():
        return True
    return False


def osm_address(tags: dict[str, Any], fallback_region: str) -> str:
    full = tags.get("addr:full")
    if full:
        return str(full)
    parts = [
        tags.get("addr:province"),
        tags.get("addr:city") or tags.get("addr:county"),
        tags.get("addr:district"),
        tags.get("addr:street"),
        tags.get("addr:housenumber"),
    ]
    text = " ".join(str(part) for part in parts if part)
    return text or fallback_region


def overpass_query(query: str) -> list[dict[str, Any]]:
    try:
        response = requests.post(
            OVERPASS_API_URL,
            data={"data": query},
            headers=HTTP_HEADERS,
            timeout=OVERPASS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    elements = data.get("elements")
    return elements if isinstance(elements, list) else []


def discover_osm_places(region: str, limit: int = 12) -> list[dict[str, Any]]:
    if not ENABLE_OSM_PLACES:
        return []

    region_name = display_region_name(region)
    cache_key = f"{normalized_text_key(region_name)}::{limit}"
    if cache_key in OSM_PLACE_CACHE:
        return [dict(place) for place in OSM_PLACE_CACHE[cache_key]]

    center_lat, center_lng = dynamic_region_center(region)
    selected: dict[str, tuple[float, dict[str, Any]]] = {}
    canonical = canonical_region_key(region)
    if canonical in {"안면도", "대부도", "제부도"}:
        radiuses = [12000]
    elif canonical in {"강화도"}:
        radiuses = [22000]
    else:
        radiuses = [18000]
    for radius in radiuses:
        query = f"""
[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];
(
  nwr(around:{radius},{center_lat},{center_lng})["name"]["tourism"~"attraction|museum|gallery|viewpoint|theme_park|zoo|aquarium"];
  nwr(around:{radius},{center_lat},{center_lng})["name"]["historic"];
  nwr(around:{radius},{center_lat},{center_lng})["name"]["leisure"~"park|garden|nature_reserve"];
  nwr(around:{radius},{center_lat},{center_lng})["name"]["amenity"~"marketplace|arts_centre"];
  nwr(around:{radius},{center_lat},{center_lng})["name"]["natural"~"beach|peak|water|wood"];
);
out center tags 50;
"""
        for element in overpass_query(query):
            tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
            name = str(tags.get("name:ko") or tags.get("name") or "").strip()
            coordinate = osm_element_coordinate(element)
            if not name or not coordinate or should_skip_osm_place(name, tags):
                continue

            category, base_score = osm_category(tags)
            distance = geo_distance_km(center_lat, center_lng, coordinate[0], coordinate[1])
            if distance > radius / 1000 + 2:
                continue

            key = normalized_text_key(name)
            score = base_score - distance * 0.15
            if key in selected and selected[key][0] >= score:
                continue

            selected[key] = (
                score,
                {
                    "day": 1,
                    "order": 1,
                    "time": "",
                    "place": name,
                    "address": osm_address(tags, region_name),
                    "lat": coordinate[0],
                    "lng": coordinate[1],
                    "category": category,
                    "summary": f"{region_name} 지도 데이터에서 찾은 {category} 장소",
                    "tip": "운영 시간과 휴무일은 방문 전 확인하세요.",
                    "location_source": "OpenStreetMap 장소 데이터",
                },
            )
        if len(selected) >= limit:
            break

    places: list[dict[str, Any]] = []
    for _, place in sorted(selected.values(), key=lambda item: -item[0]):
        name_key = route_point_key(place)
        is_duplicate = False
        for existing in places:
            existing_key = route_point_key(existing)
            same_spot = geo_distance_km(
                float(existing["lat"]),
                float(existing["lng"]),
                float(place["lat"]),
                float(place["lng"]),
            ) < 0.12
            similar_name = bool(name_key and existing_key and (name_key in existing_key or existing_key in name_key))
            if same_spot or similar_name:
                is_duplicate = True
                break
        if is_duplicate:
            continue
        places.append(place)
        if len(places) >= limit:
            break

    OSM_PLACE_CACHE[cache_key] = [dict(place) for place in places]
    return places


def discover_osm_transport_hub(region: str) -> dict[str, Any] | None:
    if not ENABLE_OSM_PLACES or not ENABLE_OSM_HUB:
        return None

    region_name = display_region_name(region)
    center_lat, center_lng = dynamic_region_center(region)
    query = f"""
[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];
(
  nwr(around:22000,{center_lat},{center_lng})["name"]["railway"="station"];
  nwr(around:22000,{center_lat},{center_lng})["name"]["amenity"="bus_station"];
  nwr(around:22000,{center_lat},{center_lng})["name"]["public_transport"~"station|stop_area"];
);
out center tags 40;
"""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for element in overpass_query(query):
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        name = str(tags.get("name:ko") or tags.get("name") or "").strip()
        coordinate = osm_element_coordinate(element)
        if not name or not coordinate:
            continue
        name_key = normalized_text_key(name)
        if any(blocked in name_key for blocked in ("정류장", "승강장", "입구")):
            continue

        score = 20.0
        if "역" in name or "터미널" in name:
            score += 15
        if normalized_text_key(region_name) in name_key:
            score += 8
        score -= geo_distance_km(center_lat, center_lng, coordinate[0], coordinate[1]) * 0.1
        candidates.append(
            (
                score,
                {
                    "day": 1,
                    "order": 1,
                    "time": "10:00",
                    "place": name,
                    "address": osm_address(tags, region_name),
                    "lat": coordinate[0],
                    "lng": coordinate[1],
                    "category": "이동",
                    "summary": "지도 데이터로 찾은 여행 시작 교통 거점",
                    "tip": "실제 도착 교통편에 맞춰 시간을 조정하세요.",
                    "location_source": "OpenStreetMap 교통 데이터",
                },
            )
        )

    if not candidates:
        for name in (
            f"{region_name}종합버스터미널",
            f"{region_name}시외버스터미널",
            f"{region_name}고속버스터미널",
            f"{region_name}버스터미널",
            f"{region_name}역",
        ):
            geo = geocode_place(name, region_name, "")
            if not geo:
                continue
            if geo_distance_km(center_lat, center_lng, float(geo["lat"]), float(geo["lng"])) > 80:
                continue
            actual_name = str(geo.get("name") or name).strip()
            return {
                "day": 1,
                "order": 1,
                "time": "10:00",
                "place": actual_name,
                "address": geo.get("address") or region_name,
                "lat": geo["lat"],
                "lng": geo["lng"],
                "category": "이동",
                "summary": "지도 검색으로 찾은 여행 시작 교통 거점",
                "tip": "실제 도착 교통편에 맞춰 시간을 조정하세요.",
                "location_source": geo.get("source") or "OpenStreetMap 교통 검색",
            }
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def discover_osm_lodging(region: str) -> dict[str, Any] | None:
    if not ENABLE_OSM_PLACES or not ENABLE_OSM_LODGING:
        return None

    region_name = display_region_name(region)
    cache_key = normalized_text_key(region_name)
    if cache_key in OSM_LODGING_CACHE:
        return dict(OSM_LODGING_CACHE[cache_key])

    center_lat, center_lng = dynamic_region_center(region_name)
    canonical = canonical_region_key(region_name)
    if canonical in {"안면도", "대부도", "제부도"}:
        radiuses = [14000, 22000]
    else:
        radiuses = [14000, 32000, 50000]
    candidates: list[tuple[float, dict[str, Any]]] = []

    for radius in radiuses:
        query = f"""
[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];
(
  nwr(around:{radius},{center_lat},{center_lng})["name"]["tourism"~"hotel|motel|guest_house|hostel|apartment|camp_site"];
);
out center tags 25;
"""
        for element in overpass_query(query):
            tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
            name = str(tags.get("name:ko") or tags.get("name") or "").strip()
            coordinate = osm_element_coordinate(element)
            if not name or not coordinate or should_skip_osm_place(name, tags):
                continue
            distance = geo_distance_km(center_lat, center_lng, coordinate[0], coordinate[1])
            if distance > MAX_ROUTE_LEG_KM:
                continue
            score = 30 - distance * 0.25
            if "호텔" in name or "리조트" in name:
                score += 8
            candidates.append(
                (
                    score,
                    {
                        "name": name,
                        "address": osm_address(tags, region_name),
                        "lat": coordinate[0],
                        "lng": coordinate[1],
                        "location_source": "OpenStreetMap 숙소 데이터",
                    },
                )
            )
        if candidates:
            break

    if not candidates:
        return None

    candidates.sort(key=lambda item: -item[0])
    lodging = candidates[0][1]
    OSM_LODGING_CACHE[cache_key] = dict(lodging)
    return lodging


def geocode_real_lodging(region: str) -> dict[str, Any] | None:
    region_name = display_region_name(region)
    for keyword in ("호텔", "모텔", "리조트", "게스트하우스", "펜션"):
        query_name = f"{region_name} {keyword}"
        geo = geocode_place(query_name, region_name, "")
        if not verified_lodging_geocode(geo, query_name, region_name):
            continue
        result_name = str(geo.get("name") or "").strip()
        if not result_name:
            result_name = str(geo.get("address") or "").split(",")[0].strip()
        if not result_name:
            continue
        return {
            "name": result_name,
            "address": geo.get("address") or region_name,
            "lat": geo["lat"],
            "lng": geo["lng"],
            "location_source": "OpenStreetMap 숙소 검색",
        }
    return None


def discover_osm_template_for_region(region: str) -> list[dict[str, Any]]:
    region_name = display_region_name(region)
    cache_key = normalized_text_key(region_name)
    if cache_key in OSM_TEMPLATE_CACHE:
        return [dict(point) for point in OSM_TEMPLATE_CACHE[cache_key]]

    places = discover_osm_places(region_name, 8)
    if not places:
        return []

    hub = discover_osm_transport_hub(region_name)
    if not hub:
        center_lat, center_lng = dynamic_region_center(region_name)
        region_geo = geocode_place(region_name, "", "")
        actual_name = str(region_geo.get("name") or region_name).strip()
        if region_geo:
            center_lat = float(region_geo["lat"])
            center_lng = float(region_geo["lng"])
        hub = {
            "day": 1,
            "order": 1,
            "time": "10:00",
            "place": actual_name,
            "address": region_geo.get("address") or region_name,
            "lat": center_lat,
            "lng": center_lng,
            "category": "이동",
            "summary": "지도 데이터로 확인한 여행 시작 지점",
            "tip": "실제 터미널/역에 맞춰 조정하세요.",
            "location_source": "OpenStreetMap 지역 중심",
        }

    template = [hub]
    for index, place in enumerate(places):
        item = dict(place)
        item["day"] = index // 3 + 1
        item["order"] = index % 3 + 2
        item["time"] = ["11:00", "13:00", "15:30"][index % 3]
        template.append(item)

    OSM_TEMPLATE_CACHE[cache_key] = [dict(point) for point in template]
    return template


def trip_day_count(payload: dict[str, Any]) -> int:
    direct_days = to_int(payload.get("trip_days"), 0)
    if direct_days:
        return max(1, min(10, direct_days))

    start_text = str(payload.get("start_date", "")).strip()
    end_text = str(payload.get("end_date", "")).strip()
    if start_text and end_text:
        try:
            start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
            if end_date < start_date:
                start_date, end_date = end_date, start_date
            return max(1, min(10, (end_date - start_date).days + 1))
        except ValueError:
            pass

    text = str(payload.get("days", ""))
    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if numbers:
        return max(1, min(10, numbers[-1]))
    return max(1, min(10, to_int(text, 2)))


def region_center(region: str) -> tuple[float, float]:
    return REGION_CENTERS.get(canonical_region_key(region), REGION_CENTERS["국내"])


def lodging_for_region(region: str, fallback_points: list[dict[str, Any]]) -> dict[str, Any]:
    canonical = canonical_region_key(region)
    if canonical in REGION_LODGINGS:
        return dict(REGION_LODGINGS[canonical])

    region_name = display_region_name(region)
    osm_lodging = discover_osm_lodging(region_name)
    if osm_lodging:
        return dict(osm_lodging)

    geocoded_lodging = geocode_real_lodging(region_name)
    if geocoded_lodging:
        return geocoded_lodging

    raise ValueError(f"{region_name} 주변에서 검증 가능한 실제 숙소 데이터를 찾지 못했습니다.")


def fallback_template_for_region(region: str) -> list[dict[str, Any]]:
    canonical = canonical_region_key(region)
    region_name = display_region_name(region)
    if canonical in FALLBACK_TEMPLATES:
        return [dict(point) for point in FALLBACK_TEMPLATES[canonical]]

    for key, points in FALLBACK_TEMPLATES.items():
        if key in region_name or region_name in key:
            return [dict(point) for point in points]

    osm_template = discover_osm_template_for_region(region_name)
    if len(osm_template) > 1:
        return [dict(point) for point in osm_template]

    region_geo = geocode_place(region_name, "", "")
    if region_geo:
        return [
            {
                "day": 1,
                "order": 1,
                "time": "10:00",
                "place": str(region_geo.get("name") or region_name).strip(),
                "address": region_geo.get("address") or region_name,
                "lat": region_geo["lat"],
                "lng": region_geo["lng"],
                "category": "이동",
                "summary": "지도 데이터로 확인한 지역 기준점",
                "tip": "실제 도착 지점에 맞춰 조정하세요.",
                "location_source": "OpenStreetMap 지역 검색",
            }
        ]

    return []


def average_coordinate(points: list[dict[str, Any]], region: str) -> tuple[float, float]:
    center_lat, center_lng = dynamic_region_center(region)
    radius = route_region_radius_km(region)
    valid = [
        (float(point["lat"]), float(point["lng"]))
        for point in points
        if to_float(point.get("lat")) is not None and to_float(point.get("lng")) is not None
        and geo_distance_km(center_lat, center_lng, float(point["lat"]), float(point["lng"])) <= radius
    ]
    if not valid:
        return center_lat, center_lng
    return sum(lat for lat, _ in valid) / len(valid), sum(lng for _, lng in valid) / len(valid)


def separate_lodging_from_route_points(
    lodging: dict[str, Any],
    points: list[dict[str, Any]],
    region: str,
) -> dict[str, Any]:
    adjusted = dict(lodging)
    lat = to_float(adjusted.get("lat"))
    lng = to_float(adjusted.get("lng"))
    if lat is None or lng is None or not is_korea_coordinate(lat, lng):
        center_lat, center_lng = dynamic_region_center(region)
        lat, lng = center_lat - 0.008, center_lng - 0.008

    lodging_ref = {"lat": lat, "lng": lng}
    overlaps = False
    for point in points:
        if is_lodging_place_name(point.get("place")) or str(point.get("category", "")).strip() == "숙소":
            continue
        point_lat = to_float(point.get("lat"))
        point_lng = to_float(point.get("lng"))
        if point_lat is None or point_lng is None:
            continue
        distance = route_point_distance_km(lodging_ref, {"lat": point_lat, "lng": point_lng})
        if distance is not None and distance < 0.18:
            overlaps = True
            break

    source = str(adjusted.get("location_source", ""))
    if overlaps:
        center_lat, center_lng = dynamic_region_center(region)
        base_lat = lat if lat is not None else center_lat
        base_lng = lng if lng is not None else center_lng
        lat = base_lat - 0.008
        lng = base_lng - 0.006
        if not is_korea_coordinate(lat, lng):
            lat, lng = center_lat - 0.008, center_lng - 0.006

    adjusted["lat"] = round(float(lat), 6)
    adjusted["lng"] = round(float(lng), 6)
    return adjusted


def clone_local_point(item: dict[str, Any], day: int, order: int, time: str, index: int) -> dict[str, Any] | None:
    copied = dict(item)
    copied.update({"day": day, "order": order, "time": time})
    point = normalize_point(copied, index)
    if not point:
        return None
    lat = to_float(copied.get("lat"))
    lng = to_float(copied.get("lng"))
    point["lat"] = round(lat, 6) if lat is not None else None
    point["lng"] = round(lng, 6) if lng is not None else None
    point["address"] = point["address"] or str(copied.get("address", "")).strip()
    point["location_source"] = str(copied.get("location_source") or "로컬 일정 좌표")
    return point


def transport_hub_for_region(region: str, template_points: list[dict[str, Any]]) -> dict[str, Any]:
    for point in template_points:
        if str(point.get("category", "")) == "이동":
            return dict(point)
    region_name = display_region_name(region)
    geo = geocode_place(region_name, "", "")
    if geo:
        return {
            "day": 1,
            "order": 1,
            "time": "10:00",
            "place": str(geo.get("name") or region_name).strip(),
            "address": geo.get("address") or region_name,
            "lat": geo["lat"],
            "lng": geo["lng"],
            "category": "이동",
            "summary": "지도 데이터로 확인한 여행 시작 지점",
            "tip": "실제 터미널/역 위치에 맞춰 조정하세요.",
            "location_source": geo.get("source") or "OpenStreetMap 지역 검색",
        }
    lat, lng = region_center(region)
    return {
        "day": 1,
        "order": 1,
        "time": "10:00",
        "place": region_name,
        "address": region_name,
        "lat": lat,
        "lng": lng,
        "category": "이동",
        "summary": "지도 데이터 기준 지역명",
        "tip": "실제 터미널/역 위치에 맞춰 조정하세요.",
    }


def ensure_visit_pool(points: list[dict[str, Any]], required_count: int, region: str) -> list[dict[str, Any]]:
    expanded = [dict(point) for point in points]
    if len(expanded) >= required_count:
        return expanded

    for candidate in discover_osm_places(region, required_count + 6):
        key = route_point_key(candidate)
        if not key or any(route_point_key(point) == key for point in expanded):
            continue
        expanded.append(dict(candidate))
        if len(expanded) >= required_count:
            break

    return expanded


def lodging_point(region: str, lodging: dict[str, Any], day: int, order: int, time: str, suffix: str) -> dict[str, Any]:
    lat, lng = region_center(region)
    lodging_lat = to_float(lodging.get("lat"))
    lodging_lng = to_float(lodging.get("lng"))
    region_name = display_region_name(region)
    name = str(lodging.get("name") or "").strip()
    if not name:
        raise ValueError(f"{region_name} 주변에서 검증 가능한 실제 숙소명을 찾지 못했습니다.")
    address = str(lodging.get("address") or f"{region_name} 중심 숙박권").strip()
    return {
        "id": f"lodging-{day}-{order}-{suffix}",
        "day": day,
        "order": order,
        "time": time,
        "place": f"숙소[{name}]",
        "address": address,
        "lat": round(lodging_lat if lodging_lat is not None else lat, 6),
        "lng": round(lodging_lng if lodging_lng is not None else lng, 6),
        "category": "숙소",
        "summary": "짐 보관과 휴식 기준으로 잡은 숙소",
        "tip": "예약 숙소가 다르면 위치에 맞춰 조정하세요.",
        "location_source": str(lodging.get("location_source") or "로컬 숙소 기준 좌표"),
    }


def create_local_route_points(payload: dict[str, Any]) -> list[dict[str, Any]]:
    region = str(payload.get("region", "국내")).strip() or "국내"
    total_days = trip_day_count(payload)
    template_points = fallback_template_for_region(region)
    hub_point = transport_hub_for_region(region, template_points)
    raw_points = [point for point in template_points if str(point.get("category", "")) != "이동"]
    if not raw_points:
        raw_points = template_points

    required_visits = 3 if total_days == 1 else 3 + max(0, total_days - 2) * 3 + 2
    visit_pool = ensure_visit_pool(raw_points, required_visits, region)
    visit_pool = [point for point in visit_pool if str(point.get("category", "")) != "이동"]
    if not visit_pool:
        raise ValueError(f"{display_region_name(region)} 주변에서 검증 가능한 실제 방문지 데이터를 찾지 못했습니다.")
    lodging = lodging_for_region(region, visit_pool)
    lodging = separate_lodging_from_route_points(lodging, [hub_point, *visit_pool], region)
    points: list[dict[str, Any]] = []
    cursor = 0
    index = 0

    for day in range(1, total_days + 1):
        if total_days == 1 or day == 1:
            arrival = dict(hub_point)
            arrival["summary"] = "여행을 시작하는 도착 거점"
            arrival["tip"] = "짐이 있으면 보관 후 이동하세요."
            point = clone_local_point(arrival, day, 1, "10:00", index)
            index += 1
            if point:
                points.append(point)
        else:
            points.append(lodging_point(region, lodging, day, 1, "09:30", "start"))

        visits_per_day = 3 if day < total_days else 2
        if total_days == 1:
            visits_per_day = 3
        visit_times = ["11:10", "13:40", "16:00"] if day == 1 else ["10:30", "13:00", "15:30"]
        if day == total_days:
            visit_times = ["10:30", "13:20", "15:10"]

        for visit_index in range(visits_per_day):
            item = visit_pool[cursor % len(visit_pool)]
            cursor += 1
            point = clone_local_point(item, day, visit_index + 2, visit_times[visit_index], index)
            index += 1
            if point and point.get("lat") is not None and point.get("lng") is not None:
                points.append(point)

        if total_days == 1 or day == total_days:
            departure = dict(hub_point)
            departure["summary"] = "귀가 전 마지막 교통 거점"
            departure["tip"] = "열차/항공/버스 시간을 기준으로 여유를 두세요."
            point = clone_local_point(departure, day, len(points) + 1, "17:00", index)
            index += 1
            if point:
                points.append(point)
        else:
            points.append(lodging_point(region, lodging, day, len(points) + 1, "19:00", "end"))

        day_points = [point for point in points if point["day"] == day]
        for order, point in enumerate(day_points, start=1):
            point["order"] = order
            point["id"] = f"p-{day}-{order}-{point['place']}"

    return sorted(points, key=lambda value: (value["day"], value["order"]))


def is_repeatable_route_point(point: dict[str, Any]) -> bool:
    category = str(point.get("category", "")).strip()
    place = str(point.get("place", "")).strip()
    return category in {"숙소", "이동"} or place.startswith("숙소[")


def route_point_key(point: dict[str, Any]) -> str:
    return normalized_text_key(point.get("place"))


def region_scope_keywords(region: str) -> set[str]:
    region_name = display_region_name(region)
    canonical = canonical_region_key(region)
    raw_values = {region_name, canonical, str(region or "")}
    known_scopes = {
        "안면도": {"안면도", "안면", "태안", "고남"},
        "태안": {"태안", "안면", "고남", "소원", "남면", "근흥"},
        "대부도": {"대부도", "대부", "안산"},
        "제부도": {"제부도", "제부", "화성"},
        "강화도": {"강화도", "강화", "인천"},
        "거제": {"거제", "거제도"},
        "남해": {"남해"},
    }
    raw_values.update(known_scopes.get(canonical, set()))
    raw_values.update(known_scopes.get(region_name, set()))

    keywords: set[str] = set()
    for value in raw_values:
        compact = normalized_text_key(value)
        if not compact:
            continue
        keywords.add(compact)
        stripped = re.sub(r"(특별자치도|특별자치시|광역시|특별시|자치도|시|군|구|읍|면|동|도)$", "", compact)
        if len(stripped) >= 2:
            keywords.add(stripped)
    return {keyword for keyword in keywords if len(keyword) >= 2 and keyword != "국내"}


def point_text_for_scope(point: dict[str, Any]) -> str:
    return normalized_text_key(
        " ".join(
            str(point.get(key, ""))
            for key in ("place", "address", "summary", "tip", "category")
        )
    )


def point_matches_region_scope(point: dict[str, Any], region: str) -> bool:
    text = point_text_for_scope(point)
    keywords = region_scope_keywords(region)
    return bool(text and keywords and any(keyword in text for keyword in keywords))


def max_region_route_distance_km(region: str) -> float | None:
    canonical = canonical_region_key(region)
    limits = {
        "안면도": 22.0,
        "대부도": 20.0,
        "제부도": 14.0,
        "강화도": 38.0,
    }
    return limits.get(canonical)


def route_leg_limit_km(region: str) -> float:
    region_limit = max_region_route_distance_km(region)
    return min(MAX_ROUTE_LEG_KM, region_limit) if region_limit else MAX_ROUTE_LEG_KM


def route_region_radius_km(region: str) -> float:
    region_limit = max_region_route_distance_km(region)
    return max(12.0, region_limit) if region_limit else 90.0


def route_point_distance_km(start: dict[str, Any], end: dict[str, Any]) -> float | None:
    start_lat = to_float(start.get("lat"))
    start_lng = to_float(start.get("lng"))
    end_lat = to_float(end.get("lat"))
    end_lng = to_float(end.get("lng"))
    if start_lat is None or start_lng is None or end_lat is None or end_lng is None:
        return None
    return geo_distance_km(start_lat, start_lng, end_lat, end_lng)


def point_distance_from_region_km(point: dict[str, Any], region: str) -> float | None:
    lat = to_float(point.get("lat"))
    lng = to_float(point.get("lng"))
    if lat is None or lng is None:
        return None
    center_lat, center_lng = dynamic_region_center(region)
    return geo_distance_km(center_lat, center_lng, lat, lng)


def point_far_out_of_region(point: dict[str, Any], region: str) -> bool:
    distance = point_distance_from_region_km(point, region)
    return distance is not None and distance > route_region_radius_km(region)


def looks_like_offshore_or_boat_place(point: dict[str, Any], region: str) -> bool:
    if is_repeatable_route_point(point):
        return False

    place = str(point.get("place", "")).strip()
    text = point_text_for_scope(point)
    if not text:
        return False

    boat_words = (
        "여객선",
        "선착장",
        "페리",
        "배편",
        "도선",
        "승선",
        "해상택시",
        "유람선",
        "항로",
    )
    if any(word in text for word in boat_words):
        return True

    boat_only_places = (
        "외도",
        "외도보타니아",
        "장사도",
        "남이섬",
        "우도",
        "마라도",
        "가파도",
        "삽시도",
        "고대도",
        "장고도",
        "효자도",
        "원산도",
        "호도",
        "녹도",
        "연도",
        "울릉도",
        "독도",
    )
    region_text = normalized_text_key(region)
    if any(place in text and place not in region_text for place in boat_only_places):
        return True

    canonical = canonical_region_key(region)
    off_scope_keywords = {
        "안면도": ("서산", "보령", "홍성", "원산", "간월", "삽시", "고대", "장고", "효자"),
        "대부도": ("영흥", "선재", "제부", "무의"),
        "제부도": ("대부", "영흥", "선재", "무의"),
    }
    if any(keyword in text for keyword in off_scope_keywords.get(canonical, ())):
        return True

    max_distance = max_region_route_distance_km(region)
    lat = to_float(point.get("lat"))
    lng = to_float(point.get("lng"))
    if max_distance is not None and lat is not None and lng is not None:
        center_lat, center_lng = region_center(region)
        if geo_distance_km(center_lat, center_lng, lat, lng) > max_distance:
            return True

    compact_place = normalized_text_key(place)
    island_name = bool(re.search(r"(섬|도)$", compact_place))
    if island_name and not point_matches_region_scope(point, region):
        return True

    return False


def filter_unreachable_route_points(points: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    region = str(payload.get("region", "국내")).strip() or "국내"
    filtered: list[dict[str, Any]] = []
    for point in points:
        if looks_like_offshore_or_boat_place(point, region):
            continue
        if not is_repeatable_route_point(point) and point_far_out_of_region(point, region):
            continue
        filtered.append(dict(point))
    return filtered


def repair_duplicate_route_points(points: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not points:
        return []

    region = str(payload.get("region", "국내")).strip() or "국내"
    candidates = [
        dict(point)
        for point in fallback_template_for_region(region)
        if str(point.get("category", "")) not in {"이동", "숙소"} and point.get("place")
    ]
    candidates = filter_unreachable_route_points(candidates, payload)
    candidate_index = 0
    used: set[str] = set()
    repaired: list[dict[str, Any]] = []

    for index, point in enumerate(sorted(points, key=lambda value: (value["day"], value["order"]))):
        current = dict(point)
        key = route_point_key(current)
        if key and not is_repeatable_route_point(current) and key in used:
            replacement: dict[str, Any] | None = None
            while candidate_index < len(candidates):
                candidate = candidates[candidate_index]
                candidate_index += 1
                candidate_key = route_point_key(candidate)
                if candidate_key and candidate_key not in used:
                    replacement = candidate
                    break

            if replacement:
                replacement_time = str(current.get("time") or replacement.get("time") or "").strip()
                cloned = clone_local_point(
                    replacement,
                    max(1, to_int(current.get("day"), 1)),
                    max(1, to_int(current.get("order"), 1)),
                    replacement_time,
                    index,
                )
                if cloned:
                    current = cloned
                    current["location_source"] = replacement.get("location_source") or "지도/장소 데이터 중복 보정"
                    key = route_point_key(current)
            else:
                continue

        repaired.append(current)
        if key and not is_repeatable_route_point(current):
            used.add(key)

    grouped = group_points_by_day(repaired)
    normalized: list[dict[str, Any]] = []
    for day, day_points in grouped.items():
        for order, point in enumerate(day_points, start=1):
            item = dict(point)
            item["day"] = day
            item["order"] = order
            item["id"] = f"p-{day}-{order}-{item['place']}"
            normalized.append(item)

    return sorted(normalized, key=lambda value: (value["day"], value["order"]))


def lodging_from_route_points(points: list[dict[str, Any]], region: str) -> dict[str, Any] | None:
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for point in points:
        if not is_repeatable_route_point(point):
            continue
        place = str(point.get("place", "")).strip()
        if not place.startswith("숙소["):
            continue
        match = re.match(r"숙소\[(.+?)\]", place)
        lodging_name = match.group(1).strip() if match else place.replace("숙소", "").strip("[] ")
        lat = to_float(point.get("lat"))
        lng = to_float(point.get("lng"))
        if not lodging_name or lat is None or lng is None:
            continue
        source = str(point.get("location_source") or "")
        if "검증 실패" in source:
            continue
        distance = point_distance_from_region_km(point, region)
        if distance is not None and distance > route_region_radius_km(region):
            continue
        score = 20.0 if point_matches_region_scope(point, region) else 0.0
        if distance is not None:
            score += max(0.0, 20.0 - distance)
        lodging = {
            "name": lodging_name,
            "address": str(point.get("address") or f"{display_region_name(region)} 숙소").strip(),
            "lat": lat,
            "lng": lng,
            "location_source": source or "AI 숙소 좌표 보정",
        }
        candidates.append((score, distance or 9999.0, lodging))
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]
    return None


def remove_trailing_departure_point(day_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(day_points) <= 1:
        return day_points
    last = day_points[-1]
    if str(last.get("category", "")).strip() == "이동" and not str(last.get("place", "")).startswith("숙소["):
        return day_points[:-1]
    return day_points


def normalize_day_orders(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for day, day_points in group_points_by_day(points).items():
        for order, point in enumerate(day_points, start=1):
            item = dict(point)
            item["day"] = day
            item["order"] = order
            item["id"] = f"p-{day}-{order}-{item['place']}"
            normalized.append(item)
    return sorted(normalized, key=lambda value: (value["day"], value["order"]))


def visit_candidates_for_region(region: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        dict(point)
        for point in fallback_template_for_region(region)
        if str(point.get("category", "")) not in {"이동", "숙소"} and point.get("place")
    ]
    required_visits = sum(minimum_visit_count(day, trip_day_count(payload)) for day in range(1, trip_day_count(payload) + 1))
    candidates = ensure_visit_pool(candidates, required_visits + 4, region)
    return filter_unreachable_route_points(candidates, payload)


def pick_repair_visits(
    candidates: list[dict[str, Any]],
    used: set[str],
    count: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    if not candidates:
        return selected

    for candidate in candidates:
        key = route_point_key(candidate)
        if not key or key in used:
            continue
        selected.append(dict(candidate))
        used.add(key)
        if len(selected) >= count:
            return selected

    for candidate in candidates:
        key = route_point_key(candidate)
        if not key:
            continue
        selected.append(dict(candidate))
        if len(selected) >= count:
            break

    return selected


def build_missing_day_points(
    region: str,
    lodging: dict[str, Any],
    day: int,
    total_days: int,
    candidates: list[dict[str, Any]],
    used: set[str],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    index_base = day * 100
    if day == 1:
        hub = transport_hub_for_region(region, fallback_template_for_region(region))
        point = clone_local_point(hub, day, 1, "10:00", index_base)
        if point:
            points.append(point)
    else:
        points.append(lodging_point(region, lodging, day, 1, "09:30", "start"))

    visit_count = 3 if day < total_days else 2
    visit_times = ["11:10", "13:40", "16:00"] if day == 1 else ["10:30", "13:00", "15:30"]
    if day == total_days:
        visit_times = ["10:30", "13:20"]

    for visit_index, candidate in enumerate(pick_repair_visits(candidates, used, visit_count)):
        point = clone_local_point(
            candidate,
            day,
            len(points) + 1,
            visit_times[min(visit_index, len(visit_times) - 1)],
            index_base + visit_index + 1,
        )
        if point:
            points.append(point)

    points.append(lodging_point(region, lodging, day, len(points) + 1, "19:00", "end"))
    return points


def limit_day_route_distance(
    day_points: list[dict[str, Any]],
    region: str,
    start_anchor: dict[str, Any] | None,
    end_anchor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    limit = route_leg_limit_km(region)
    kept: list[dict[str, Any]] = []
    previous = start_anchor

    for point in sorted(day_points, key=lambda item: to_int(item.get("order"), 1)):
        if point_far_out_of_region(point, region):
            continue

        distance = route_point_distance_km(previous, point) if previous else None
        if distance is not None and distance > limit:
            continue

        kept.append(dict(point))
        previous = point

    while kept and end_anchor:
        distance = route_point_distance_km(kept[-1], end_anchor)
        if distance is None or distance <= limit:
            break
        kept.pop()

    return kept


def minimum_visit_count(day: int, total_days: int) -> int:
    if total_days == 1:
        return 3
    return 2 if day == total_days else 3


def fill_day_visits(
    day_points: list[dict[str, Any]],
    region: str,
    lodging: dict[str, Any],
    day: int,
    total_days: int,
    candidates: list[dict[str, Any]],
    used: set[str],
    start_anchor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    needed = minimum_visit_count(day, total_days) - len(
        [point for point in day_points if not is_repeatable_route_point(point)]
    )
    if needed <= 0:
        return day_points

    limit = route_leg_limit_km(region)
    end_anchor = lodging_point(region, lodging, day, 999, "19:00", "end")
    previous = day_points[-1] if day_points else start_anchor
    local_used = {
        route_point_key(point)
        for point in day_points
        if route_point_key(point) and not is_repeatable_route_point(point)
    }
    times = ["10:30", "12:30", "14:30", "16:00"]

    for candidate in candidates:
        key = route_point_key(candidate)
        if key and (key in used or key in local_used):
            continue
        point = clone_local_point(
            candidate,
            day,
            len(day_points) + 1,
            times[min(len(day_points), len(times) - 1)],
            day * 1000 + len(day_points),
        )
        if not point or point_far_out_of_region(point, region):
            continue
        distance_from_previous = route_point_distance_km(previous, point) if previous else None
        distance_to_lodging = route_point_distance_km(point, end_anchor)
        if distance_from_previous is not None and distance_from_previous > limit:
            continue
        if distance_to_lodging is not None and distance_to_lodging > limit:
            continue

        day_points.append(point)
        if key:
            local_used.add(key)
            used.add(key)
        previous = point
        needed -= 1
        if needed <= 0:
            break

    return day_points


def ensure_daily_lodging_return(points: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    region = str(payload.get("region", "국내")).strip() or "국내"
    total_days = trip_day_count(payload)
    if not points:
        points = create_local_route_points(payload)

    lodging = lodging_from_route_points(points, region) or lodging_for_region(region, points)
    lodging = separate_lodging_from_route_points(lodging, points, region)
    grouped = group_points_by_day(points)
    result: list[dict[str, Any]] = []
    candidates = visit_candidates_for_region(region, payload)
    used: set[str] = set()

    for day in range(1, total_days + 1):
        day_points = [
            dict(point)
            for point in grouped.get(day, [])
            if not is_lodging_place_name(point.get("place")) and str(point.get("category", "")).strip() != "숙소"
        ]
        if not day_points:
            result.extend(build_missing_day_points(region, lodging, day, total_days, candidates, used))
            continue

        day_points = remove_trailing_departure_point(day_points)
        start_lodging = lodging_point(region, lodging, day, 1, "09:30", "start") if day > 1 else None
        end_lodging = lodging_point(region, lodging, day, 999, "19:00", "end")
        day_points = limit_day_route_distance(day_points, region, start_lodging, end_lodging)
        day_points = fill_day_visits(day_points, region, lodging, day, total_days, candidates, used, start_lodging)

        if not day_points:
            result.extend(build_missing_day_points(region, lodging, day, total_days, candidates, used))
            continue

        if start_lodging:
            day_points.insert(0, start_lodging)

        day_points.append(lodging_point(region, lodging, day, len(day_points) + 1, "19:00", "end"))

        result.extend(day_points)
        for point in day_points:
            key = route_point_key(point)
            if key and not is_repeatable_route_point(point):
                used.add(key)

    return normalize_day_orders(result)


def prepare_route_points(points: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    prepared = filter_unreachable_route_points(points, payload)
    prepared = repair_duplicate_route_points(prepared, payload)
    prepared = filter_unreachable_route_points(prepared, payload)
    prepared = ensure_daily_lodging_return(prepared, payload)
    return prepared


def create_fallback_plan(payload: dict[str, Any], reason: Exception) -> dict[str, Any]:
    route_points = prepare_route_points(create_local_route_points(payload), payload)
    transport_steps = normalize_transport_steps([], route_points, str(payload.get("transport", "")))
    warning = "AI 응답이 불안정하거나 요청 한도에 걸려 기본 발표용 일정으로 표시합니다. 잠시 뒤 다시 생성하면 AI 일정으로 바뀔 수 있습니다."
    return {
        "plan_markdown": build_plan_markdown(payload, route_points, transport_steps, "기본 발표용 여행 일정"),
        "route_points": route_points,
        "transport_steps": transport_steps,
        "meta": {
            "provider": "groq",
            "model": active_model_name(),
            "route_count": len(route_points),
            "transport_step_count": len(transport_steps),
            "fallback": True,
            "warning": warning,
            "fallback_reason": str(reason)[:240],
        },
    }


def create_local_plan(payload: dict[str, Any], warning: str | None = None) -> dict[str, Any]:
    route_points = prepare_route_points(create_local_route_points(payload), payload)
    transport_steps = normalize_transport_steps([], route_points, str(payload.get("transport", "")))
    return {
        "plan_markdown": build_plan_markdown(payload, route_points, transport_steps, "숙소를 기준으로 이어지는 로컬 빠른 일정"),
        "route_points": route_points,
        "transport_steps": transport_steps,
        "meta": {
            "provider": "local",
            "model": "local-fast-planner",
            "route_count": len(route_points),
            "transport_step_count": len(transport_steps),
            "local_mode": True,
            "warning": warning or "AI 호출 없이 빠른 로컬 일정으로 생성했습니다. 외부 공유용 기본 모드입니다.",
        },
    }


def create_plan(payload: dict[str, Any]) -> dict[str, Any]:
    region = str(payload.get("region", "")).strip()
    if not region or not str(payload.get("budget", "")).strip():
        raise ValueError("여행 지역과 예산을 입력해주세요.")

    live_groq = bool(payload.get("live_groq"))
    confirm_groq = bool(payload.get("confirm_groq"))
    if not live_groq:
        return create_local_plan(payload)
    if not confirm_groq:
        return create_local_plan(
            payload,
            "외부 공유 안정 모드입니다. API 한도 보호를 위해 실시간 GPT-OSS 호출은 잠겨 있고 로컬 일정으로 생성했습니다.",
        )

    if not GROQ_API_KEY:
        raise RuntimeError("배포 환경변수 GROQ_API_KEY가 설정되어 있지 않습니다. 이 오류는 API 호출 문제가 아니라 Render 설정 문제입니다.")

    try:
        parsed = generate_plan_data(payload)
        route_points = normalize_route_points(parsed.get("route_points"), region)
        route_points = prepare_route_points(route_points, payload)
        if not route_points:
            raise ValueError("AI가 방문지 목록을 만들지 못했습니다.")
    except Exception as exc:
        print(f"[groq-error] {type(exc).__name__}: {exc}", flush=True)
        raise

    transport_steps = normalize_transport_steps(parsed.get("transport_steps"), route_points, str(payload.get("transport", "")))
    plan_markdown = build_plan_markdown(
        payload,
        route_points,
        transport_steps,
        str(parsed.get("trip_theme", "")).strip(),
    )

    return {
        "plan_markdown": plan_markdown,
        "route_points": route_points,
        "transport_steps": transport_steps,
        "meta": {
            "provider": "groq",
            "model": active_model_name(),
            "route_count": len(route_points),
            "transport_step_count": len(transport_steps),
            "rate_limit": dict(GROQ_LAST_RATE_LIMIT_INFO),
        },
    }


class TravelPlannerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        client = self.client_address[0] if self.client_address else "local"
        print(f"[web] {client} - {format % args}")

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if unquote(parsed.path) != "/api/plan":
            self.send_json(404, {"error": "지원하지 않는 경로입니다."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw_body or "{}")
            result = create_plan(payload)
            self.send_json(200, result)
        except Exception as exc:
            print(f"[api-error] {type(exc).__name__}: {exc}", flush=True)
            self.send_json(400, {"error": str(exc) or type(exc).__name__})


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), TravelPlannerHandler)
    print(f"AI Travel Planner running on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
