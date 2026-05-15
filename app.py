"""
네이버 쇼핑인사이트 기반 급상승 키워드 분석 웹앱
"""
import csv, json, math, time, io, hashlib, hmac, base64, threading, os
import urllib.parse, urllib.request, urllib.error
from datetime import date, timedelta
from pathlib import Path
from flask import Flask, jsonify, render_template, request, Response

app = Flask(__name__)

# ── API 자격증명 ──────────────────────────────────────────────────────
CLIENT_ID     = "WRi5ZynoxSJgIsO7KxVW"
CLIENT_SECRET = "NbbB7j_aWk"
DATALAB_URL   = "https://openapi.naver.com/v1/datalab/search"

# 검색광고 API (월간 실제 검색량용)
API_KEY       = "01000000009988b3132a19a8b6fe011404126809d4f61f251471857a6e927f5d4e57a9656c"
SECRET_KEY    = "AQAAAACZiLMTKhmotv4BFAQSaAnUikiThXDLRrEsy0ylkSDblQ=="
CUSTOMER_ID   = "2403721"
SEARCHAD_BASE = "https://api.searchad.naver.com"

SI_BASE    = "https://datalab.naver.com"
SI_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer":      "https://datalab.naver.com/shoppingInsight/sCategory.naver",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
}

# ── 카테고리 API ──────────────────────────────────────────────────────
_cat_cache: dict = {}

def _get_category(cid: int) -> dict:
    if cid in _cat_cache:
        return _cat_cache[cid]
    url = f"{SI_BASE}/shoppingInsight/getCategory.naver?cid={cid}"
    req = urllib.request.Request(url)
    for k, v in SI_HEADERS.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    _cat_cache[cid] = data
    return data


# ── 전체 카테고리 플랫 리스트 (검색용) ────────────────────────────────
_CAT_CACHE_FILE  = Path(__file__).parent / "category_cache.json"
_KEYWORD_MAP_FILE = Path(__file__).parent / "keyword_map.json"
_cat_flat: list[dict] | None = None
_cat_build_lock = threading.Lock()

