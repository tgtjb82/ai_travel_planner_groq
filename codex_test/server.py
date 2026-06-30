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
GEOCODE_TIMEOUT_SECONDS = 3
WALKING_DISTANCE_KM = 1.6
WALKING_SPEED_KMH = 4.2
GEOCODE_CACHE: dict[str, dict[str, Any]] = {}
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

    for query in queries:
        cache_key = f"geo::{query}"
        if cache_key in GEOCODE_CACHE:
            return GEOCODE_CACHE[cache_key]

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
            lat = to_float(candidate.get("lat"))
            lng = to_float(candidate.get("lon"))
            if lat is None or lng is None or not is_korea_coordinate(lat, lng):
                continue

            result = {
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "address": candidate.get("display_name", address),
                "source": "OpenStreetMap",
            }
            GEOCODE_CACHE[cache_key] = result
            return result

    return {}


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

    center = geocode_place(region, "", "")
    center_lat = to_float(center.get("lat")) or 36.5
    center_lng = to_float(center.get("lng")) or 127.8
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

        if point.get("lat") is not None and point.get("lng") is not None:
            geo = fallback_coordinate(point, region)
        else:
            geo = geocode_place(point["place"], region, point["address"])
            if not geo:
                geo = fallback_coordinate(point, region)

        point["lat"] = geo["lat"]
        point["lng"] = geo["lng"]
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
            recommend_walk = distance <= WALKING_DISTANCE_KM

            public = raw.get("public_transport") if isinstance(raw.get("public_transport"), dict) else {}
            taxi = raw.get("taxi") if isinstance(raw.get("taxi"), dict) else {}
            public_instruction = str(public.get("instruction", "")).strip()
            if recommend_walk:
                public_instruction = f"도보 이용 추천. 약 {walk_minutes}분 소요"
            elif not public_instruction:
                public_instruction = "가까운 정류장/역에서 목적지 방향 버스 또는 지하철 이용"

            public_minutes = walk_minutes if recommend_walk else to_int(public.get("duration_minutes"), transit_minutes)
            public_fare = 0 if recommend_walk else to_int(public.get("fare_krw"), transit_fare)

            steps.append(
                {
                    "day": day,
                    "from_order": start["order"],
                    "to_order": end["order"],
                    "from_place": start["place"],
                    "to_place": end["place"],
                    "distance_km": round(distance, 1),
                    "transport_preference": transport,
                    "recommended_mode": "walk" if recommend_walk else "public",
                    "walk": {
                        "duration_minutes": walk_minutes,
                        "instruction": f"도보 이용 추천. 약 {walk_minutes}분 소요",
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
- 실제 장소명/상호명을 사용하고 가상의 장소는 만들지 마.
- 숙소가 들어갈 때는 place를 반드시 "숙소[정확한 숙소명]" 형식으로 써. 예: 숙소[나인트리 프리미어 호텔 명동2]
- 일정은 실제 여행처럼 이어져야 해. 1일차는 도착 거점에서 시작해 저녁에 숙소로 이동하고, 중간 날짜는 숙소에서 출발해 숙소로 돌아오며, 마지막날은 숙소에서 출발해 귀가/터미널/공항 거점으로 끝나게 구성해.
- 단, 무조건 모든 날짜의 시작과 끝이 숙소일 필요는 없고 여행 흐름상 자연스러운 경우에만 숙소를 넣어.
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
        for key, points in FALLBACK_TEMPLATES.items():
            if key in region or region in key:
                seed_places = [str(point.get("place", "")).strip() for point in points if point.get("place")]
                break
    except NameError:
        seed_places = []
    seed_text = ", ".join(seed_places[:14]) if seed_places else "Use well-known public landmarks, markets, parks, stations, beaches, museums, or streets."

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
- If lodging is naturally needed, write place as 숙소[exact real lodging name].
- summary and tip must be short Korean phrases.
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


def generate_groq_plan_text(payload: dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY가 설정되어 있지 않습니다.")

    max_tokens = min(6000, max(3000, 1800 + trip_day_count(payload) * 900))
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
    public_minutes = to_int(public.get("duration_minutes"), 0)
    taxi_minutes = to_int(taxi.get("duration_minutes"), 0)

    if step.get("recommended_mode") == "walk":
        return f"도보 {public_minutes}분 추천 / 택시 {taxi_minutes}분"

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
    for step in transport_steps:
        public = step.get("public_transport") if isinstance(step.get("public_transport"), dict) else {}
        taxi = step.get("taxi") if isinstance(step.get("taxi"), dict) else {}
        public_total += to_int(public.get("fare_krw"), 0)
        taxi_total += to_int(taxi.get("fare_krw"), 0)

    food_count = max(1, len([point for point in route_points if category_matches(point, ("맛집", "카페", "식당"))]))
    food_estimate = food_count * 18000
    return "\n".join(
        [
            "| 항목 | 예상 비용 |",
            "|---|---:|",
            f"| 입력 예산 | {table_text(budget) or '미입력'} |",
            f"| 대중교통 합계 | {format_krw(public_total)} |",
            f"| 택시 이동 시 합계 | {format_krw(taxi_total)} |",
            f"| 식비/카페 참고치 | {format_krw(food_estimate)} |",
        ]
    )


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
            "## 이동 정보 요약",
        ]
    )

    for step in transport_steps[:10]:
        lines.append(
            f"- {table_text(step.get('from_place'))} → {table_text(step.get('to_place'))}: "
            f"{move_summary(step)}"
        )

    lines.extend(
        [
            "",
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

REGION_CENTERS: dict[str, tuple[float, float]] = {
    "서울": (37.5665, 126.9780),
    "부산": (35.1796, 129.0756),
    "제주": (33.4996, 126.5312),
    "강릉": (37.7519, 128.8761),
    "대구": (35.8714, 128.6014),
    "대전": (36.3504, 127.3845),
    "광주": (35.1595, 126.8526),
    "여수": (34.7604, 127.6622),
    "전주": (35.8242, 127.1480),
    "경주": (35.8562, 129.2247),
    "인천": (37.4563, 126.7052),
    "속초": (38.2070, 128.5918),
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
}


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
    for key, center in REGION_CENTERS.items():
        if key in region:
            return center
    return 36.5, 127.8


def lodging_for_region(region: str, fallback_points: list[dict[str, Any]]) -> dict[str, Any]:
    for key, lodging in REGION_LODGINGS.items():
        if key in region:
            return dict(lodging)
    lat, lng = average_coordinate(fallback_points, region)
    return {
        "name": f"{region} 시내 숙소",
        "address": f"{region} 중심 숙박권",
        "lat": lat - 0.006,
        "lng": lng - 0.006,
    }


def fallback_template_for_region(region: str) -> list[dict[str, Any]]:
    for key, points in FALLBACK_TEMPLATES.items():
        if key in region:
            return points
    lat, lng = region_center(region)
    return [
        {"day": 1, "order": 1, "time": "10:00", "place": f"{region} 중심지", "address": region, "lat": lat, "lng": lng, "category": "이동", "summary": "여행 시작 기준점", "tip": "숙소 위치에 맞춰 조정하세요."},
        {"day": 1, "order": 2, "time": "11:30", "place": f"{region} 대표 산책지", "address": region, "lat": lat + 0.01, "lng": lng + 0.01, "category": "관광지", "summary": "가볍게 둘러보는 산책 코스", "tip": "날씨에 따라 실내 코스로 바꾸세요."},
        {"day": 1, "order": 3, "time": "13:00", "place": f"{region} 로컬 맛집 거리", "address": region, "lat": lat + 0.015, "lng": lng + 0.004, "category": "맛집", "summary": "점심과 휴식을 넣기 좋은 구역", "tip": "현장 대기 시간을 고려하세요."},
        {"day": 2, "order": 1, "time": "10:00", "place": f"{region} 카페 거리", "address": region, "lat": lat - 0.01, "lng": lng + 0.012, "category": "카페", "summary": "여유 있게 쉬어가는 코스", "tip": "창가 좌석은 빨리 차는 편입니다."},
        {"day": 2, "order": 2, "time": "13:00", "place": f"{region} 전통시장", "address": region, "lat": lat - 0.006, "lng": lng - 0.01, "category": "맛집", "summary": "먹거리와 기념품을 보는 코스", "tip": "현금이 있으면 편합니다."},
        {"day": 3, "order": 1, "time": "10:00", "place": f"{region} 전망 명소", "address": region, "lat": lat + 0.018, "lng": lng - 0.012, "category": "관광지", "summary": "여행을 마무리하기 좋은 전망 코스", "tip": "체크아웃 전후 시간을 맞추세요."},
        {"day": 3, "order": 2, "time": "13:00", "place": f"{region} 기념품 거리", "address": region, "lat": lat - 0.016, "lng": lng + 0.006, "category": "문화", "summary": "기념품과 가벼운 식사를 넣기 좋은 곳", "tip": "이동 전 여유 시간을 남기세요."},
    ]


def average_coordinate(points: list[dict[str, Any]], region: str) -> tuple[float, float]:
    valid = [
        (float(point["lat"]), float(point["lng"]))
        for point in points
        if to_float(point.get("lat")) is not None and to_float(point.get("lng")) is not None
    ]
    if not valid:
        return region_center(region)
    return sum(lat for lat, _ in valid) / len(valid), sum(lng for _, lng in valid) / len(valid)


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
    point["location_source"] = "로컬 일정 좌표"
    return point


def transport_hub_for_region(region: str, template_points: list[dict[str, Any]]) -> dict[str, Any]:
    for point in template_points:
        if str(point.get("category", "")) == "이동":
            return dict(point)
    lat, lng = region_center(region)
    return {
        "day": 1,
        "order": 1,
        "time": "10:00",
        "place": f"{region} 도착 지점",
        "address": region,
        "lat": lat,
        "lng": lng,
        "category": "이동",
        "summary": "여행 시작과 귀가 기준점",
        "tip": "실제 터미널/역 위치에 맞춰 조정하세요.",
    }


def ensure_visit_pool(points: list[dict[str, Any]], required_count: int, region: str) -> list[dict[str, Any]]:
    expanded = [dict(point) for point in points]
    if len(expanded) >= required_count:
        return expanded

    lat, lng = region_center(region)
    fillers = [
        ("전망 산책 코스", "관광지", "여행 분위기를 정리하기 좋은 전망 코스", 0.018, -0.012),
        ("로컬 카페 거리", "카페", "휴식과 사진을 함께 넣기 좋은 거리", -0.012, 0.014),
        ("기념품 거리", "문화", "가볍게 쇼핑하고 식사하기 좋은 구역", 0.006, 0.018),
        ("전통시장", "맛집", "먹거리와 지역 분위기를 보는 코스", -0.014, -0.01),
    ]

    filler_index = 0
    while len(expanded) < required_count:
        name, category, summary, lat_offset, lng_offset = fillers[filler_index % len(fillers)]
        lap = filler_index // len(fillers)
        expanded.append(
            {
                "day": max(1, len(expanded) // 3 + 1),
                "order": len(expanded) + 1,
                "time": "",
                "place": f"{region} {name}" if lap == 0 else f"{region} {name} {lap + 1}",
                "address": region,
                "lat": round(lat + lat_offset + lap * 0.004, 6),
                "lng": round(lng + lng_offset - lap * 0.004, 6),
                "category": category,
                "summary": summary,
                "tip": "실제 운영 여부는 출발 전 확인하세요.",
            }
        )
        filler_index += 1

    return expanded


def lodging_point(region: str, lodging: dict[str, Any], day: int, order: int, time: str, suffix: str) -> dict[str, Any]:
    lat, lng = region_center(region)
    lodging_lat = to_float(lodging.get("lat"))
    lodging_lng = to_float(lodging.get("lng"))
    name = str(lodging.get("name") or f"{region} 시내 숙소").strip()
    address = str(lodging.get("address") or f"{region} 중심 숙박권").strip()
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
        "location_source": "로컬 숙소 기준 좌표",
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
    lodging = lodging_for_region(region, visit_pool)
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


def create_fallback_plan(payload: dict[str, Any], reason: Exception) -> dict[str, Any]:
    route_points = create_local_route_points(payload)
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
    route_points = create_local_route_points(payload)
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
        },
    }


class TravelPlannerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

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
    print(f"AI Travel Planner running on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
