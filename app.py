
import streamlit as st
import pandas as pd
import re
import itertools
import urllib.parse
import os
import json
import shutil
from pathlib import Path
from sqlalchemy import create_engine, text as sql_text
from datetime import date, datetime, timedelta
from dataclasses import dataclass, asdict, is_dataclass
from typing import List

st.set_page_config(page_title="Flight Trip Optimizer", layout="wide")

# -----------------------------
# Models
# -----------------------------
@dataclass(frozen=True)
class Leg:
    origin: str
    destination: str
    departure_date: str

# -----------------------------
# Helpers
# -----------------------------
def norm_code(v: str) -> str:
    return re.sub(r"[^A-Za-z]", "", v or "").upper()[:3]

# Frequently used airport/city labels. Unknown codes remain usable as-is.
AIRPORT_LABELS = {
    "ICN": "인천/서울",
    "GMP": "김포/서울",
    "PUS": "김해/부산",
    "CJU": "제주",
    "IST": "이스탄불",
    "SAW": "사비하 괵첸/이스탄불",
    "ATH": "아테네",
    "JTR": "산토리니",
    "DXB": "두바이",
    "DOH": "도하",
    "AUH": "아부다비",
    "PEK": "베이징",
    "PKX": "베이징 다싱",
    "CAN": "광저우",
    "HKG": "홍콩",
    "SIN": "싱가포르",
    "BKK": "방콕",
    "NRT": "나리타/도쿄",
    "HND": "하네다/도쿄",
    "KIX": "간사이/오사카",
    "LHR": "히스로/런던",
    "CDG": "샤를드골/파리",
    "FCO": "피우미치노/로마",
    "FRA": "프랑크푸르트",
    "MUC": "뮌헨",
    "AMS": "암스테르담",
    "MAD": "마드리드",
    "BCN": "바르셀로나",
    "VIE": "빈",
    "ZRH": "취리히",
    "JFK": "JFK/뉴욕",
    "LAX": "로스앤젤레스",
    "SFO": "샌프란시스코",
}

def airport_label(code: str) -> str:
    code = norm_code(code)
    name = AIRPORT_LABELS.get(code)
    return f"{code} ({name})" if name else code