def _load_keyword_map() -> dict[str, int]:
    try:
        return json.loads(_KEYWORD_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

_keyword_map: dict[str, int] = _load_keyword_map()

def _build_cat_flat():
    global _cat_flat
    with _cat_build_lock:
        if _cat_flat is not None:
            return

        print("[카테고리] 빌드 시작…")
        all_cats: list[dict] = []
        existing_cids: set = set()
        existing_l1:  set = set()

        # 기존 캐시 로드 (완성/불완성 모두)
        if _CAT_CACHE_FILE.exists():
            try:
                prev = json.loads(_CAT_CACHE_FILE.read_text(encoding="utf-8"))
                if prev:
                    all_cats.extend(prev)
                    existing_cids = {c["cid"] for c in prev}
                    # 하위 항목이 1개 이상 있는 1분류 = 완성된 것
                    existing_l1 = {
                        c["cid"] for c in prev if c["level"] == 1
                        and any(x["level"] > 1 and x["path"].startswith(c["name"] + " >") for x in prev)
                    }
                    print(f"[카테고리] 캐시 로드: {len(prev)}개, 완성 1분류: {len(existing_l1)}개")
                    if len(existing_l1) >= 12:  # 12개 1분류 모두 완성
                        _cat_flat = all_cats
                        print(f"[카테고리] 완전한 캐시 확인, 빌드 생략")
                        return
                    print(f"[카테고리] 불완전한 캐시 — 나머지 빌드 시작")
            except Exception as e:
                print(f"[카테고리] 캐시 로드 실패: {e}")

        def _fetch_with_retry(cid, retries=3):
            for attempt in range(retries):
                try:
                    return _get_category(cid)
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        wait = 5 * (attempt + 1)
                        print(f"[카테고리] 429 rate limit, {wait}초 대기…")
                        time.sleep(wait)
                    else:
                        raise
                except Exception:
                    raise
            raise Exception(f"[카테고리] CID {cid} 최대 재시도 초과")

        try:
            root = _fetch_with_retry(0)
            cat1_list = root.get("childList", [])
            print(f"[카테고리] 1분류 {len(cat1_list)}개 처리 중…")
            for cat1 in cat1_list:
                # 이미 하위 카테고리가 있는 1분류는 skip
                if cat1["cid"] in existing_l1:
                    print(f"[카테고리] {cat1['name']} skip (이미 완성)")
                    continue
                # 1분류 자체도 없으면 추가
                if cat1["cid"] not in existing_cids:
                    all_cats.append({"cid": cat1["cid"], "name": cat1["name"],
                                     "path": cat1["name"], "level": 1})
                    existing_cids.add(cat1["cid"])
                try:
                    cat1_data = _fetch_with_retry(cat1["cid"])
                except Exception as e:
                    print(f"[카테고리] {cat1['name']} 2분류 오류: {e}")
                    time.sleep(3.0)
                    continue
                for cat2 in cat1_data.get("childList", []):
                    if cat2["cid"] not in existing_cids:
                        all_cats.append({"cid": cat2["cid"], "name": cat2["name"],
                                         "path": f"{cat1['name']} > {cat2['name']}", "level": 2})
                        existing_cids.add(cat2["cid"])
                    try:
                        cat2_data = _fetch_with_retry(cat2["cid"])
                    except Exception as e:
                        print(f"[카테고리] {cat2['name']} 3분류 오류: {e}")
                        time.sleep(2.0)
                        continue
                    for cat3 in cat2_data.get("childList", []):
                        path3 = f"{cat1['name']} > {cat2['name']} > {cat3['name']}"
                        if cat3["cid"] not in existing_cids:
                            all_cats.append({"cid": cat3["cid"], "name": cat3["name"],
                                             "path": path3, "level": 3})
                            existing_cids.add(cat3["cid"])
                        if not cat3.get("leaf", True):
                            try:
                                cat3_data = _fetch_with_retry(cat3["cid"])
                                for cat4 in cat3_data.get("childList", []):
                                    if cat4["cid"] not in existing_cids:
                                        all_cats.append({"cid": cat4["cid"], "name": cat4["name"],
                                                         "path": f"{path3} > {cat4['name']}", "level": 4})
                                        existing_cids.add(cat4["cid"])
                            except Exception as e:
                                print(f"[카테고리] {cat3['name']} 4분류 오류: {e}")
                            time.sleep(0.05)
                    time.sleep(0.1)
                # 1분류 완료 시 중간 저장
                try:
                    _CAT_CACHE_FILE.write_text(json.dumps(all_cats, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                print(f"[카테고리] {cat1['name']} 완료, 누적: {len(all_cats)}개")
                time.sleep(2.0)  # 1분류 간 충분한 대기
        except Exception as e:
            print(f"[카테고리] 빌드 최상위 오류: {e}")

        print(f"[카테고리] 빌드 완료: {len(all_cats)}개")
        if all_cats:
            _cat_flat = all_cats
            try:
                _CAT_CACHE_FILE.write_text(json.dumps(all_cats, ensure_ascii=False), encoding="utf-8")
                print(f"[카테고리] 캐시 저장 완료")
            except Exception as e:
                print(f"[카테고리] 캐시 저장 실패: {e}")
        else:
            print("[카테고리] 빌드 결과 없음 — 다음 요청 시 재시도")
            # _cat_flat은 None 유지 → 프론트엔드에서 loading 상태로 처리

# 서버 프로세스에서만 백그라운드 빌드 시작 (Werkzeug reloader 부모 프로세스 제외)
# WERKZEUG_RUN_MAIN='true' → 실제 서버 자식 프로세스
# 미설정 → 리로더 없이 직접 실행
if os.environ.get('WERKZEUG_RUN_MAIN') != 'false':
    threading.Thread(target=_build_cat_flat, daemon=True).start()


@app.route("/naver/category")
def naver_product_category():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    url = "https://openapi.naver.com/v1/search/shop.json?" + urllib.parse.urlencode({
        "query": q, "display": 20, "start": 1, "sort": "sim"
    })
    req = urllib.request.Request(url)
    req.add_header("X-Naver-Client-Id",     CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", CLIENT_SECRET)

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # 카테고리 경로별 집계
    from collections import Counter
    cat_counter: Counter = Counter()
    for item in data.get("items", []):
        parts = [item.get(f"category{i}", "") for i in range(1, 5)]
        parts = [p for p in parts if p]
        if parts:
            cat_counter[" > ".join(parts)] += 1

    # 내부 카테고리 트리와 매칭해 CID 조회 (가장 구체적인 경로부터 역으로 탐색)
    path_to_cid = {c["path"]: c["cid"] for c in (_cat_flat or [])}
    results = []
    for path, count in cat_counter.most_common(10):
        cid = None
        test = path
        while test and cid is None:
            cid = path_to_cid.get(test)
            test = test.rsplit(" > ", 1)[0] if " > " in test else None
        results.append({"path": path, "count": count, "cid": cid})

    return jsonify(results)


@app.route("/categories/rebuild", methods=["POST"])
def rebuild_categories():
    global _cat_flat
    if _CAT_CACHE_FILE.exists():
        _CAT_CACHE_FILE.unlink()
    _cat_flat = None
    threading.Thread(target=_build_cat_flat, daemon=True).start()
    return jsonify({"status": "rebuilding"})


@app.route("/categories/status")
def categories_status():
    if _cat_flat is None:
        return jsonify({"status": "building", "count": 0})
    return jsonify({"status": "ready", "count": len(_cat_flat)})


@app.route("/categories/search")
def search_categories():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    if _cat_flat is None:
        return jsonify({"loading": True})

    cid_map = {c["cid"]: c for c in _cat_flat}
    seen: set = set()
    results: list[dict] = []

    # 1) keyword_map 정확 매핑 우선
    mapped_cid = _keyword_map.get(q)
    if mapped_cid and mapped_cid in cid_map:
        cat = cid_map[mapped_cid]
        results.append(cat)
        seen.add(cat["cid"])

    # 2) 이름 포함 검색 (fallback / 추가 후보)
    ql = q.lower()
    sub = [c for c in _cat_flat if ql in c["name"].lower() and c["cid"] not in seen]
    sub.sort(key=lambda c: (
        0 if c["name"].lower().startswith(ql) else 1,
        c["level"],
        c["name"],
    ))
    results.extend(sub)

    return jsonify(results[:15])


@app.route("/categories")
def get_categories():
    cid = int(request.args.get("cid", 0))
    try:
        data = _get_category(cid)
        children = [
            {"cid": c["cid"], "name": c["name"], "leaf": c.get("leaf", False)}
            for c in data.get("childList", [])
        ]
        return jsonify({"cid": cid, "name": data.get("name", ""), "children": children})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 쇼핑인사이트 키워드 수집 ──────────────────────────────────────────
def _si_keywords(cid: str, start: str, end: str, max_pages: int = 7) -> list[dict]:
    """Shopping Insight 인기검색어 수집 (최대 max_pages × 20개)"""
    keywords = []
    url = f"{SI_BASE}/shoppingInsight/getCategoryKeywordRank.naver"

    for page_num in range(1, max_pages + 1):
        body = urllib.parse.urlencode({
            "cid": cid, "timeUnit": "date",
            "startDate": start, "endDate": end,
            "age": "", "gender": "", "device": "",
            "page": str(page_num), "count": "20",
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in SI_HEADERS.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                break
            raise
        ranks = resp.get("ranks", [])
        if not ranks:
            break
        for item in ranks:
            keywords.append({
                "si_rank": item.get("rank", 0),
                "keyword": item.get("keyword", ""),
            })
        time.sleep(0.6)

    return keywords


# ── 검색광고 API (월간 실제 검색량) ──────────────────────────────────
def _sign(ts: str, method: str, uri: str) -> str:
    msg = f"{ts}.{method}.{uri}"
    h = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(h.digest()).decode()

def _to_int(v) -> int:
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("<"):  # "< 10" 같은 저검색량 표기 → 5로 처리
            return 5
    try:
        return int(v)
    except Exception:
        return 0

def _searchad_call(hint_keywords: list[str]) -> dict[str, int]:
    """Search Ad API 단일 배치 호출 → {keyword: monthly_vol}"""
    uri = "/keywordstool"
    ts  = str(round(time.time() * 1000))
    sig = _sign(ts, "GET", uri)
    params = urllib.parse.urlencode({"hintKeywords": ",".join(hint_keywords), "showDetail": "1"})
    url = SEARCHAD_BASE + uri + "?" + params
    req = urllib.request.Request(url)
    req.add_header("Content-Type", "application/json; charset=UTF-8")
    req.add_header("X-Timestamp",  ts)
    req.add_header("X-API-KEY",    API_KEY)
    req.add_header("X-Customer",   CUSTOMER_ID)
    req.add_header("X-Signature",  sig)
    result: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            kws = json.loads(r.read().decode())["keywordList"]
        # 대소문자 무관 매칭: API가 브랜드명을 대문자로 돌려줄 수 있음
        hint_lower = {kw.lower(): kw for kw in hint_keywords}
        for k in kws:
            rel = k.get("relKeyword", "")
            original = hint_lower.get(rel.lower())
            if original:
                result[original] = _to_int(k.get("monthlyPcQcCnt", 0)) + _to_int(k.get("monthlyMobileQcCnt", 0))
    except Exception:
        pass
    return result


def _searchad_monthly_batch(keywords: list[str]) -> dict[str, int]:
    """여러 키워드 월간 검색량 일괄 조회 (5개씩 배치)"""
    vol_map: dict[str, int] = {}
    for batch in _chunked(keywords, 5):
        vol_map.update(_searchad_call(batch))
        time.sleep(0.2)
    return vol_map


def _searchad_monthly(keyword: str) -> int:
    """단일 키워드 월간 검색량 (모달 상세용)"""
    result = _searchad_call([keyword])
    return result.get(keyword, 0)


# ── 데이터랩 API ──────────────────────────────────────────────────────
def _datalab_trend(keyword_list: list[str], start: str, end: str) -> dict:
    groups = [{"groupName": kw, "keywords": [kw]} for kw in keyword_list]
    body = json.dumps({
        "startDate": start, "endDate": end,
        "timeUnit": "date", "keywordGroups": groups,
    }).encode()
    req = urllib.request.Request(DATALAB_URL, method="POST")
    req.add_header("X-Naver-Client-Id",     CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", CLIENT_SECRET)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=body, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read().decode(errors="replace")}


# ── 분석 로직 ────────────────────────────────────────────────────────
def _analyze(series: list[dict], days: int) -> dict | None:
    ratios = [pt["ratio"] for pt in series]
    if not ratios:
        return None
    init_w   = min(days - 30, len(ratios) - 30)
    recent_w = 30
    if init_w <= 0:
        init_w = max(1, len(ratios) - recent_w)
    init   = ratios[:init_w]
    recent = ratios[-recent_w:]
    prior  = ratios[-(init_w + recent_w):-recent_w] or init
    init_avg   = sum(init)   / len(init)
    recent_avg = sum(recent) / len(recent)
    prior_avg  = sum(prior)  / len(prior) if prior else 1
    growth     = recent_avg / prior_avg if prior_avg > 0 else 0
    peak_idx   = ratios.index(max(ratios))
    return {
        "init_avg":   round(init_avg, 2),
        "prior_avg":  round(prior_avg, 2),
        "recent_avg": round(recent_avg, 2),
        "growth":     round(growth, 2),
        "max_ratio":  round(max(ratios), 2),
        "peak_date":  series[peak_idx]["period"],
        "series":     [{"date": pt["period"], "ratio": pt["ratio"]} for pt in series],
    }


def _score(m: dict) -> float:
    if m["recent_avg"] < 3:
        return 0
    g = max(0.1, m["growth"])
    return round(g * math.sqrt(m["recent_avg"]) / (1 + m["init_avg"] / 15), 2)


def _chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# 기본 제외 키워드 (시즌·이벤트성)
_DEFAULT_EXCLUDE = {
    "스승의날", "어버이날", "크리스마스", "발렌타인", "화이트데이",
    "빼빼로데이", "설날", "추석", "명절", "연휴", "한가위",
    "선물세트", "선물추천", "생일선물", "졸업선물", "입학선물",
    "돌선물", "결혼기념일", "기념일선물", "답례품",
}

def _is_excluded(keyword: str, extra: set[str]) -> bool:
    combined = _DEFAULT_EXCLUDE | extra
    return any(excl in keyword for excl in combined)


# ── 라우트 ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    body      = request.get_json(force=True)
    cid       = str(body.get("cid", "")).strip()
    cat_name  = body.get("cat_name", cid)
    days      = int(body.get("days", 90))
    max_pages = min(int(body.get("max_pages", 7)), 25)
    top_n     = int(body.get("top_n", 100))
    min_monthly = int(body.get("min_monthly", 1000))
    extra_exclude = {k.strip() for k in body.get("exclude_keywords", "").split(",") if k.strip()}

    if not cid:
        return jsonify({"error": "카테고리를 선택해 주세요."}), 400

    end_dt   = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=days - 1)
    end_str, start_str = end_dt.isoformat(), start_dt.isoformat()

    # 1) 쇼핑인사이트 인기검색어 (현재 + 이전 기간)
    try:
        si_kws = _si_keywords(cid, start_str, end_str, max_pages)
    except Exception as e:
        return jsonify({"error": f"쇼핑인사이트 API 오류: {e}"}), 500

    if not si_kws:
        return jsonify({"error": "키워드를 수집하지 못했습니다. CID를 확인해 주세요."}), 400

    # 제외 키워드 필터링
    si_kws = [k for k in si_kws if not _is_excluded(k["keyword"], extra_exclude)]

    prev_si_rank_map: dict = {}

    pool = si_kws[:top_n]

    # 2) 월간 검색량 일괄 조회 → 임계값 미만 제거
    vol_map = _searchad_monthly_batch([c["keyword"] for c in pool])
    if min_monthly > 0:
        candidates = [c for c in pool if vol_map.get(c["keyword"], 999999) >= min_monthly]
    else:
        candidates = pool

    if not candidates:
        return jsonify({"error": f"월간 검색량 {min_monthly:,}회 이상인 키워드가 없습니다. 임계값을 낮춰보세요."}), 400

    # 3) 데이터랩 트렌드 (5개씩 batch)
    metrics: dict = {}
    for batch in _chunked([c["keyword"] for c in candidates], 5):
        resp = _datalab_trend(batch, start_str, end_str)
        if "error" in resp:
            return jsonify({"error": f"데이터랩 API 오류: {resp.get('msg', '')[:200]}"}), 500
        for kw, item in zip(batch, resp["results"]):
            m = _analyze(item["data"], days)
            if m:
                metrics[kw] = m
        time.sleep(0.3)

    # 4) 점수 계산 + 정렬
    si_rank_map = {c["keyword"]: c["si_rank"] for c in candidates}

    # DataLab 기반 이전 기간 vol 순위 추정 (prior_avg = 최근30일 직전 구간 평균)
    valid_kws = [c["keyword"] for c in candidates if c["keyword"] in metrics]
    ratio_rank_curr = {kw: i for i, kw in enumerate(
        sorted(valid_kws, key=lambda k: metrics[k]["recent_avg"], reverse=True), 1)}
    ratio_rank_prev = {kw: i for i, kw in enumerate(
        sorted(valid_kws, key=lambda k: metrics[k]["prior_avg"], reverse=True), 1)}

    result = []
    for c in candidates:
        kw = c["keyword"]
        m  = metrics.get(kw)
        if not m:
            continue

        prev_si = prev_si_rank_map.get(kw)
        si_rc   = (prev_si - si_rank_map.get(kw, 0)) if prev_si is not None else None

        vol_rc  = ratio_rank_prev.get(kw, 0) - ratio_rank_curr.get(kw, 0)

        result.append({
            "rank":            0,
            "keyword":         kw,
            "si_rank":         si_rank_map.get(kw, 0),
            "si_rank_change":  si_rc,
            "monthly_vol":     vol_map.get(kw, 0),
            "init_avg":        m["init_avg"],
            "recent_avg":      m["recent_avg"],
            "growth":          m["growth"],
            "max_ratio":       m["max_ratio"],
            "peak_date":       m["peak_date"],
            "score":           _score(m),
            "series":          m["series"],
            "vol_rank_change": vol_rc,
        })

    result.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(result, 1):
        r["rank"] = i

    # 월간 검색량 순위 + 변화
    for i, r in enumerate(sorted(result, key=lambda x: x["monthly_vol"], reverse=True), 1):
        r["vol_rank"] = i

    return jsonify({
        "keywords":  result,
        "start":     start_str,
        "end":       end_str,
        "cat_name":  cat_name,
        "si_total":  len(si_kws),
    })


def _dl_monthly_call(keyword: str, start: str, end: str) -> list[dict]:
    """DataLab 월간 시계열 반환"""
    body = json.dumps({
        "startDate": start, "endDate": end,
        "timeUnit": "month",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }).encode()
    req = urllib.request.Request(DATALAB_URL, method="POST")
    req.add_header("X-Naver-Client-Id",     CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", CLIENT_SECRET)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=body, timeout=20) as r:
            resp = json.loads(r.read().decode())
        return resp.get("results", [{}])[0].get("data", [])
    except Exception:
        return []


def _scale_daily(series: list[dict], monthly_vol: int) -> list[dict]:
    """일별 ratio → 검색량 (월 총합 기준 비례 배분)"""
    if not series:
        return []
    if monthly_vol <= 0:
        return [{"date": p["period"], "vol": round(p["ratio"])} for p in series]
    total = sum(p["ratio"] for p in series)
    if total <= 0:
        return [{"date": p["period"], "vol": 0} for p in series]
    s = monthly_vol / total
    return [{"date": p["period"], "vol": round(p["ratio"] * s)} for p in series]


def _scale_monthly(series: list[dict], monthly_vol: int) -> list[dict]:
    """월별 ratio → 검색량 (최근 달 실제 검색량 기준 스케일)"""
    if not series:
        return []
    if monthly_vol <= 0:
        return [{"date": p["period"][:7], "vol": round(p["ratio"])} for p in series]
    last_ratio = series[-1]["ratio"]
    if last_ratio <= 0:
        return [{"date": p["period"][:7], "vol": 0} for p in series]
    s = monthly_vol / last_ratio
    return [{"date": p["period"][:7], "vol": round(p["ratio"] * s)} for p in series]


@app.route("/detail", methods=["POST"])
def detail():
    body    = request.get_json(force=True)
    keyword = body.get("keyword", "")
    if not keyword:
        return jsonify({"error": "keyword 필요"}), 400

    end_dt = date.today() - timedelta(days=1)
    # 당월은 진행 중이라 ratio가 절반 수준 → 배율 왜곡 방지를 위해 전월 말일 기준
    prev_month_end = end_dt.replace(day=1) - timedelta(days=1)

    # 1) 월간 실제 검색량 (Search Ad API)
    monthly_vol = _searchad_monthly(keyword)

    # 2) 일간: 최근 30일 (당일 기준 그대로)
    daily_start = (end_dt - timedelta(days=29)).isoformat()
    daily_resp  = _datalab_trend([keyword], daily_start, end_dt.isoformat())
    daily_series = daily_resp.get("results", [{}])[0].get("data", []) if "results" in daily_resp else []

    # 3) 월간: 최근 12개월 (당월 제외)
    monthly_series = _dl_monthly_call(keyword,
        (prev_month_end - timedelta(days=364)).isoformat(), prev_month_end.isoformat())

    # 4) 년간: 최근 3년 월간 (당월 제외)
    yearly_series = _dl_monthly_call(keyword,
        (prev_month_end - timedelta(days=365 * 3)).isoformat(), prev_month_end.isoformat())

    return jsonify({
        "keyword":     keyword,
        "monthly_vol": monthly_vol,
        "daily":       _scale_daily(daily_series, monthly_vol),
        "monthly":     _scale_monthly(monthly_series, monthly_vol),
        "yearly":      _scale_monthly(yearly_series, monthly_vol),
    })


@app.route("/download", methods=["POST"])
def download():
    body = request.get_json(force=True)
    rows = body.get("keywords", [])
    buf  = io.StringIO()
    w    = csv.writer(buf)
    w.writerow(["순위", "키워드", "인사이트순위", "월간검색량",
                "초기avg(ratio)", "최근avg(ratio)", "성장률(x)", "최고ratio", "정점날짜", "점수"])
    for r in rows:
        w.writerow([r["rank"], r["keyword"], r["si_rank"], r.get("monthly_vol", 0),
                    r["init_avg"], r["recent_avg"], r["growth"],
                    r["max_ratio"], r["peak_date"], r["score"]])
    output = buf.getvalue().encode("utf-8-sig")
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=rising_keywords.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", debug=False, use_reloader=False, port=port)
