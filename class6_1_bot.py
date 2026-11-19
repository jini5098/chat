"""
배방초등학교 6학년 1반 카카오톡 AI 봇 '톡반장' — 출시·배포용 (class6_1_bot.py)

실행 방법:
    1) 로컬 콘솔 테스트:    python class6_1_bot.py --test
    2) 로컬 서버 실행:      python class6_1_bot.py
    3) 클라우드 배포 실행:  gunicorn class6_1_bot:app --bind 0.0.0.0:$PORT
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import sys
from typing import Optional

import requests
from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


# ============================================================
# 1. 기본 설정 (.env 파일 또는 클라우드 환경변수에서 로드)
# ============================================================
KST = datetime.timezone(datetime.timedelta(hours=9))

BOT_NAME = os.getenv("BOT_NAME", "톡반장")
BOT_ALIASES = [BOT_NAME, f"{BOT_NAME}아", f"{BOT_NAME}야", f"!{BOT_NAME}"]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NEIS_API_KEY = os.getenv("NEIS_API_KEY", "")  # 6교시 전체 조회를 위해 나이스 무료 인증키 권장

SCHOOL_NAME = os.getenv("SCHOOL_NAME", "배방초등학교")
ATPT_OFCDC_SC_CODE = os.getenv("ATPT_OFCDC_SC_CODE", "N10")  # 충청남도교육청
SD_SCHUL_CODE = os.getenv("SD_SCHUL_CODE", "")               # 비워두면 나이스에서 자동 조회
GRADE = os.getenv("GRADE", "6")
CLASS_NM = os.getenv("CLASS_NM", "1")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ALLOW_DIRECT_IN_KAKAO_CHANNEL = True

# 나이스에 학교 시간표가 미등록일 때 대신 보여줄 백업 시간표 (필요 시 요일별로 채워두세요)
# 0=월, 1=화, 2=수, 3=목, 4=금
FALLBACK_TIMETABLE: dict[int, list[str]] = {
    # 예시: 0: ["국어", "수학", "사회", "영어", "체육", "음악"],
}

SYSTEM_INSTRUCTION = f"""
너는 배방초등학교 6학년 1반 카카오톡 AI 봇 '{BOT_NAME}'이야.
- 말투: 친근하고 센스 있는 해요체. 카톡에서 읽기 편하게 3~5줄 이내로 빠르게 답해.
- 최신 정보나 궁금한 사실은 Google 검색 도구를 활용해 정확하게 답해.
- 가벼운 수다나 농담에는 유쾌하게 받아쳐 줘.
""".strip()

_SCHOOL_CODE_CACHE: dict[str, str] = {}


# ============================================================
# 2. 호출 규칙 판별 (이름 호출 / 답장 감지)
# ============================================================
def extract_query_if_called(
    text: str,
    is_reply_to_bot: bool = False,
    allow_direct: bool = False,
) -> Optional[str]:
    cleaned = text.strip()
    if not cleaned:
        return None

    for alias in sorted(BOT_ALIASES, key=len, reverse=True):
        if cleaned.startswith(alias):
            rest = cleaned[len(alias):].lstrip(" ,:!~")
            return rest if rest else "안녕!"

    if is_reply_to_bot or allow_direct:
        return cleaned

    return None


# ============================================================
# 3. 나이스(NEIS) 학교 코드 자동 조회 + 급식 & 시간표 (KST 기준)
# ============================================================
def resolve_school_codes() -> tuple[str, str]:
    if ATPT_OFCDC_SC_CODE and SD_SCHUL_CODE:
        return ATPT_OFCDC_SC_CODE, SD_SCHUL_CODE

    if "office" in _SCHOOL_CODE_CACHE and "school" in _SCHOOL_CODE_CACHE:
        return _SCHOOL_CODE_CACHE["office"], _SCHOOL_CODE_CACHE["school"]

    url = "https://open.neis.go.kr/hub/schoolInfo"
    params = {"Type": "json", "SCHUL_NM": SCHOOL_NAME}
    if ATPT_OFCDC_SC_CODE:
        params["ATPT_OFCDC_SC_CODE"] = ATPT_OFCDC_SC_CODE
    if NEIS_API_KEY:
        params["KEY"] = NEIS_API_KEY

    res = requests.get(url, params=params, timeout=3.5)
    data = res.json()
    row = data["schoolInfo"][1]["row"][0]
    office_code = row["ATPT_OFCDC_SC_CODE"]
    school_code = row["SD_SCHUL_CODE"]

    _SCHOOL_CODE_CACHE["office"] = office_code
    _SCHOOL_CODE_CACHE["school"] = school_code
    return office_code, school_code


def _resolve_target_date(query: str) -> tuple[datetime.datetime, str, str]:
    now = datetime.datetime.now(KST)
    if "모레" in query:
        target = now + datetime.timedelta(days=2)
        label = "모레"
    elif "내일" in query:
        target = now + datetime.timedelta(days=1)
        label = "내일"
    else:
        target = now
        label = "오늘"
    return target, target.strftime("%Y%m%d"), f"{label}({target.strftime('%m/%d')})"


def fetch_neis_meal(query: str) -> str:
    try:
        office_code, school_code = resolve_school_codes()
    except Exception as e:
        return f"{SCHOOL_NAME} 학교 코드를 찾는 중 오류가 났어요: {e}"

    _, ymd, date_label = _resolve_target_date(query)
    url = "https://open.neis.go.kr/hub/mealServiceDietInfo"
    params = {
        "Type": "json",
        "ATPT_OFCDC_SC_CODE": office_code,
        "SD_SCHUL_CODE": school_code,
        "MLSV_YMD": ymd,
    }
    if NEIS_API_KEY:
        params["KEY"] = NEIS_API_KEY

    try:
        res = requests.get(url, params=params, timeout=3.5)
        data = res.json()
        rows = data["mealServiceDietInfo"][1]["row"]
        raw_dish = rows[0]["DDISH_NM"]
        dishes = [re.sub(r"\([0-9.]+\)", "", line).strip() for line in raw_dish.split("<br/>")]
        menu_text = "\n".join(f"• {d}" for d in dishes if d)
        cal = rows[0].get("CAL_INFO", "")
        return f"🍱 {SCHOOL_NAME} {date_label} 급식\n{menu_text}\n({cal})"
    except KeyError:
        return f"🍱 {date_label}에는 등록된 급식 정보가 없어요. (주말/공휴일 또는 미등록)"
    except Exception as e:
        return f"급식 정보를 가져오는 중 오류가 났어요: {e}"


def fetch_neis_timetable(query: str) -> str:
    target_dt, ymd, date_label = _resolve_target_date(query)

    try:
        office_code, school_code = resolve_school_codes()
        url = "https://open.neis.go.kr/hub/elsTimetable"
        params = {
            "Type": "json",
            "ATPT_OFCDC_SC_CODE": office_code,
            "SD_SCHUL_CODE": school_code,
            "GRADE": GRADE,
            "CLASS_NM": CLASS_NM,
            "ALL_TI_YMD": ymd,
            "pSize": 10,
        }
        if NEIS_API_KEY:
            params["KEY"] = NEIS_API_KEY

        res = requests.get(url, params=params, timeout=3.5)
        data = res.json()
        rows = data["elsTimetable"][1]["row"]
        rows.sort(key=lambda r: int(r.get("PERIO", 0)))
        lines = [f"{r['PERIO']}교시: {r['ITRT_CNTNT']}" for r in rows]
        return f"📚 {SCHOOL_NAME} {GRADE}학년 {CLASS_NM}반 {date_label} 시간표\n" + "\n".join(lines)
    except KeyError:
        # 나이스에 미등록된 경우 백업 시간표가 있으면 자동 사용
        weekday = target_dt.weekday()
        if weekday in FALLBACK_TIMETABLE:
            lines = [f"{i}교시: {subj}" for i, subj in enumerate(FALLBACK_TIMETABLE[weekday], start=1)]
            return f"📚 {SCHOOL_NAME} {GRADE}학년 {CLASS_NM}반 {date_label} 시간표\n" + "\n".join(lines)
        return f"📚 {date_label}에는 나이스에 등록된 시간표가 없어요. (주말/공휴일 또는 학교 미등록)"
    except Exception as e:
        return f"시간표를 가져오는 중 오류가 났어요: {e}"


# ============================================================
# 4. 오늘의 운세 (KST 날짜 + 사용자 ID 시드 고정)
# ============================================================
def build_daily_fortune_seed(user_id: str) -> dict:
    today = datetime.datetime.now(KST).date().isoformat()
    digest = hashlib.sha256(f"{today}:{user_id}".encode("utf-8")).hexdigest()
    score = (int(digest[:4], 16) % 41) + 60
    colors = ["하늘색", "노란색", "민트색", "빨간색", "보라색", "초록색", "주황색", "네이비"]
    items = ["지우개", "텀블러", "형광펜", "운동화", "포스트잇", "이어폰", "초콜릿", "손목시계"]
    return {
        "date": today,
        "score": score,
        "lucky_color": colors[int(digest[4:6], 16) % len(colors)],
        "lucky_item": items[int(digest[6:8], 16) % len(items)],
        "lucky_num": (int(digest[8:10], 16) % 9) + 1,
    }


# ============================================================
# 5. Gemini API 클라이언트 (AQ. Express Mode 키 자동 지원)
# ============================================================
def _create_genai_client():
    if GEMINI_API_KEY.startswith("AQ."):
        return genai.Client(vertexai=True, api_key=GEMINI_API_KEY)
    return genai.Client(api_key=GEMINI_API_KEY)


def ask_gemini(prompt: str, enable_search: bool = True) -> str:
    if not GEMINI_API_KEY:
        return "GEMINI_API_KEY가 비어 있어요! 환경변수(.env)에 API 키를 넣어주세요."
    if genai is None or types is None:
        return "`google-genai` 패키지가 필요합니다. `pip install -r requirements.txt`를 실행해 주세요."

    client = _create_genai_client()
    tools = [types.Tool(google_search=types.GoogleSearch())] if enable_search else []

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=tools,
            ),
        )
        return response.text or "음, 답변을 만들지 못했어요. 다시 한 번 물어봐 줄래요?"
    except Exception as e:
        return f"AI 응답 생성 중 오류가 났어요: {e}"


# ============================================================
# 6. 통합 메시지 라우터
# ============================================================
def handle_message(
    text: str,
    user_id: str,
    is_reply_to_bot: bool = False,
    allow_direct: bool = False,
) -> Optional[str]:
    query = extract_query_if_called(
        text,
        is_reply_to_bot=is_reply_to_bot,
        allow_direct=allow_direct,
    )
    if query is None:
        return None

    if any(k in query for k in ["급식", "밥", "식단", "점심"]):
        return fetch_neis_meal(query)

    if any(k in query for k in ["시간표", "교시", "수업"]):
        return fetch_neis_timetable(query)

    if any(k in query for k in ["운세", "타로", "행운", "오늘 운"]):
        seed = build_daily_fortune_seed(user_id)
        if not GEMINI_API_KEY:
            return (
                f"🔮 오늘의 운세 ({seed['date']})\n"
                f"• 행운 점수: {seed['score']}점\n"
                f"• 행운의 색: {seed['lucky_color']} / 숫자: {seed['lucky_num']}\n"
                f"• 행운의 아이템: {seed['lucky_item']}\n"
                f"• 한마디: 오늘은 자신감 있게 도전하면 좋은 일이 생길 거예요!"
            )
        fortune_prompt = (
            f"배방초등학교 6학년 1반 친구의 오늘({seed['date']}) 운세를 재밌고 짧게 써줘.\n"
            f"고정 수치 -> 행운점수: {seed['score']}점, 행운의 색: {seed['lucky_color']}, "
            f"행운의 숫자: {seed['lucky_num']}, 행운의 아이템: {seed['lucky_item']}.\n"
            "총운, 공부운, 친구운을 각 1줄씩 깔끔하게 정리해줘."
        )
        return ask_gemini(fortune_prompt, enable_search=False)

    return ask_gemini(query, enable_search=True)


# ============================================================
# 7. 배포용 Flask WSGI 앱 (gunicorn class6_1_bot:app)
# ============================================================
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": BOT_NAME, "school": SCHOOL_NAME})


@app.route("/skill", methods=["POST"])
def kakao_skill():
    payload = request.get_json(silent=True) or {}
    user_req = payload.get("userRequest", {})
    utterance = user_req.get("utterance", "")
    user_id = user_req.get("user", {}).get("id", "unknown")

    answer = handle_message(
        text=utterance,
        user_id=user_id,
        allow_direct=ALLOW_DIRECT_IN_KAKAO_CHANNEL,
    )
    if not answer:
        answer = f"'{BOT_NAME} 오늘 급식', '{BOT_NAME} 시간표', '{BOT_NAME} 운세'처럼 불러 주세요!"

    return jsonify({
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": answer[:1000]
                    }
                }
            ]
        },
    })


def run_console_prototype() -> None:
    print(f"=== {SCHOOL_NAME} 6학년 1반 '{BOT_NAME}' 콘솔 프로토타입 ===")
    print(f"이름 호출 예시: '{BOT_NAME} 오늘 급식', '{BOT_NAME} 시간표', '{BOT_NAME} 운세'")
    print("답장 테스트: 문장 앞에 '@reply ' 붙이기 (종료: quit)\n")

    user_name = input("테스트할 닉네임 입력 (기본: 홍): ").strip() or "홍"
    while True:
        try:
            raw = input(f"\n[{user_name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if raw.lower() in {"quit", "exit"}:
            break

        is_reply = False
        if raw.startswith("@reply "):
            is_reply = True
            raw = raw[len("@reply "):]

        reply = handle_message(
            text=raw,
            user_id=user_name,
            is_reply_to_bot=is_reply,
            allow_direct=False,
        )
        if reply is None:
            print("(봇이 호출되지 않아 무시함)")
        else:
            print(f"\n[{BOT_NAME}]\n{reply}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_console_prototype()
    else:
        port = int(os.getenv("PORT", "5000"))
        print(f"'{BOT_NAME}' 카카오톡 스킬 서버 시작: http://0.0.0.0:{port}/skill")
        app.run(host="0.0.0.0", port=port)