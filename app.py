
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
from dataclasses import dataclass
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

def generate_physical_routes(home: str, visits: List[str], max_revisit_per_city=1, max_extra_legs=3):
    base = [home] + visits
    prev = visits[:-1]
    routes = set()
    routes.add(tuple(base + [home]))

    max_r = min(max_extra_legs, len(prev) * max_revisit_per_city)
    for r in range(1, max_r + 1):
        for seq in itertools.product(prev, repeat=r):
            ok = True
            counts = {}
            last = visits[-1]
            for city in seq:
                counts[city] = counts.get(city, 0) + 1
                if counts[city] > max_revisit_per_city or city == last:
                    ok = False
                    break
                last = city
            if ok:
                routes.add(tuple(base + list(seq) + [home]))

    return [list(r) for r in sorted(routes, key=lambda x: (len(x), x))]

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

def build_legs_for_route(route: List[str], departure_dates_by_city: dict):
    """
    Build dated physical legs.

    Rule:
    - During the required visit sequence, use each city's user-entered departure date.
    - After the final required visit city has been reached, revisit/return legs use
      the final visit city's departure date as the search anchor.

    Example:
      ICN 12/24 -> IST 12/28 -> ATH 01/02 -> IST -> ICN
    becomes
      ICN->IST 12/24
      IST->ATH 12/28
      ATH->IST 01/02
      IST->ICN 01/02

    Actual feasible connection time is checked later from the selected flight times.
    """
    legs = []
    nodes_with_dates = list(departure_dates_by_city.keys())
    final_required_city = nodes_with_dates[-1]
    final_departure_date = departure_dates_by_city[final_required_city]

    final_required_index = None
    for i, city in enumerate(route):
        if city == final_required_city:
            final_required_index = i
            break

    for i, (a, b) in enumerate(zip(route[:-1], route[1:])):
        if final_required_index is not None and i >= final_required_index:
            dep = final_departure_date
        else:
            dep = departure_dates_by_city.get(a, final_departure_date)
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
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value

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
    payload = json.dumps(_jsonable(value), ensure_ascii=False)
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
                    "payload": json.dumps(_jsonable(offer), ensure_ascii=False),
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
        "generated": _jsonable(st.session_state.get("generated")),
        "offers": _jsonable(st.session_state.get("offers", [])),
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")

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
    Create a named-trip table in either PostgreSQL or SQLite.
    Stores a complete snapshot of current generated state + saved offers.
    """
    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS trip_projects (
                        trip_id TEXT PRIMARY KEY,
                        trip_name TEXT NOT NULL UNIQUE,
                        snapshot_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
            conn.commit()
    else:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trip_projects (
                    trip_id TEXT PRIMARY KEY,
                    trip_name TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

def _make_trip_id():
    return datetime.now().strftime("trip_%Y%m%d_%H%M%S_%f")

def current_trip_snapshot():
    return {
        "generated": serialize_generated(st.session_state.get("generated")),
        "offers": st.session_state.get("offers", []),
    }

def save_named_trip(trip_name, trip_id=None):
    trip_name = (trip_name or "").strip()
    if not trip_name:
        raise ValueError("여행 이름을 입력하세요.")

    snapshot = json.dumps(current_trip_snapshot(), ensure_ascii=False, default=str)
    now = datetime.now().isoformat(timespec="seconds")
    if not trip_id:
        trip_id = _make_trip_id()

    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO trip_projects(trip_id, trip_name, snapshot_json, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (trip_id) DO UPDATE SET
                        trip_name = EXCLUDED.trip_name,
                        snapshot_json = EXCLUDED.snapshot_json,
                        updated_at = EXCLUDED.updated_at
                """, (trip_id, trip_name, snapshot, now, now))
            conn.commit()
    else:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO trip_projects(trip_id, trip_name, snapshot_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trip_id) DO UPDATE SET
                    trip_name=excluded.trip_name,
                    snapshot_json=excluded.snapshot_json,
                    updated_at=excluded.updated_at
            """, (trip_id, trip_name, snapshot, now, now))
            conn.commit()
    return trip_id

def list_named_trips():
    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT trip_id, trip_name, created_at, updated_at
                    FROM trip_projects
                    ORDER BY updated_at DESC
                """)
                rows = cur.fetchall()
        return [
            {"trip_id": r[0], "trip_name": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]
    else:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT trip_id, trip_name, created_at, updated_at
                FROM trip_projects
                ORDER BY updated_at DESC
            """).fetchall()
        return [
            {
                "trip_id": r["trip_id"],
                "trip_name": r["trip_name"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

def load_named_trip(trip_id):
    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT trip_name, snapshot_json FROM trip_projects WHERE trip_id=%s",
                    (trip_id,)
                )
                row = cur.fetchone()
        if not row:
            return None
        trip_name, payload = row
    else:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT trip_name, snapshot_json FROM trip_projects WHERE trip_id=?",
                (trip_id,)
            ).fetchone()
        if not row:
            return None
        trip_name, payload = row["trip_name"], row["snapshot_json"]

    data = json.loads(payload)
    generated = deserialize_generated(data.get("generated"))
    offers = data.get("offers", [])

    st.session_state.generated = generated
    st.session_state.offers = offers
    st.session_state.active_trip_id = trip_id
    st.session_state.active_trip_name = trip_name

    # Also update current autosave state.
    autosave()
    return trip_name

def rename_named_trip(trip_id, new_name):
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("새 이름을 입력하세요.")
    now = datetime.now().isoformat(timespec="seconds")

    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trip_projects SET trip_name=%s, updated_at=%s WHERE trip_id=%s",
                    (new_name, now, trip_id)
                )
            conn.commit()
    else:
        with db_connect() as conn:
            conn.execute(
                "UPDATE trip_projects SET trip_name=?, updated_at=? WHERE trip_id=?",
                (new_name, now, trip_id)
            )
            conn.commit()

def delete_named_trip(trip_id):
    if CLOUD_DB:
        with pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM trip_projects WHERE trip_id=%s", (trip_id,))
            conn.commit()
    else:
        with db_connect() as conn:
            conn.execute("DELETE FROM trip_projects WHERE trip_id=?", (trip_id,))
            conn.commit()

def save_active_trip_if_any():
    trip_id = st.session_state.get("active_trip_id")
    trip_name = st.session_state.get("active_trip_name")
    if trip_id and trip_name:
        save_named_trip(trip_name, trip_id=trip_id)

ensure_trip_table()

# -----------------------------
# Session
# -----------------------------
if "offers" not in st.session_state:
    st.session_state.offers = [] if DB_INIT_ERROR else load_all_offers()
if "generated" not in st.session_state:
    st.session_state.generated = None if DB_INIT_ERROR else load_state("generated", None)

if "active_trip_id" not in st.session_state:
    st.session_state.active_trip_id = None
if "active_trip_name" not in st.session_state:
    st.session_state.active_trip_name = None

st.title("✈️ Flight Trip Optimizer")
st.caption("유료 항공 API·Tesseract 없이 사용하는 개인용 여행 항공권 비교 도구")

with st.sidebar:
    st.markdown("### 🧳 여행 데이터")

    active_name = st.session_state.get("active_trip_name")
    if active_name:
        st.success(f"현재 여행: {active_name}")
    else:
        st.info("현재 여행: 이름 없음")

    new_trip_name = st.text_input(
        "현재 작업을 여행으로 저장",
        value=active_name or "",
        placeholder="예: 2026 터키-그리스"
    )

    c_save1, c_save2 = st.columns(2)
    with c_save1:
        if st.button("💾 저장", use_container_width=True):
            try:
                current_id = st.session_state.get("active_trip_id")
                saved_id = save_named_trip(new_trip_name, trip_id=current_id)
                st.session_state.active_trip_id = saved_id
                st.session_state.active_trip_name = new_trip_name.strip()
                st.success("여행 저장 완료")
                st.rerun()
            except Exception as e:
                st.error(f"저장 실패: {e}")

    with c_save2:
        if st.button("➕ 새 여행", use_container_width=True):
            st.session_state.generated = None
            st.session_state.offers = []
            st.session_state.active_trip_id = None
            st.session_state.active_trip_name = None
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
        home = norm_code(st.text_input("출발지 IATA", "ICN"))
    with c2:
        visit_text = st.text_input("방문 도시 IATA (순서대로, 쉼표)", "IST,ATH")
        visits = [norm_code(x) for x in visit_text.split(",") if norm_code(x)]
    with c3:
        flex = st.selectbox("날짜 유연성", [0,1,2,3], index=1,
                            format_func=lambda x: "정확한 날짜" if x == 0 else f"±{x}일")

    st.caption("공항 코드는 아래처럼 공항/도시명을 함께 표시합니다. 예: ICN (인천/서울), IST (이스탄불), ATH (아테네), JTR (산토리니)")
    with st.expander("✈️ 주요 공항 코드 보기", expanded=False):
        guide_codes = ["ICN","GMP","PUS","CJU","IST","SAW","ATH","JTR","DXB","DOH","AUH","PEK","CAN","NRT","HND","KIX","LHR","CDG","FCO","FRA","AMS","JFK","LAX"]
        guide_rows = [{"코드": c, "공항/도시": AIRPORT_LABELS[c]} for c in guide_codes]
        st.dataframe(pd.DataFrame(guide_rows), use_container_width=True, hide_index=True)

    departure_dates_by_city = {}
    arrival_dates_by_city = {}

    st.markdown("### 🗓️ 도시별 도착 / 출발 일정")
    st.caption(
        "각 방문 도시의 도착일과 출발일을 입력하세요. "
        "항공권 검색 날짜는 출발일을 기준으로 생성하고, 도착일은 체류 일정 확인에 사용합니다."
    )

    default_home_depart = date(2026, 12, 23)
    default_visit_arrivals = [
        date(2026, 12, 24),
        date(2026, 12, 29),
        date(2027, 1, 3),
        date(2027, 1, 6),
    ]
    default_visit_departures = [
        date(2026, 12, 28),
        date(2027, 1, 2),
        date(2027, 1, 5),
        date(2027, 1, 8),
    ]

    c_home1, c_home2 = st.columns(2)
    with c_home1:
        home_depart = st.date_input(
            f"{airport_label(home)} 출발",
            default_home_depart,
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
            default_final_home_arrival,
            key="home_final_arrival_date"
        )
        arrival_dates_by_city[home] = home_arrive.isoformat()

    if visits:
        for i, node in enumerate(visits):
            c_arr, c_dep = st.columns(2)
            with c_arr:
                arr_default = default_visit_arrivals[min(i, len(default_visit_arrivals)-1)]
                arr = st.date_input(
                    f"{airport_label(node)} 도착",
                    arr_default,
                    key=f"arr_{node}_{i}"
                )
                arrival_dates_by_city[node] = arr.isoformat()

            with c_dep:
                dep_default = default_visit_departures[min(i, len(default_visit_departures)-1)]
                dep = st.date_input(
                    f"{airport_label(node)} 출발",
                    dep_default,
                    key=f"dep_{node}_{i}"
                )
                departure_dates_by_city[node] = dep.isoformat()

            if dep < arr:
                st.error(f"{airport_label(node)} 출발일은 도착일보다 빠를 수 없습니다.")

    c4, c5, c6 = st.columns(3)
    with c4:
        max_revisit = st.number_input("도시별 최대 재방문", 0, 2, 1)
    with c5:
        max_extra = st.number_input("추가 이동 최대 leg", 0, 6, max(1, len(visits)-1))
    with c6:
        topn = st.selectbox("최종 표시 Top N", [5,10,20,50], index=1)

    if visits:
        schedule_rows = [{
            "도시": airport_label(home),
            "도착": "-",
            "출발": departure_dates_by_city.get(home, "")
        }]
        for city in visits:
            schedule_rows.append({
                "도시": airport_label(city),
                "도착": arrival_dates_by_city.get(city, ""),
                "출발": departure_dates_by_city.get(city, "")
            })
        schedule_rows.append({
            "도시": airport_label(home),
            "도착": arrival_dates_by_city.get(home, ""),
            "출발": "-"
        })
        with st.expander("📅 입력한 전체 여행 일정 보기", expanded=True):
            st.dataframe(pd.DataFrame(schedule_rows), use_container_width=True, hide_index=True)

    if st.button("경로·발권 후보 생성", type="primary"):
        routes = generate_physical_routes(home, visits, int(max_revisit), int(max_extra))
        generated = []
        for ridx, route in enumerate(routes, 1):
            legs, patterns = generate_ticket_patterns(route, departure_dates_by_city)
            for pidx, (kind, tickets) in enumerate(patterns, 1):
                generated.append({
                    "route": route,
                    "legs": legs,
                    "kind": kind,
                    "tickets": tickets,
                    "pattern_id": f"R{ridx}-P{pidx}"
                })
        st.session_state.generated = {
            "home": home,
            "visits": visits,
            "dates": departure_dates_by_city,
            "arrival_dates": arrival_dates_by_city,
            "home_final_arrival": arrival_dates_by_city.get(home),
            "flex": flex,
            "topn": topn,
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
        patterns = st.session_state.generated["patterns"]
        search_tasks = collect_unique_search_tasks(patterns)

        saved_counts = {}
        for oo in st.session_state.offers:
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

        # Select a concrete search task directly.
        task_labels = [
            f'{i+1}. [{t["type"]}] {t["title"]} — {t["summary"]}'
            for i, t in enumerate(search_tasks)
        ]
        selected_task_label = st.selectbox(
            "지금 검색/입력할 항공권 선택",
            task_labels,
            key="search_task_selector"
        )
        selected_task = search_tasks[task_labels.index(selected_task_label)]

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

        st.markdown("### 📌 이 검색 결과에서 입력할 정보")

        with st.expander("📋 검색 후 어떤 정보를 가져오면 되나요?", expanded=True):
            st.markdown("""
**필수 정보**
- **항공사**
- **출발시간**
- **도착시간**
- **도착일 표시**: 같은 날 / `+1` / `+2`
- **총 소요시간**
- **가격**
- **직항 또는 경유**
- 경유라면 **경유 공항** (화면에 보이는 경우)

**선택 정보**
- 수하물 포함 여부
- 추가 수하물 무게 및 가격
- 현재 Skyscanner/Google Flights 검색 페이지 URL

**가져오는 예시**
```text
Emirates
오후 11:40 → 오후 2:25 +1
20시간 45분
1회 경유 DXB
₩823,600
```

아래 `검색 결과 텍스트 붙여넣기`에 여러 항공편을 한꺼번에 붙여넣어도 됩니다.
프로그램이 항공사/시간/소요시간/경유/가격을 분리하고, 저장 전 표에서 수정할 수 있습니다.
""")


        with st.expander("기술 정보 (평소에는 볼 필요 없음)", expanded=False):
            st.code(skey, language=None)

        source_url = st.text_input("현재 검색 결과 페이지 URL (선택)", "", help="나중에 같은 검색 결과를 다시 열기 위한 용도입니다.")
        st.file_uploader("검색 화면 캡처 (선택·참고용)", type=["png","jpg","jpeg","webp"])

        manual_text = st.text_area(
            "검색 결과 텍스트 붙여넣기",
            height=260,
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
            for _, r in edited.iterrows():
                if not bool(r.get("선택", True)):
                    continue
                st.session_state.offers.append({
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
                })
                saved += 1
            autosave()
            st.success(f"{saved}개 저장 · SQLite 자동저장 완료")

    st.markdown("### 저장된 항공편")
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
        for idx, o in enumerate(st.session_state.offers):
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
            for idx, o in enumerate(st.session_state.offers):
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
        for idx, o in enumerate(st.session_state.offers):
            oo = dict(o)
            oo["_uid"] = offer_uid(o, idx)
            offer_map.setdefault(oo["search_key"], []).append(oo)

        plans = []
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