def yymmdd(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return dt.strftime("%y%m%d")

def google_oneway_url(o, d, dep):
    q = f"Flights from {o} to {d} on {dep} one way"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(q) + "&curr=KRW&hl=ko"

def google_multicity_url(legs: List[Leg]):
    parts = [f"{x.origin} to {x.destination} on {x.departure_date}" for x in legs]
    q = "Multi city flights " + ", ".join(parts)
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(q) + "&curr=KRW&hl=ko"

def skyscanner_oneway_url(o, d, dep):
    return f"https://www.skyscanner.co.kr/transport/flights/{o.lower()}/{d.lower()}/{yymmdd(dep)}/?adultsv2=1&cabinclass=economy&currency=KRW"

def skyscanner_multicity_url(legs: List[Leg]):
    params = {
        "adultsv2": "1",
        "cabinclass": "economy",
        "market": "KR",
        "locale": "ko-KR",
        "currency": "KRW",
    }
    for i, x in enumerate(legs[:6]):
        params[f"origin{i}"] = x.origin
        params[f"date{i}"] = x.departure_date
        params[f"destination{i}"] = x.destination
    return "https://www.skyscanner.co.kr/flights/multicity?" + urllib.parse.urlencode(params)

def route_key(route):
    return " → ".join(airport_label(x) for x in route)

def generate_physical_routes(home: str, visits: List[str], max_revisit_per_city=0, max_extra_legs=0):
    """
    Generate only the chronological physical travel route.

    A city that has already been visited is never inserted again merely to create
    more ticketing combinations. Ticketing alternatives are generated separately
    by partitioning this fixed route into one-way / round-trip / multi-city tickets.

    Example:
      ICN -> IST -> JTR -> ATH -> ICN
    stays exactly that physical route.
    """
    if not visits:
        return [[home, home]]
    return [[home] + list(visits) + [home]]


def build_route_and_legs_from_stays(home: str, home_departure: str, stays: list):
    ordered = sorted(
        stays,
        key=lambda x: (
            x.get("arrival_date", "9999-12-31"),
            x.get("departure_date", "9999-12-31"),
            x.get("city", ""),
            int(x.get("visit_no", 1)),
        ),
    )
    route = [home] + [s["city"] for s in ordered] + [home]
    legs = []
    if ordered:
        legs.append(Leg(home, ordered[0]["city"], home_departure))
        for i in range(len(ordered) - 1):
            legs.append(Leg(
                ordered[i]["city"],
                ordered[i+1]["city"],
                ordered[i]["departure_date"],
            ))
        legs.append(Leg(ordered[-1]["city"], home, ordered[-1]["departure_date"]))
    return route, legs, ordered

def generate_ticket_patterns_from_legs(route: List[str], legs: List[Leg]):
    patterns = []
    for groups in all_contiguous_partitions(len(legs)):
        patterns.append(("contiguous", [list(range(s, e)) for s, e in groups]))

    # Only create round-trip grouping if both outbound and return legs really exist
    # in the user-entered physical itinerary.
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            a, b = legs[i], legs[j]
            if a.origin == b.destination and a.destination == b.origin:
                patterns.append(("explicit_roundtrip", [[i, j]]))

    seen, unique = set(), []
    for kind, tickets in patterns:
        key = tuple(sorted(tuple(sorted(t)) for t in tickets))
        if key not in seen:
            seen.add(key)
            unique.append((kind, tickets))
    return unique


def all_contiguous_partitions(nlegs: int):
    if nlegs <= 0:
        return []
    result = []
    for mask in range(1 << (nlegs - 1)):
        groups = []
        start = 0
        for i in range(nlegs - 1):
            if mask & (1 << i):
                groups.append((start, i + 1))
                start = i + 1
        groups.append((start, nlegs))
        result.append(groups)
    return result

def symmetric_roundtrip_partition(route: List[str]):
    if route != route[::-1]:
        return None
    groups = []
    n = len(route)
    for i in range((n - 1) // 2):
        j = n - 2 - i
        groups.append([i, j])
    if (n - 1) % 2 == 1:
        groups.append([(n - 1) // 2])
    return groups

def build_legs_for_route(route: List[str], departure_dates_by_city: dict, final_required_city=None):
    """
    Build dated legs from the fixed chronological route.
    Each leg departs on the user-entered departure date of its origin city.
    No previously visited city is reinserted.
    """
    legs = []
    fallback = max(departure_dates_by_city.values()) if departure_dates_by_city else date.today().isoformat()
    for a, b in zip(route[:-1], route[1:]):
        dep = departure_dates_by_city.get(a, fallback)
        legs.append(Leg(a, b, dep))
    return legs

def generate_ticket_patterns(route: List[str], departure_dates_by_city: dict):
    legs = build_legs_for_route(route, departure_dates_by_city)
    patterns = []

    for groups in all_contiguous_partitions(len(legs)):
        tickets = [list(range(s, e)) for s, e in groups]
        patterns.append(("contiguous", tickets))

    sym = symmetric_roundtrip_partition(route)
    if sym:
        patterns.append(("nested_roundtrip", sym))

    seen, unique = set(), []
    for kind, tickets in patterns:
        key = tuple(sorted(tuple(sorted(t)) for t in tickets))
        if key not in seen:
            seen.add(key)
            unique.append((kind, tickets))
    return legs, unique

def rebuild_generated_from_saved_state(generated):
    if not generated:
        return generated

    home = generated.get("home")
    dep_map = generated.get("dates") or {}
    arr_map = generated.get("arrival_dates") or {}
    exact_map = generated.get("exact_arrival_required") or {}
    home_departure = dep_map.get(home)
    if not home or not home_departure:
        return generated

    stays = generated.get("itinerary_stays")
    if not stays:
        stays = []
        seen = {}
        for city in generated.get("visits") or []:
            seen[city] = seen.get(city, 0) + 1
            stays.append({
                "city": city,
                "visit_no": seen[city],
                "arrival_date": arr_map.get(city),
                "departure_date": dep_map.get(city),
                "exact_arrival": bool(exact_map.get(city, False)),
            })

    route, legs, ordered = build_route_and_legs_from_stays(home, home_departure, stays)
    defs = generate_ticket_patterns_from_legs(route, legs)
    patterns = [{
        "route": route,
        "legs": legs,
        "kind": kind,
        "tickets": tickets,
        "pattern_id": f"R1-P{i+1}",
    } for i, (kind, tickets) in enumerate(defs)]

    rebuilt = dict(generated)
    rebuilt["itinerary_stays"] = [dict(s) for s in stays]
    rebuilt["visits"] = [s["city"] for s in ordered]
    rebuilt["patterns"] = patterns
    return rebuilt


def clear_trip_form_widget_state():
    fixed_keys = {
        "home_input",
        "visit_input",
        "flex_input",
        "topn_input",
        "home_departure_date",
        "home_final_arrival_date",
        "exact_home_arrival",
    }
    dynamic_prefixes = (
        "repeat_count_",
        "arr_",
        "dep_",
        "exact_arrival_",
        "add_repeat_",
        "remove_repeat_",
    )
    for key in list(st.session_state.keys()):
        if key in fixed_keys or key.startswith(dynamic_prefixes):
            st.session_state.pop(key, None)

def _strict_saved_date(value, field_name):
    if value is None or str(value).strip() == "":
        raise ValueError(f"저장된 여행의 {field_name} 날짜가 비어 있습니다.")
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(
            f"저장된 여행의 {field_name} 날짜 형식이 잘못되었습니다: {value}"
        ) from exc

def sync_widgets_from_generated(generated):
    if not generated:
        return

    home = generated.get("home") or "ICN"
    dep = generated.get("dates") or {}
    arr = generated.get("arrival_dates") or {}
    exact = generated.get("exact_arrival_required") or {}
    stays = [dict(s) for s in (generated.get("itinerary_stays") or [])]

    if not stays:
        counts = {}
        for city in generated.get("visits") or []:
            counts[city] = counts.get(city, 0) + 1
            stays.append({
                "city": city,
                "visit_no": counts[city],
                "arrival_date": arr.get(city),
                "departure_date": dep.get(city),
                "exact_arrival": bool(exact.get(city, False)),
            })

    clear_trip_form_widget_state()

    unique_cities = []
    for s in stays:
        city = s.get("city")
        if city and city not in unique_cities:
            unique_cities.append(city)

    st.session_state["home_input"] = home
    st.session_state["visit_input"] = ",".join(unique_cities)
    st.session_state["flex_input"] = int(generated.get("flex", 1) or 0)
    st.session_state["topn_input"] = int(generated.get("topn", 10) or 10)

    st.session_state["home_departure_date"] = _strict_saved_date(
        dep.get(home), f"{home} 출발"
    )
    st.session_state["home_final_arrival_date"] = _strict_saved_date(
        generated.get("home_final_arrival") or arr.get(home),
        f"{home} 최종 도착"
    )
    st.session_state["exact_home_arrival"] = bool(exact.get(home, False))

    counts = {}
    for s in stays:
        city = s.get("city")
        if not city:
            continue
        counts[city] = counts.get(city, 0) + 1
        n = counts[city]

        st.session_state[f"repeat_count_{city}"] = n
        st.session_state[f"arr_{city}_{n}"] = _strict_saved_date(
            s.get("arrival_date"), f"{city} 방문 #{n} 도착"
        )
        st.session_state[f"dep_{city}_{n}"] = _strict_saved_date(
            s.get("departure_date"), f"{city} 방문 #{n} 출발"
        )
        st.session_state[f"exact_arrival_{city}_{n}"] = bool(
            s.get("exact_arrival", False)
        )

    st.session_state["skip_form_autosave_once"] = True

def format_ticket(legs: List[Leg], idxs: List[int]):
    return " / ".join(f"{airport_label(legs[i].origin)} → {airport_label(legs[i].destination)} ({legs[i].departure_date})" for i in idxs)

def search_key_for_ticket(legs, idxs):
    return " | ".join(f"{legs[i].origin}-{legs[i].destination}-{legs[i].departure_date}" for i in idxs)

def ticket_search_rows(legs, idxs):
    rows = []
    for n, i in enumerate(idxs, 1):
        leg = legs[i]
        rows.append({
            "구간": n,
            "출발": airport_label(leg.origin),
            "도착": airport_label(leg.destination),
            "출발일": leg.departure_date,
        })
    return rows

def ticket_search_summary(legs, idxs):
    parts = []
    for i in idxs:
        leg = legs[i]
        parts.append(
            f"{airport_label(leg.origin)} → {airport_label(leg.destination)} / {leg.departure_date}"
        )
    return "  |  ".join(parts)

def ticket_search_type(legs, idxs):
    if len(idxs) == 1:
        return "편도"
    if len(idxs) == 2:
        a = legs[idxs[0]]
        b = legs[idxs[1]]
        if a.origin == b.destination and a.destination == b.origin:
            return "왕복"
    return "다구간"

def ticket_search_title(legs, idxs):
    typ = ticket_search_type(legs, idxs)
    if typ == "편도":
        leg = legs[idxs[0]]
        return f"{airport_label(leg.origin)} → {airport_label(leg.destination)} 편도"
    if typ == "왕복":
        leg = legs[idxs[0]]
        return f"{airport_label(leg.origin)} ↔ {airport_label(leg.destination)} 왕복"
    first = legs[idxs[0]]
    last = legs[idxs[-1]]
    return f"{airport_label(first.origin)} → … → {airport_label(last.destination)} 다구간"

def collect_unique_search_tasks(patterns):
    """
    Deduplicate every Ticket that appears in any generated ticketing pattern.
    This becomes the user's master 'what do I need to search?' checklist.
    """
    tasks = {}
    for p in patterns:
        for idxs in p["tickets"]:
            key = search_key_for_ticket(p["legs"], idxs)
            if key not in tasks:
                tasks[key] = {
                    "key": key,
                    "legs": p["legs"],
                    "idxs": idxs,
                    "type": ticket_search_type(p["legs"], idxs),
                    "title": ticket_search_title(p["legs"], idxs),
                    "summary": ticket_search_summary(p["legs"], idxs),
                    "rows": ticket_search_rows(p["legs"], idxs),
                }
    order = {"편도": 0, "왕복": 1, "다구간": 2}
    return sorted(
        tasks.values(),
        key=lambda x: (order.get(x["type"], 9), x["title"], x["summary"])
    )

def money_to_int(s):
    digits = re.sub(r"[^\d]", "", str(s or ""))
    return int(digits) if digits else 0

def offer_uid(offer, idx=None):
    parts = [
        str(offer.get("search_key", "")),
        str(offer.get("airline", "")),
        str(offer.get("departure_time", "")),
        str(offer.get("arrival_time", "")),
        str(offer.get("price_krw", "")),
        str(idx if idx is not None else ""),
    ]
    return "||".join(parts)

def is_valid_offer(offer):
    if not offer:
        return False
    dep = str(offer.get("departure_time", "") or "").strip()
    arr = str(offer.get("arrival_time", "") or "").strip()
    try:
        price = int(offer.get("price_krw", 0) or 0)
    except Exception:
        price = 0
    return bool(dep and arr and price > 0)

def valid_offers_only(offers):
    return [o for o in (offers or []) if is_valid_offer(o)]

def _korean_ampm_to_24h(token: str) -> str:
    token = re.sub(r"\s+", " ", token.strip())
    m = re.match(r"(오전|오후)\s*(\d{1,2}):(\d{2})", token)
    if not m:
        return token
    ap, hh, mm = m.groups()
    hh = int(hh)
    if ap == "오전":
        hh = 0 if hh == 12 else hh
    else:
        hh = 12 if hh == 12 else hh + 12
    return f"{hh:02d}:{mm}"

def _extract_times(blob: str):
    out = []
    for m in re.finditer(r"(오전|오후)\s*(\d{1,2}):(\d{2})", blob):
        out.append((m.start(), _korean_ampm_to_24h(m.group(0))))
    for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", blob):
        prefix = blob[max(0, m.start()-4):m.start()]
        if "오전" in prefix or "오후" in prefix:
            continue
        out.append((m.start(), f"{int(m.group(1)):02d}:{m.group(2)}"))
    out.sort(key=lambda x: x[0])
    return [x[1] for x in out]


def _guess_airline_name(lines):
    """
    Guess airline name from the lines belonging to one result card.
    It looks for text before the first departure time and filters out labels,
    airports, prices, ads and common UI text.
    """
    banned_exact = {
        "상품 가격", "선택하기", "더 보기", "광고", "직항",
        "ICN", "IST", "ATH", "GMP", "SAW",
    }
    banned_contains = [
        "예약 최저가", "총", "경유", "시간", "분", "₩", "KRW",
        "오전", "오후", "선택", "상품", "광고",
    ]

    candidates = []
    for line in lines:
        s = re.sub(r"\s+", " ", str(line)).strip()
        if not s:
            continue
        if s in banned_exact:
            continue
        if any(x in s for x in banned_contains):
            continue
        if re.fullmatch(r"[A-Z]{3}", s):
            continue
        if re.fullmatch(r"[\d\s:,+\-]+", s):
            continue
        if re.search(r"\d{1,2}:\d{2}", s):
            continue

        # Remove obvious UI suffixes.
        s = re.sub(r"\s*(예약 최저가|상품 가격).*?$", "", s).strip()

        # Prefer human-readable carrier names, including Korean names.
        if re.search(r"[A-Za-z가-힣]", s):
            candidates.append(s)

    if not candidates:
        return ""

    # In Skyscanner, the carrier label is usually the last useful text line
    # before the departure-time row.
    value = candidates[-1]

    # Avoid taking marketplace/banner lines as airline names.
    if value.lower() in {"trip.com", "skyscanner"}:
        return ""
    return value[:80]


def parse_result_text(text: str, search_key: str, source_url: str = ""):
    """
    Parses pasted text from Skyscanner/Google Flights.
    Example:
    Emirates
    오후 11:40 → 오후 2:25 +1
    20시간 45분
    1회 경유 DXB
    ₩823,600
    """
    if not text.strip():
        return []

    raw_lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]
    chunks = []
    current = []
    price_re = re.compile(r"(?:₩|KRW\s*)\s*[\d,]{4,}", flags=re.I)

    for line in raw_lines:
        current.append(line)
        if price_re.search(line):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    rows = []
    for ch in chunks:
        blob = " | ".join(ch)
        prices = re.findall(r"(?:₩|KRW\s*)\s*([\d,]{4,})", blob, flags=re.I)
        if not prices:
            prices = re.findall(r"\b(\d{2,3}(?:,\d{3})+)\b", blob)

        times = _extract_times(blob)
        dur = re.search(r"(\d+)\s*(?:시간|hr|hrs|h)\s*(?:(\d+)\s*(?:분|min|mins|m))?", blob, flags=re.I)
        stop_match = re.search(
            r"(직항|nonstop|direct|(\d+)\s*(?:회\s*)?경유(?:\s*([A-Z]{3}))?|\d+\s*stop(?:s)?)",
            blob, flags=re.I
        )

        if prices and len(times) >= 2:
            dep, arr = times[0], times[1]
            dmin = (int(dur.group(1))*60 + int(dur.group(2) or 0)) if dur else 0
            offset = 2 if "+2" in blob else (1 if "+1" in blob else 0)
            stops = re.sub(r"\s+", " ", stop_match.group(1)).strip() if stop_match else ""

            rows.append({
                "선택": True,
                "search_key": search_key,
                "항공사": _guess_airline_name(ch),
                "출발시간": dep,
                "도착시간": arr,
                "도착+일": offset,
                "소요시간(분)": dmin,
                "경유": stops,
                "가격(KRW)": money_to_int(prices[-1]),
                "수하물": "",
                "추가수하물kg": 0,
                "추가수하물가격": 0,
                "수하물포함체크": False,
                "source_url": source_url,
                "원문": blob[:500],
            })
    return rows


# -----------------------------
# Persistent storage
# Cloud PostgreSQL when DATABASE_URL exists; local SQLite otherwise.
# -----------------------------
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
BACKUP_DIR = APP_DIR / "backup"
SQLITE_PATH = DATA_DIR / "flight_optimizer.db"

DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

RAW_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if RAW_DATABASE_URL:
    # SQLAlchemy + psycopg v3. Accept common provider URLs.
    if RAW_DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql+psycopg://" + RAW_DATABASE_URL[len("postgres://"):]
    elif RAW_DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = "postgresql+psycopg://" + RAW_DATABASE_URL[len("postgresql://"):]
    else:
        DATABASE_URL = RAW_DATABASE_URL
    DB_MODE = "cloud"
else:
    DATABASE_URL = f"sqlite:///{SQLITE_PATH.as_posix()}"
    DB_MODE = "local"

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

def _jsonable(value):
    """Recursively convert app objects into JSON-safe objects."""
    if isinstance(value, Leg):
        return {
            "__type__": "Leg",
            "origin": value.origin,
            "destination": value.destination,
            "departure_date": value.departure_date,
        }
    if isinstance(value, (date, datetime)):
        return {"__type__": "datetime", "value": value.isoformat()}
    if is_dataclass(value):
        data = asdict(value)
        data["__type__"] = value.__class__.__name__
        return {str(k): _jsonable(v) for k, v in data.items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Last-resort safe representation so export/download can never crash
    # on a non-JSON-native object.
    return str(value)

def _json_default(value):
    """Fallback serializer for json.dumps(default=...)."""
    return _jsonable(value)

def _restore_objects(value):
    """Restore objects that must remain structured after DB reload."""
    if isinstance(value, dict):
        if value.get("__type__") == "Leg":
            return Leg(
                origin=value["origin"],
                destination=value["destination"],
                departure_date=value["departure_date"],
            )
        if value.get("__type__") == "datetime":
            return value.get("value")
        return {k: _restore_objects(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_objects(v) for v in value]
    return value

def init_db():
    # Portable schema for both SQLite and PostgreSQL.
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS app_state (
                key VARCHAR(100) PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
        """))
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                search_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
        """) if DB_MODE == "cloud" else sql_text("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
        """))

def save_state(key, value):
    payload = json.dumps(_jsonable(value), ensure_ascii=False, default=_json_default)
    now = datetime.now().isoformat(timespec="seconds")
    with engine.begin() as conn:
        # Avoid dialect-specific UPSERT syntax.
        result = conn.execute(
            sql_text("UPDATE app_state SET value_json=:payload, updated_at=:now WHERE key=:key"),
            {"payload": payload, "now": now, "key": key},
        )
        if result.rowcount == 0:
            conn.execute(
                sql_text("INSERT INTO app_state(key, value_json, updated_at) VALUES (:key, :payload, :now)"),
                {"key": key, "payload": payload, "now": now},
            )

def load_state(key, default=None):
    with engine.begin() as conn:
        row = conn.execute(
            sql_text("SELECT value_json FROM app_state WHERE key=:key"),
            {"key": key},
        ).mappings().first()
    if not row:
        return default
    try:
        return _restore_objects(json.loads(row["value_json"]))
    except Exception:
        return default

def save_all_offers(offers):
    now = datetime.now().isoformat(timespec="seconds")
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM offers"))
        for offer in offers:
            conn.execute(
                sql_text("""
                    INSERT INTO offers(search_key, payload_json, created_at, updated_at)
                    VALUES (:search_key, :payload, :created_at, :updated_at)
                """),
                {
                    "search_key": offer.get("search_key", ""),
                    "payload": json.dumps(_jsonable(offer), ensure_ascii=False, default=_json_default),
                    "created_at": now,
                    "updated_at": now,
                },
            )

def load_all_offers():
    result = []
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("SELECT payload_json FROM offers ORDER BY id")
        ).mappings().all()
    for row in rows:
        try:
            result.append(_restore_objects(json.loads(row["payload_json"])))
        except Exception:
            pass
    return result

def autosave():
    if DB_INIT_ERROR:
        return
    save_state("generated", st.session_state.get("generated"))
    save_all_offers(st.session_state.get("offers", []))
    if "save_active_trip_if_any" in globals():
        save_active_trip_if_any()

def export_snapshot_json():
    snapshot = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "db_mode": DB_MODE,
        "generated": st.session_state.get("generated"),
        "offers": st.session_state.get("offers", []),
    }
    safe_snapshot = _jsonable(snapshot)
    return json.dumps(
        safe_snapshot,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    ).encode("utf-8")

def daily_backup():
    # Local SQLite only. Cloud PostgreSQL is persistent independently of Render's filesystem.
    if DB_MODE != "local" or not SQLITE_PATH.exists():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d")
    target = BACKUP_DIR / f"flight_optimizer_{stamp}.db"
    if not target.exists():
        shutil.copy2(SQLITE_PATH, target)
        return target
    return None

DB_INIT_ERROR = None
try:
    init_db()
    daily_backup()
except Exception as exc:
    DB_INIT_ERROR = str(exc)


# -----------------------------
# Named trip projects
# -----------------------------
def ensure_trip_table():
    """
    Create named-trip table using the same SQLAlchemy engine used by v3.9.
    Works for both PostgreSQL and SQLite.
    """
    if DB_INIT_ERROR:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS trip_projects (
                trip_id VARCHAR(120) PRIMARY KEY,
                trip_name VARCHAR(255) NOT NULL UNIQUE,
                snapshot_json TEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
        """))

def _make_trip_id():
    return datetime.now().strftime("trip_%Y%m%d_%H%M%S_%f")

def current_trip_snapshot():
    # Manual save must capture the CURRENT visible form, not merely the last
    # state produced by pressing "경로·발권 후보 생성".
    generated_for_save = (
        st.session_state.get("draft_generated")
        or st.session_state.get("generated")
    )
    return {
        "generated": _jsonable(generated_for_save),
        "offers": _jsonable(valid_offers_only(st.session_state.get("offers", []))),
    }

def save_named_trip(trip_name, trip_id=None):
    if DB_INIT_ERROR:
        raise RuntimeError(f"DB 초기화 오류: {DB_INIT_ERROR}")

    trip_name = (trip_name or "").strip()
    if not trip_name:
        raise ValueError("여행 이름을 입력하세요.")

    snapshot = json.dumps(_jsonable(current_trip_snapshot()), ensure_ascii=False, default=_json_default)
    now = datetime.now().isoformat(timespec="seconds")
    if not trip_id:
        trip_id = _make_trip_id()

    with engine.begin() as conn:
        # Check duplicate name belonging to another trip.
        duplicate = conn.execute(
            sql_text("""
                SELECT trip_id
                FROM trip_projects
                WHERE trip_name=:trip_name AND trip_id<>:trip_id
            """),
            {"trip_name": trip_name, "trip_id": trip_id},
        ).mappings().first()
        if duplicate:
            raise ValueError("같은 이름의 여행이 이미 있습니다.")

        result = conn.execute(
            sql_text("""
                UPDATE trip_projects
                SET trip_name=:trip_name,
                    snapshot_json=:snapshot,
                    updated_at=:updated_at
                WHERE trip_id=:trip_id
            """),
            {
                "trip_name": trip_name,
                "snapshot": snapshot,
                "updated_at": now,
                "trip_id": trip_id,
            },
        )

        if result.rowcount == 0:
            conn.execute(
                sql_text("""
                    INSERT INTO trip_projects(
                        trip_id, trip_name, snapshot_json, created_at, updated_at
                    )
                    VALUES(
                        :trip_id, :trip_name, :snapshot, :created_at, :updated_at
                    )
                """),
                {
                    "trip_id": trip_id,
                    "trip_name": trip_name,
                    "snapshot": snapshot,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    return trip_id

def list_named_trips():
    if DB_INIT_ERROR:
        return []
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("""
                SELECT trip_id, trip_name, created_at, updated_at
                FROM trip_projects
                ORDER BY updated_at DESC
            """)
        ).mappings().all()
    return [dict(r) for r in rows]

def load_named_trip(trip_id):
    if DB_INIT_ERROR:
        raise RuntimeError(f"DB 초기화 오류: {DB_INIT_ERROR}")

    with engine.begin() as conn:
        row = conn.execute(
            sql_text("""
                SELECT trip_name, snapshot_json
                FROM trip_projects
                WHERE trip_id=:trip_id
            """),
            {"trip_id": trip_id},
        ).mappings().first()

    if not row:
        return None

    data = json.loads(row["snapshot_json"])
    generated_raw = _restore_objects(data.get("generated"))
    offers = _restore_objects(data.get("offers", []))

    if not generated_raw:
        raise ValueError("저장된 여행 일정 데이터가 없습니다.")

    st.session_state.pop("pending_manual_trip_save", None)
    st.session_state.active_trip_id = trip_id
    st.session_state.active_trip_name = row["trip_name"]
    st.session_state.offers = offers

    # Restore exact saved date values first.
    sync_widgets_from_generated(generated_raw)

    # Rebuild only derived route/ticket pattern objects.
    generated = rebuild_generated_from_saved_state(generated_raw)
    st.session_state.generated = generated
    st.session_state["draft_generated"] = generated

    save_state("generated", generated)
    save_all_offers(offers)
    st.session_state["last_autosave_at"] = datetime.now().isoformat(timespec="seconds")
    st.session_state["last_autosave_error"] = None
    return row["trip_name"]

def rename_named_trip(trip_id, new_name):
    if DB_INIT_ERROR:
        raise RuntimeError(f"DB 초기화 오류: {DB_INIT_ERROR}")

    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("새 이름을 입력하세요.")

    now = datetime.now().isoformat(timespec="seconds")

    with engine.begin() as conn:
        duplicate = conn.execute(
            sql_text("""
                SELECT trip_id
                FROM trip_projects
                WHERE trip_name=:trip_name AND trip_id<>:trip_id
            """),
            {"trip_name": new_name, "trip_id": trip_id},
        ).mappings().first()
        if duplicate:
            raise ValueError("같은 이름의 여행이 이미 있습니다.")

        conn.execute(
            sql_text("""
                UPDATE trip_projects
                SET trip_name=:trip_name, updated_at=:updated_at
                WHERE trip_id=:trip_id
            """),
            {
                "trip_name": new_name,
                "updated_at": now,
                "trip_id": trip_id,
            },
        )

def delete_named_trip(trip_id):
    if DB_INIT_ERROR:
        raise RuntimeError(f"DB 초기화 오류: {DB_INIT_ERROR}")

    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM trip_projects WHERE trip_id=:trip_id"),
            {"trip_id": trip_id},
        )

def save_active_trip_if_any():
    trip_id = st.session_state.get("active_trip_id")
    trip_name = st.session_state.get("active_trip_name")
    if trip_id and trip_name and not DB_INIT_ERROR:
        save_named_trip(trip_name, trip_id=trip_id)


def autosave_active_trip_from_form(
    home,
    itinerary_stays,
    departure_dates_by_city,
    arrival_dates_by_city,
    exact_arrival_required,
    flex,
    topn,
):
    """
    Persist the CURRENT visible travel form to the active named trip.

    Streamlit reruns whenever a widget changes, so an already-named trip is
    automatically refreshed after date/city/exact-arrival/Top-N changes.
    Unnamed/new workspaces are intentionally not persisted as named trips.
    """
    skip_db_write = st.session_state.pop("skip_form_autosave_once", False)

    trip_id = st.session_state.get("active_trip_id")
    trip_name = st.session_state.get("active_trip_name")

    try:
        route, legs, ordered_stays = build_route_and_legs_from_stays(
            home,
            departure_dates_by_city.get(home, date.today().isoformat()),
            itinerary_stays,
        )
        defs = generate_ticket_patterns_from_legs(route, legs)
        patterns = [{
            "route": route,
            "legs": legs,
            "kind": kind,
            "tickets": tickets,
            "pattern_id": f"R1-P{i+1}",
        } for i, (kind, tickets) in enumerate(defs)]

        current_draft = {
            "home": home,
            "visits": [s["city"] for s in ordered_stays],
            "dates": dict(departure_dates_by_city),
            "arrival_dates": dict(arrival_dates_by_city),
            "exact_arrival_required": dict(exact_arrival_required),
            "itinerary_stays": [dict(s) for s in ordered_stays],
            "home_final_arrival": arrival_dates_by_city.get(home),
            "flex": int(flex),
            "topn": int(topn),
            "max_revisit": 0,
            "max_extra": 0,
            "patterns": patterns,
        }

        # Always keep the latest visible form in memory, even before the trip
        # has ever been named/saved.
        st.session_state["draft_generated"] = current_draft
        st.session_state.generated = current_draft

        # Skip DB write once immediately after loading, but keep the draft.
        if skip_db_write:
            return

        # Only named trips are automatically persisted.
        if not trip_id or not trip_name or DB_INIT_ERROR:
            return

        save_state("generated", current_draft)
        save_all_offers(valid_offers_only(st.session_state.get("offers", [])))
        save_named_trip(trip_name, trip_id=trip_id)

        st.session_state["last_autosave_at"] = datetime.now().isoformat(timespec="seconds")
        st.session_state["last_autosave_error"] = None
    except Exception as exc:
        st.session_state["last_autosave_error"] = str(exc)

try:
    ensure_trip_table()
except Exception as exc:
    if not DB_INIT_ERROR:
        DB_INIT_ERROR = str(exc)

# -----------------------------
# Session
# -----------------------------
# A fresh browser session must start with an empty workspace.
# Saved trips are restored ONLY when the user explicitly selects "불러오기".
if "offers" not in st.session_state:
    st.session_state.offers = []
if "generated" not in st.session_state:
    st.session_state.generated = None
if "active_trip_id" not in st.session_state:
    st.session_state.active_trip_id = None
if "active_trip_name" not in st.session_state:
    st.session_state.active_trip_name = None

st.title("✈️ Flight Trip Optimizer")
st.caption("Version 3.20.0 · 일정에 없는 역방향 후보 제거")
st.caption("유료 항공 API·Tesseract 없이 사용하는 개인용 여행 항공권 비교 도구")

with st.sidebar:
    st.markdown("### 🧳 여행 데이터")

    active_name = st.session_state.get("active_trip_name")
    if active_name:
        st.success(f"현재 여행: {active_name}")
        st.caption("🔄 자동저장 ON · 여행 조건 변경 시 Cloud DB에 자동 반영")
        last_saved = st.session_state.get("last_autosave_at")
        if last_saved:
            st.caption(f"최근 자동저장: {last_saved.replace('T', ' ')}")
        autosave_error = st.session_state.get("last_autosave_error")
        if autosave_error:
            st.warning(f"자동저장 오류: {autosave_error}")
    else:
        st.info("현재 여행: 새 작업")
        st.caption("여행 이름을 한 번 저장하면 이후 변경사항부터 자동저장됩니다.")

    new_trip_name = st.text_input(
        "현재 작업을 여행으로 저장",
        value=active_name or "",
        placeholder="예: 2026 터키-그리스"
    )

    c_save1, c_save2 = st.columns(2)
    with c_save1:
        if st.button("💾 저장", use_container_width=True):
            name_for_save = (new_trip_name or "").strip()
            if not name_for_save:
                st.error("여행 이름을 입력하세요.")
            else:
                # IMPORTANT:
                # Sidebar is executed before the main travel form.
                # Therefore do NOT save here. Queue the request and let the
                # main form save AFTER all current city/date widgets are read.
                st.session_state["pending_manual_trip_save"] = {
                    "trip_name": name_for_save,
                    "trip_id": st.session_state.get("active_trip_id"),
                }

    with c_save2:
        if st.button("➕ 새 여행", use_container_width=True):
            st.session_state.generated = None
            st.session_state.pop("draft_generated", None)
            st.session_state.pop("pending_manual_trip_save", None)
            st.session_state.offers = []
            st.session_state.active_trip_id = None
            st.session_state.active_trip_name = None
            st.session_state.pop("last_autosave_at", None)
            st.session_state.pop("last_autosave_error", None)
            autosave()
            st.rerun()

    trips = list_named_trips()
    if trips:
        trip_labels = [
            f'{t["trip_name"]}  ·  {str(t["updated_at"])[:16]}'
            for t in trips
        ]
        selected_label = st.selectbox(
            "저장된 여행",
            trip_labels,
            key="saved_trip_selector"
        )
        selected_trip = trips[trip_labels.index(selected_label)]

        if st.button("📂 이 여행 불러오기", use_container_width=True):
            try:
                name = load_named_trip(selected_trip["trip_id"])
                if name:
                    st.success(f"{name} 불러오기 완료")
                    st.rerun()
            except Exception as e:
                st.error(f"불러오기 실패: {e}")

        with st.expander("여행 이름 변경 / 삭제", expanded=False):
            rename_value = st.text_input(
                "새 여행 이름",
                value=selected_trip["trip_name"],
                key="trip_rename_value"
            )
            if st.button("이름 변경", use_container_width=True):
                try:
                    rename_named_trip(selected_trip["trip_id"], rename_value)
                    if st.session_state.get("active_trip_id") == selected_trip["trip_id"]:
                        st.session_state.active_trip_name = rename_value.strip()
                    st.success("이름 변경 완료")
                    st.rerun()
                except Exception as e:
                    st.error(f"이름 변경 실패: {e}")

            confirm_delete = st.checkbox(
                "삭제 확인",
                key="trip_delete_confirm"
            )
            if st.button("🗑️ 여행 삭제", use_container_width=True, disabled=not confirm_delete):
                try:
                    delete_named_trip(selected_trip["trip_id"])
                    if st.session_state.get("active_trip_id") == selected_trip["trip_id"]:
                        st.session_state.active_trip_id = None
                        st.session_state.active_trip_name = None
                    st.success("여행 삭제 완료")
                    st.rerun()
                except Exception as e:
                    st.error(f"삭제 실패: {e}")
    else:
        st.caption("저장된 여행이 없습니다.")

    st.divider()

with st.sidebar:
    st.markdown("### 💾 데이터 저장")

    if DB_INIT_ERROR:
        st.error("DB 연결 실패")
        st.caption(DB_INIT_ERROR)
    elif DB_MODE == "cloud":
        st.success("☁️ Cloud DB 연결됨")
        st.caption("Render/PC/휴대폰에서 같은 앱 주소를 사용하면 동일한 저장 데이터를 봅니다.")
        st.caption("저장소: PostgreSQL (`DATABASE_URL`)")
    else:
        st.success("💻 Local SQLite 자동저장")
        st.caption(f"DB 파일: {SQLITE_PATH.name}")
        st.caption("현재 PC에서만 유지됩니다. Render에서 공유 저장하려면 DATABASE_URL을 설정하세요.")

        backups = sorted(BACKUP_DIR.glob("flight_optimizer_*.db"), reverse=True)
        if backups:
            st.caption(f"최근 로컬 백업: {backups[0].name}")

        if st.button("지금 로컬 DB 백업 만들기"):
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            target = BACKUP_DIR / f"flight_optimizer_manual_{stamp}.db"
            if SQLITE_PATH.exists():
                shutil.copy2(SQLITE_PATH, target)
                st.success(f"백업 완료: {target.name}")
            else:
                st.warning("아직 DB 파일이 없습니다.")

    if not DB_INIT_ERROR:
        st.download_button(
            "현재 여행 데이터 JSON 내보내기",
            data=export_snapshot_json(),
            file_name=f'flight_optimizer_snapshot_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json',
            mime="application/json",
        )

tab1, tab2, tab3 = st.tabs(["1. 여행/경로", "2. 검색결과 입력", "3. 조합/랭킹"])

with tab1:
    st.subheader("여행 조건")
    c1, c2, c3 = st.columns(3)
    with c1:
        home = norm_code(st.text_input("출발지 IATA", st.session_state.get("home_input", "ICN"), key="home_input"))
    with c2:
        visit_text = st.text_input("방문 도시 IATA (쉼표)", st.session_state.get("visit_input", "IST,ATH"), key="visit_input", help="입력 순서는 상관없습니다. 아래 도착일 기준으로 자동 정렬합니다.")
        visits = [norm_code(x) for x in visit_text.split(",") if norm_code(x)]
    with c3:
        flex_options = [0,1,2,3]
        current_flex = int(st.session_state.get("flex_input", 1))
        flex = st.selectbox(
            "날짜 유연성",
            flex_options,
            index=flex_options.index(current_flex) if current_flex in flex_options else 1,
            format_func=lambda x: "정확한 날짜" if x == 0 else f"±{x}일",
            key="flex_input"
        )

    st.caption("공항 코드는 아래처럼 공항/도시명을 함께 표시합니다. 예: ICN (인천/서울), IST (이스탄불), ATH (아테네), JTR (산토리니)")
    with st.expander("✈️ 주요 공항 코드 보기", expanded=False):
        guide_codes = ["ICN","GMP","PUS","CJU","IST","SAW","ATH","JTR","DXB","DOH","AUH","PEK","CAN","NRT","HND","KIX","LHR","CDG","FCO","FRA","AMS","JFK","LAX"]
        guide_rows = [{"코드": c, "공항/도시": AIRPORT_LABELS[c]} for c in guide_codes]
        st.dataframe(pd.DataFrame(guide_rows), use_container_width=True, hide_index=True)

    departure_dates_by_city = {}
    arrival_dates_by_city = {}
    exact_arrival_required = {}
    itinerary_stays = []

    st.markdown("### 🗓️ 도시별 도착 / 출발 일정")
    st.caption(
        "같은 도시를 실제로 다시 방문할 때만 `＋ 날짜 추가`를 누르세요. "
        "프로그램이 임의로 재방문 경로를 만들지는 않습니다."
    )

    default_home_depart = date(2026, 12, 23)
    default_visit_arrivals = [
        date(2026, 12, 24), date(2026, 12, 29),
        date(2027, 1, 3), date(2027, 1, 6),
    ]
    default_visit_departures = [
        date(2026, 12, 28), date(2027, 1, 2),
        date(2027, 1, 5), date(2027, 1, 8),
    ]

    c_home1, c_home2, c_home3 = st.columns([2, 2, 1])
    with c_home1:
        home_depart = st.date_input(
            f"{airport_label(home)} 출발",
            st.session_state.get("home_departure_date", default_home_depart),
            key="home_departure_date"
        )
        departure_dates_by_city[home] = home_depart.isoformat()

    default_final_home_arrival = (
        default_visit_departures[min(max(len(visits)-1, 0), len(default_visit_departures)-1)] + timedelta(days=1)
        if visits else default_home_depart + timedelta(days=1)
    )
    with c_home2:
        home_arrive = st.date_input(
            f"{airport_label(home)} 최종 도착",
            st.session_state.get("home_final_arrival_date", default_final_home_arrival),
            key="home_final_arrival_date"
        )
        arrival_dates_by_city[home] = home_arrive.isoformat()
    with c_home3:
        st.markdown("##### 도착 조건")
        exact_arrival_required[home] = st.checkbox(
            "정확 도착",
            value=bool(st.session_state.get("exact_home_arrival", True)),
            key="exact_home_arrival"
        )

    for i, node in enumerate(visits):
        if f"repeat_count_{node}" not in st.session_state:
            st.session_state[f"repeat_count_{node}"] = 1

        h1, h2 = st.columns([4, 1])
        with h1:
            st.markdown(f"#### {airport_label(node)} 일정")
        with h2:
            if st.button("＋ 날짜 추가", key=f"add_repeat_{node}", use_container_width=True):
                st.session_state[f"repeat_count_{node}"] += 1
                st.rerun()

        repeat_count = int(st.session_state[f"repeat_count_{node}"])
        for n in range(1, repeat_count + 1):
            arr_key, dep_key = f"arr_{node}_{n}", f"dep_{node}_{n}"
            exact_key = f"exact_arrival_{node}_{n}"
            c1, c2, c3, c4 = st.columns([2, 1, 2, 0.7])

            base_idx = min(i, len(default_visit_arrivals)-1)
            arr_default = default_visit_arrivals[base_idx] + timedelta(days=(n-1)*3)
            dep_default = default_visit_departures[base_idx] + timedelta(days=(n-1)*3)

            with c1:
                arr = st.date_input(
                    f"{airport_label(node)} 도착" + (f" #{n}" if repeat_count > 1 else ""),
                    st.session_state.get(arr_key, arr_default),
                    key=arr_key
                )
            with c2:
                st.markdown("##### 도착 조건")
                exact_val = st.checkbox(
                    "정확 도착",
                    value=bool(st.session_state.get(exact_key, True)),
                    key=exact_key
                )
            with c3:
                dep = st.date_input(
                    f"{airport_label(node)} 출발" + (f" #{n}" if repeat_count > 1 else ""),
                    st.session_state.get(dep_key, dep_default),
                    key=dep_key
                )
            with c4:
                st.markdown("##### ")
                if n > 1 and st.button("삭제", key=f"remove_repeat_{node}_{n}"):
                    st.session_state[f"repeat_count_{node}"] -= 1
                    st.session_state.pop(arr_key, None)
                    st.session_state.pop(dep_key, None)
                    st.session_state.pop(exact_key, None)
                    st.rerun()

            if dep < arr:
                st.error(f"{airport_label(node)} 방문 #{n}: 출발일은 도착일보다 빠를 수 없습니다.")

            itinerary_stays.append({
                "city": node,
                "visit_no": n,
                "arrival_date": arr.isoformat(),
                "departure_date": dep.isoformat(),
                "exact_arrival": bool(exact_val),
            })

            if n == 1:
                arrival_dates_by_city[node] = arr.isoformat()
                departure_dates_by_city[node] = dep.isoformat()
                exact_arrival_required[node] = bool(exact_val)

    ordered_stays = sorted(
        itinerary_stays,
        key=lambda s: (s["arrival_date"], s["departure_date"], s["city"], s["visit_no"])
    )
    sorted_visits = [s["city"] for s in ordered_stays]

    if ordered_stays:
        st.info(
            "자동 여행 순서: "
            + " → ".join([airport_label(home)] + [airport_label(s["city"]) for s in ordered_stays] + [airport_label(home)])
        )

    with st.expander("📅 입력한 전체 여행 일정 보기", expanded=True):
        schedule_rows = [{
            "도시": airport_label(home), "방문": "출국", "도착": "-",
            "출발": departure_dates_by_city.get(home, ""), "정확 도착": "-"
        }]
        for s in ordered_stays:
            schedule_rows.append({
                "도시": airport_label(s["city"]),
                "방문": f'#{s["visit_no"]}',
                "도착": s["arrival_date"],
                "출발": s["departure_date"],
                "정확 도착": "✓" if s["exact_arrival"] else "-"
            })
        schedule_rows.append({
            "도시": airport_label(home), "방문": "귀국",
            "도착": arrival_dates_by_city.get(home, ""), "출발": "-",
            "정확 도착": "✓" if exact_arrival_required.get(home, False) else "-"
        })
        st.dataframe(pd.DataFrame(schedule_rows), use_container_width=True, hide_index=True)

    c4, c5 = st.columns(2)
    with c4:
        topn_options = [5,10,20,50]
        current_topn = int(st.session_state.get("topn_input", 10))
        topn = st.selectbox(
            "최종 표시 Top N",
            topn_options,
            index=topn_options.index(current_topn) if current_topn in topn_options else 1,
            key="topn_input"
        )
    with c5:
        st.caption("재방문은 사용자가 `＋ 날짜 추가`로 입력한 경우에만 경로에 포함됩니다.")

    max_revisit = 0
    max_extra = 0

    # Named trips are continuously persisted from the current visible form.
    autosave_active_trip_from_form(
        home=home,
        itinerary_stays=itinerary_stays,
        departure_dates_by_city=departure_dates_by_city,
        arrival_dates_by_city=arrival_dates_by_city,
        exact_arrival_required=exact_arrival_required,
        flex=flex,
        topn=topn,
    )

    # Complete a manual save only AFTER the complete current form has been read
    # and draft_generated has been rebuilt from the visible values.
    pending_manual_save = st.session_state.pop("pending_manual_trip_save", None)
    if pending_manual_save:
        try:
            current_draft = st.session_state.get("draft_generated")
            if not current_draft:
                raise ValueError("현재 여행 일정 snapshot을 만들지 못했습니다.")

            # The draft above was rebuilt from the CURRENT form in this same run.
            # Do not compare it with a differently ordered/raw itinerary list:
            # build_route_and_legs_from_stays() intentionally orders stays by date,
            # so a raw-vs-ordered comparison can falsely reject a valid save.
            # Save the freshly rebuilt current draft directly.
            # Basic integrity: the snapshot must contain every current stay record.
            draft_stays = current_draft.get("itinerary_stays") or []
            if len(draft_stays) != len(itinerary_stays):
                raise ValueError(
                    f"현재 일정 {len(itinerary_stays)}개 중 snapshot에는 "
                    f"{len(draft_stays)}개만 있어 저장을 중단했습니다."
                )

            st.session_state.generated = current_draft
            saved_id = save_named_trip(
                pending_manual_save["trip_name"],
                trip_id=pending_manual_save.get("trip_id"),
            )
            st.session_state.active_trip_id = saved_id
            st.session_state.active_trip_name = pending_manual_save["trip_name"]
            st.session_state["last_autosave_at"] = datetime.now().isoformat(timespec="seconds")
            st.session_state["last_autosave_error"] = None
            st.session_state["manual_save_success_message"] = (
                f'{pending_manual_save["trip_name"]} 저장 완료'
            )
            st.rerun()
        except Exception as exc:
            st.session_state["manual_save_error_message"] = str(exc)
            st.rerun()

    if st.session_state.pop("manual_save_success_message", None):
        st.success("여행 저장 완료")
    manual_save_error = st.session_state.pop("manual_save_error_message", None)
    if manual_save_error:
        st.error(f"저장 실패: {manual_save_error}")


    if st.button("경로·발권 후보 생성", type="primary"):
        route, legs, ordered_stays = build_route_and_legs_from_stays(
            home, departure_dates_by_city[home], itinerary_stays
        )
        defs = generate_ticket_patterns_from_legs(route, legs)
        generated = [{
            "route": route,
            "legs": legs,
            "kind": kind,
            "tickets": tickets,
            "pattern_id": f"R1-P{i+1}"
        } for i, (kind, tickets) in enumerate(defs)]
        routes = [route]

        st.session_state.generated = {
            "home": home,
            "visits": sorted_visits,
            "dates": departure_dates_by_city,
            "arrival_dates": arrival_dates_by_city,
            "exact_arrival_required": exact_arrival_required,
            "itinerary_stays": ordered_stays,
            "home_final_arrival": arrival_dates_by_city.get(home),
            "flex": flex,
            "topn": topn,
            "max_revisit": int(max_revisit),
            "max_extra": int(max_extra),
            "patterns": generated
        }
        autosave()
        st.success(f"Physical route {len(routes)}개 / 발권 패턴 {len(generated)}개 생성 · 자동저장 완료")

    g = st.session_state.generated
    if g:
        st.subheader("생성된 후보")
        rows = []
        for p in g["patterns"]:
            rows.append({
                "ID": p["pattern_id"],
                "Physical Route": route_key(p["route"]),
                "유형": p["kind"],
                "티켓 수": len(p["tickets"]),
                "발권 묶음": " || ".join(format_ticket(p["legs"], t) for t in p["tickets"])
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.markdown("### 검색 링크")
        selected_id = st.selectbox("발권 패턴 선택", [p["pattern_id"] for p in g["patterns"]])
        p = next(x for x in g["patterns"] if x["pattern_id"] == selected_id)

        for ti, idxs in enumerate(p["tickets"], 1):
            ticket_legs = [p["legs"][i] for i in idxs]
            st.markdown(f"**Ticket {ti}: {format_ticket(p['legs'], idxs)}**")
            a, b = st.columns(2)
            with a:
                st.link_button("Google Flights 열기",
                    google_multicity_url(ticket_legs) if len(ticket_legs)>1
                    else google_oneway_url(ticket_legs[0].origin,ticket_legs[0].destination,ticket_legs[0].departure_date))
            with b:
                st.link_button("Skyscanner 열기",
                    skyscanner_multicity_url(ticket_legs) if len(ticket_legs)>1
                    else skyscanner_oneway_url(ticket_legs[0].origin,ticket_legs[0].destination,ticket_legs[0].departure_date))
            st.code(search_key_for_ticket(p["legs"], idxs), language=None)

with tab2:
    st.subheader("검색 결과 입력")
    st.info("v3에서는 Tesseract를 완전히 제거했습니다. Skyscanner/Google Flights 검색 결과에서 보이는 텍스트를 복사해서 붙여넣으면 됩니다. 캡처는 참고용으로 같이 올릴 수 있지만 OCR은 하지 않습니다.")

    if not st.session_state.generated:
        st.warning("먼저 1번 탭에서 경로를 생성하세요.")
    else:
        # Defensive rebuild: old saved trips may contain stale revisit patterns.
        st.session_state.generated = rebuild_generated_from_saved_state(st.session_state.generated)
        patterns = st.session_state.generated["patterns"]
        search_tasks = collect_unique_search_tasks(patterns)

        saved_counts = {}
        for oo in valid_offers_only(st.session_state.offers):
            saved_counts[oo["search_key"]] = saved_counts.get(oo["search_key"], 0) + 1

        # Master checklist: this is the important "what do I search?" view.
        st.markdown("### ✅ 전체 검색 체크리스트")
        checklist_rows = []
        for n, task in enumerate(search_tasks, 1):
            cnt = saved_counts.get(task["key"], 0)
            legs_for_task = task["legs"]
            idxs_for_task = task["idxs"]

            if task["type"] == "왕복":
                outbound = legs_for_task[idxs_for_task[0]]
                inbound = legs_for_task[idxs_for_task[1]]
                dates = f"가는 날 {outbound.departure_date} / 오는 날 {inbound.departure_date}"
            elif task["type"] == "편도":
                leg = legs_for_task[idxs_for_task[0]]
                dates = leg.departure_date
            else:
                dates = " / ".join(legs_for_task[i].departure_date for i in idxs_for_task)

            checklist_rows.append({
                "번호": n,
                "상태": "✓ 입력 완료" if cnt else "○ 검색 필요",
                "검색 방식": task["type"],
                "검색할 항공권": task["title"],
                "날짜": dates,
                "구간 상세": task["summary"],
                "저장 후보": cnt,
            })

        st.dataframe(
            pd.DataFrame(checklist_rows),
            use_container_width=True,
            hide_index=True
        )

        done_count = sum(1 for task in search_tasks if saved_counts.get(task["key"], 0) > 0)
        st.progress(done_count / len(search_tasks) if search_tasks else 0)
        st.caption(f"전체 진행상황: {done_count}/{len(search_tasks)}개 검색 완료")

        # Select only among ticket searches that do not yet have valid saved offers.
        # Completed checklist items disappear from this selector automatically.
        # Keep the ORIGINAL checklist number even after completed items disappear.
        numbered_search_tasks = [
            (original_no, task)
            for original_no, task in enumerate(search_tasks, 1)
        ]

        pending_search_tasks = [
            (original_no, task)
            for original_no, task in numbered_search_tasks
            if saved_counts.get(task["key"], 0) == 0
        ]

        if not pending_search_tasks:
            st.success("체크리스트의 모든 항공권 입력이 완료되었습니다.")
            st.stop()

        task_labels = [
            f'{original_no}. [{t["type"]}] {t["title"]} — {t["summary"]}'
            for original_no, t in pending_search_tasks
        ]

        # Reset selector if its previous value belonged to a task that just became completed.
        current_selector = st.session_state.get("search_task_selector")
        if current_selector not in task_labels:
            st.session_state["search_task_selector"] = task_labels[0]

        selected_task_label = st.selectbox(
            "지금 검색/입력할 항공권 선택",
            task_labels,
            key="search_task_selector"
        )
        selected_original_no, selected_task = pending_search_tasks[
            task_labels.index(selected_task_label)
        ]

        p2_legs = selected_task["legs"]
        idxs = selected_task["idxs"]
        skey = selected_task["key"]
        search_rows = selected_task["rows"]
        search_summary = selected_task["summary"]

        st.markdown("### 🔎 지금 검색해야 할 항공권")
        st.warning(
            f'**{selected_task["type"]} 검색**\n\n'
            f'**{selected_task["title"]}**\n\n'
            + search_summary
        )

        st.dataframe(
            pd.DataFrame(search_rows),
            use_container_width=True,
            hide_index=True
        )

        if selected_task["type"] == "편도":
            st.caption("Skyscanner / Google Flights에서 편도(One-way)로 검색하세요.")
        elif selected_task["type"] == "왕복":
            a = p2_legs[idxs[0]]
            b = p2_legs[idxs[1]]
            st.caption(
                f"Skyscanner / Google Flights에서 왕복(Round-trip)으로 검색하세요. "
                f"가는 날 {a.departure_date} / 오는 날 {b.departure_date}"
            )
        else:
            st.caption("Skyscanner / Google Flights에서 다구간(Multi-city)으로 검색하세요.")

        selected_ticket_legs = [p2_legs[i] for i in idxs]
        c_search1, c_search2 = st.columns(2)
        with c_search1:
            st.link_button(
                "Google Flights에서 이 티켓 검색",
                google_multicity_url(selected_ticket_legs)
                if len(selected_ticket_legs) > 1
                else google_oneway_url(
                    selected_ticket_legs[0].origin,
                    selected_ticket_legs[0].destination,
                    selected_ticket_legs[0].departure_date
                ),
                use_container_width=True
            )
        with c_search2:
            st.link_button(
                "Skyscanner에서 이 티켓 검색",
                skyscanner_multicity_url(selected_ticket_legs)
                if len(selected_ticket_legs) > 1
                else skyscanner_oneway_url(
                    selected_ticket_legs[0].origin,
                    selected_ticket_legs[0].destination,
                    selected_ticket_legs[0].departure_date
                ),
                use_container_width=True
            )

        # Keep the input area compact. Detailed guidance is available on demand.
        source_url = ""

        with st.expander("ⓘ 붙여넣을 정보 보기", expanded=False):
            st.markdown("""
**검색 결과에서 아래 정보가 보이도록 복사해서 붙여넣으세요.**

**필수**
- 항공사
- 출발시간
- 도착시간
- 도착일 표시: 같은 날 / `+1` / `+2`
- 총 소요시간
- 가격
- 직항 또는 경유
- 경유 시 경유 공항

**있으면 함께 입력**
- 수하물 포함 여부
- 수하물 중량
- 추가 수하물 비용

**예시**
```text
Emirates
오후 11:40 → 오후 2:25 +1
20시간 45분
1회 경유 DXB
₩823,600
```

여러 항공편을 한꺼번에 붙여넣어도 됩니다.
추출 후 아래 표에서 항공사·시간·가격·수하물 정보를 직접 수정할 수 있습니다.
""")

        with st.expander("기술 정보 (평소에는 볼 필요 없음)", expanded=False):
            st.code(skey, language=None)

        # Streamlit widget state must be cleared BEFORE the widget is instantiated.
        if st.session_state.pop("clear_paste_after_save", False):
            st.session_state["search_result_paste_text"] = ""
            st.session_state.pop("parsed_rows", None)
            st.session_state.pop("parsed_search_key", None)

        manual_text = st.text_area(
            "검색 결과 텍스트 붙여넣기  ⓘ",
            height=260,
            key="search_result_paste_text",
            placeholder="""예:
Emirates
오후 11:40 → 오후 2:25 +1
20시간 45분
1회 경유 DXB
₩823,600

터키항공
오전 12:05 → 오전 5:55
11시간 50분
직항
₩840,200"""
        )

        if st.button("가격·시간 후보 추출"):
            rows = parse_result_text(manual_text, skey, source_url)
            st.session_state["parsed_rows"] = rows
            if not rows:
                st.warning("자동 추출 후보가 없습니다. 아래 표에 직접 입력해도 됩니다.")

        rows = st.session_state.get("parsed_rows", [])
        if rows:
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame([{
                "선택": True, "search_key": skey, "항공사": "", "출발시간": "", "도착시간": "",
                "도착+일": 0, "소요시간(분)": 0, "경유": "", "가격(KRW)": 0,
                "수하물": "", "추가수하물kg": 0, "추가수하물가격": 0, "수하물포함체크": False,
                "source_url": source_url, "원문": ""
            }])

        edited = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "선택": st.column_config.CheckboxColumn(),
                "가격(KRW)": st.column_config.NumberColumn(format="%d"),
                "추가수하물가격": st.column_config.NumberColumn(format="%d"),
                "수하물포함체크": st.column_config.CheckboxColumn("수하물 비용 합산"),
            },
            key="offer_editor_v3"
        )

        if st.button("선택 항공편 저장", type="primary"):
            saved = 0
            invalid_skipped = 0

            for _, r in edited.iterrows():
                if not bool(r.get("선택", True)):
                    continue

                candidate = {
                    "search_key": str(r["search_key"]),
                    "airline": str(r.get("항공사","")),
                    "departure_time": str(r.get("출발시간","")),
                    "arrival_time": str(r.get("도착시간","")),
                    "arrival_day_offset": int(r.get("도착+일",0) or 0),
                    "duration_min": int(r.get("소요시간(분)",0) or 0),
                    "stops": str(r.get("경유","")),
                    "price_krw": int(r.get("가격(KRW)",0) or 0),
                    "baggage_note": str(r.get("수하물","")),
                    "baggage_extra_kg": int(r.get("추가수하물kg",0) or 0),
                    "baggage_extra_price": int(r.get("추가수하물가격",0) or 0),
                    "include_baggage": bool(r.get("수하물포함체크",False)),
                    "source_url": str(r.get("source_url","")),
                }

                if not is_valid_offer(candidate):
                    invalid_skipped += 1
                    continue

                st.session_state.offers.append(candidate)
                saved += 1

            st.session_state.offers = valid_offers_only(st.session_state.offers)
            autosave()

            if saved:
                # Do not modify the text_area key in the same run after instantiation.
                # Mark it for clearing, then rerun; the next run clears it before widget creation.
                st.session_state["clear_paste_after_save"] = True
                st.success(f"{saved}개 유효 항공편 저장 완료")
                st.rerun()
            if invalid_skipped:
                st.warning(
                    f"{invalid_skipped}개 행은 출발시간/도착시간/가격이 없어 저장하지 않았습니다."
                )

    st.markdown("### 저장된 항공편")

    legacy_invalid_count = len(st.session_state.offers) - len(valid_offers_only(st.session_state.offers))
    if legacy_invalid_count > 0:
        st.session_state.offers = valid_offers_only(st.session_state.offers)
        autosave()
        st.info(f"기존 빈 데이터 {legacy_invalid_count}개를 자동 정리했습니다.")

    if st.session_state.offers:
        odf = pd.DataFrame(st.session_state.offers)
        odf["적용가격"] = odf["price_krw"] + odf.apply(
            lambda x: x["baggage_extra_price"] if x["include_baggage"] else 0, axis=1)
        edited_offers = st.data_editor(
            odf, use_container_width=True,
            column_config={"include_baggage": st.column_config.CheckboxColumn("수하물 합산")},
            key="saved_offer_editor_v3"
        )
        if st.button("수하물 체크/수정 반영"):
            st.session_state.offers = edited_offers.drop(columns=["적용가격"], errors="ignore").to_dict("records")
            autosave()
            st.rerun()
        if st.button("저장 항공편 전체 삭제"):
            st.session_state.offers = []
            autosave()
            st.rerun()
    else:
        st.caption("아직 저장된 항공편이 없습니다.")


def _expected_arrival_date_for_city(generated_state, city):
    if not generated_state:
        return None

    required_map = generated_state.get("exact_arrival_required", {})
    # Backward compatibility: older saved trips did not have this map.
    # Treat them as not enforced until the user checks the box and regenerates.
    if not required_map.get(city, False):
        return None

    home = generated_state.get("home")
    if city == home:
        return generated_state.get("home_final_arrival") or generated_state.get("arrival_dates", {}).get(home)
    return generated_state.get("arrival_dates", {}).get(city)

def _actual_ticket_final_arrival_date(legs, ticket_idxs, offer):
    """
    Calculate the ticket's final arrival calendar date from:
      final slice departure date + arrival_day_offset.

    This is exact for one-way ticket records and for any saved ticket record whose
    displayed arrival/+N corresponds to the ticket's final slice.
    """
    if not ticket_idxs:
        return None
    final_leg = legs[ticket_idxs[-1]]
    try:
        dep_date = datetime.strptime(final_leg.departure_date, "%Y-%m-%d").date()
        offset = int(offer.get("arrival_day_offset", 0) or 0)
        return (dep_date + timedelta(days=offset)).isoformat()
    except Exception:
        return None

def combo_matches_exact_arrival_dates(pattern, combo, generated_state):
    reasons = []
    stays = generated_state.get("itinerary_stays") or []
    home = generated_state.get("home")
    home_exact = (generated_state.get("exact_arrival_required") or {}).get(home, False)

    expected_by_leg = {}
    for i, stay in enumerate(stays):
        if stay.get("exact_arrival"):
            expected_by_leg[i] = stay.get("arrival_date")

    if home_exact and pattern.get("legs"):
        expected_by_leg[len(pattern["legs"]) - 1] = (
            generated_state.get("home_final_arrival")
            or (generated_state.get("arrival_dates") or {}).get(home)
        )

    for ticket_idxs, offer in zip(pattern["tickets"], combo):
        if not ticket_idxs:
            continue
        final_idx = ticket_idxs[-1]
        expected = expected_by_leg.get(final_idx)
        if not expected:
            continue
        actual = _actual_ticket_final_arrival_date(pattern["legs"], ticket_idxs, offer)
        if not actual:
            return False, ["실제 도착일 확인 불가"]
        if actual != expected:
            dest = pattern["legs"][final_idx].destination
            return False, [f"{airport_label(dest)} 도착일 불일치: 실제 {actual} / 계획 {expected}"]

    return True, reasons


with tab3:
    st.subheader("전체 발권 조합 및 가격 Ranking")

    if not st.session_state.generated or not st.session_state.offers:
        st.warning("경로 생성 후 비교하려는 Ticket 검색 결과를 최소 1개 이상 저장해야 합니다.")
    else:
        st.markdown("### 🧳 수하물 옵션 반영")
        st.caption(
            "위탁수하물이 미포함인 항공권은 추가할 수하물 중량과 비용을 입력하고 "
            "`총액에 포함`을 체크하세요. 반영 후 전체 총액과 Top N 순위가 다시 계산됩니다."
        )

        baggage_rows = []
        valid_current_offers = valid_offers_only(st.session_state.offers)
        for idx, o in enumerate(valid_current_offers):
            baggage_rows.append({
                "offer_uid": offer_uid(o, idx),
                "항공사": o.get("airline", ""),
                "검색 티켓": o.get("search_key", ""),
                "출발": o.get("departure_time", ""),
                "도착": o.get("arrival_time", ""),
                "기본가격(KRW)": int(o.get("price_krw", 0) or 0),
                "수하물 상태": o.get("baggage_note", "") or "확인 필요",
                "추가수하물(kg)": int(o.get("baggage_extra_kg", 0) or 0),
                "추가수하물비용(KRW)": int(o.get("baggage_extra_price", 0) or 0),
                "총액에 포함": bool(o.get("include_baggage", False)),
            })

        baggage_df = pd.DataFrame(baggage_rows)

        edited_baggage = st.data_editor(
            baggage_df,
            use_container_width=True,
            hide_index=True,
            disabled=["offer_uid", "항공사", "검색 티켓", "출발", "도착", "기본가격(KRW)"],
            column_config={
                "offer_uid": None,
                "기본가격(KRW)": st.column_config.NumberColumn(format="%d"),
                "추가수하물(kg)": st.column_config.NumberColumn(min_value=0, step=1),
                "추가수하물비용(KRW)": st.column_config.NumberColumn(min_value=0, step=1000, format="%d"),
                "총액에 포함": st.column_config.CheckboxColumn(
                    "총액에 포함",
                    help="체크하면 추가 수하물 비용이 전체 조합 총액과 순위에 반영됩니다."
                ),
            },
            key="result_baggage_editor_v37"
        )

        if st.button("수하물 반영 → 총액/순위 자동 재계산", type="primary"):
            uid_to_row = {str(r["offer_uid"]): r for _, r in edited_baggage.iterrows()}
            updated = []
            for idx, o in enumerate(valid_current_offers):
                uid = offer_uid(o, idx)
                r = uid_to_row.get(uid)
                if r is not None:
                    o = dict(o)
                    o["baggage_note"] = str(r.get("수하물 상태", "") or "")
                    o["baggage_extra_kg"] = int(r.get("추가수하물(kg)", 0) or 0)
                    o["baggage_extra_price"] = int(r.get("추가수하물비용(KRW)", 0) or 0)
                    o["include_baggage"] = bool(r.get("총액에 포함", False))
                updated.append(o)
            st.session_state.offers = updated
            autosave()
            st.success("수하물 정보 반영 완료. 총액과 순위를 다시 계산했습니다.")
            st.rerun()

        offer_map = {}
        for idx, o in enumerate(valid_offers_only(st.session_state.offers)):
            oo = dict(o)
            oo["_uid"] = offer_uid(o, idx)
            offer_map.setdefault(oo["search_key"], []).append(oo)

        plans = []
        rejected_exact_arrival = []
        for p in st.session_state.generated["patterns"]:
            ticket_keys = [search_key_for_ticket(p["legs"], idxs) for idxs in p["tickets"]]
            if not all(k in offer_map and offer_map[k] for k in ticket_keys):
                continue

            pools = [offer_map[k] for k in ticket_keys]
            pools = [
                sorted(
                    pool,
                    key=lambda x: (
                        int(x.get("price_krw", 0) or 0)
                        + (int(x.get("baggage_extra_price", 0) or 0) if x.get("include_baggage") else 0)
                    )
                )[:10]
                for pool in pools
            ]

            for combo in itertools.product(*pools):
                exact_ok, exact_reasons = combo_matches_exact_arrival_dates(
                    p, combo, st.session_state.generated
                )
                if not exact_ok:
                    rejected_exact_arrival.append({
                        "패턴ID": p["pattern_id"],
                        "이유": " / ".join(exact_reasons),
                    })
                    continue

                base_total = sum(int(x.get("price_krw", 0) or 0) for x in combo)
                baggage_total = sum(
                    int(x.get("baggage_extra_price", 0) or 0) if x.get("include_baggage") else 0
                    for x in combo
                )
                adjusted_total = base_total + baggage_total
                total_duration = sum(int(x.get("duration_min", 0) or 0) for x in combo)

                baggage_summary_parts = []
                for x in combo:
                    note = x.get("baggage_note", "") or "확인 필요"
                    if x.get("include_baggage"):
                        kg = int(x.get("baggage_extra_kg", 0) or 0)
                        fee = int(x.get("baggage_extra_price", 0) or 0)
                        baggage_summary_parts.append(
                            f'{x.get("airline","") or "항공권"}: {note} / +{kg}kg +₩{fee:,}'
                        )
                    else:
                        baggage_summary_parts.append(
                            f'{x.get("airline","") or "항공권"}: {note}'
                        )

                plans.append({
                    "패턴ID": p["pattern_id"],
                    "Physical Route": route_key(p["route"]),
                    "발권유형": p["kind"],
                    "티켓수": len(combo),
                    "기본총액(KRW)": base_total,
                    "수하물추가비용(KRW)": baggage_total,
                    "적용총액(KRW)": adjusted_total,
                    "총표시소요시간(분)": total_duration,
                    "수하물": " || ".join(baggage_summary_parts),
                    "항공편": " || ".join(
                        f'{x.get("airline","")} {x.get("departure_time","")}→{x.get("arrival_time","")} {x.get("stops","")}'
                        for x in combo
                    ),
                    "검색링크": " || ".join(
                        x.get("source_url","") for x in combo if x.get("source_url","")
                    ),
                })

        if rejected_exact_arrival:
            st.info(
                f"📅 체크된 `정확 도착` 조건으로 {len(rejected_exact_arrival)}개 조합을 자동 제외했습니다."
            )
            with st.expander("도착일 불일치로 제외된 조합 보기", expanded=False):
                st.dataframe(
                    pd.DataFrame(rejected_exact_arrival).drop_duplicates(),
                    use_container_width=True,
                    hide_index=True
                )

        if plans:
            rdf = pd.DataFrame(plans)

            base_order = rdf.sort_values(["기본총액(KRW)", "총표시소요시간(분)"]).index.tolist()
            base_rank_map = {idx: rank for rank, idx in enumerate(base_order, 1)}

            rdf = rdf.sort_values(["적용총액(KRW)", "총표시소요시간(분)"])
            rdf["기본순위"] = [base_rank_map[idx] for idx in rdf.index]
            rdf = rdf.reset_index(drop=True)
            rdf.insert(0, "현재순위", range(1, len(rdf) + 1))
            rdf["순위변동"] = rdf.apply(
                lambda r: int(r["기본순위"]) - int(r["현재순위"]), axis=1
            )
            rdf["순위변동표시"] = rdf["순위변동"].apply(
                lambda x: f"↑{x}" if x > 0 else (f"↓{abs(x)}" if x < 0 else "-")
            )

            display_cols = [
                "현재순위", "기본순위", "순위변동표시",
                "패턴ID", "Physical Route", "발권유형", "티켓수",
                "기본총액(KRW)", "수하물추가비용(KRW)", "적용총액(KRW)",
                "수하물", "항공편", "총표시소요시간(분)", "검색링크",
            ]

            topn = st.session_state.generated["topn"]

            st.markdown("### 🏆 현재 Top 결과")
            st.caption(
                "`현재순위`는 수하물 옵션을 반영한 적용총액 기준이며, "
                "`기본순위`는 수하물 추가비용 반영 전 순위입니다."
            )

            st.dataframe(
                rdf[display_cols].head(topn),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "기본총액(KRW)": st.column_config.NumberColumn(format="%d"),
                    "수하물추가비용(KRW)": st.column_config.NumberColumn(format="%d"),
                    "적용총액(KRW)": st.column_config.NumberColumn(format="%d"),
                }
            )

            st.markdown("### 🔍 Top 결과 상세")
            top_options = []
            for _, row in rdf.head(topn).iterrows():
                top_options.append(
                    f'{int(row["현재순위"])}위 | ₩{int(row["적용총액(KRW)"]):,} | '
                    f'{row["Physical Route"]} | {row["패턴ID"]}'
                )

            selected_plan_label = st.selectbox(
                "상세 확인할 조합",
                top_options,
                key="top_plan_detail_v37"
            )
            selected_pos = top_options.index(selected_plan_label)
            selected_row = rdf.head(topn).iloc[selected_pos]

            c1, c2, c3 = st.columns(3)
            c1.metric("기본 총액", f'₩{int(selected_row["기본총액(KRW)"]):,}')
            c2.metric("수하물 추가", f'₩{int(selected_row["수하물추가비용(KRW)"]):,}')
            c3.metric("적용 총액", f'₩{int(selected_row["적용총액(KRW)"]):,}')

            st.write("**수하물 상태**")
            st.write(selected_row["수하물"])
            st.write("**항공편**")
            st.write(selected_row["항공편"])

            csv_df = rdf[display_cols].head(topn)
            st.download_button(
                "Top 결과 CSV 다운로드",
                data=csv_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="flight_top_results_baggage_adjusted.csv",
                mime="text/csv"
            )
        else:
            st.info(
                "현재 입력한 Ticket 가격만으로 완성 가능한 발권 조합이 없습니다. "
                "비교하려는 검색 후보만 추가로 입력하면 됩니다."
            )

st.divider()
st.caption("검색 가격은 변동될 수 있습니다. 최종 예약 전 Skyscanner/Google Flights/판매처에서 반드시 재확인하세요.")
st.caption("DATABASE_URL이 있으면 Cloud PostgreSQL에 저장되고, 없으면 로컬 data/flight_optimizer.db에 저장됩니다.")
