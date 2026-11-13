"""매일 수집 + 블랙리스트 (2026-11-14 추가).

- 매일 수집: 화면에서 분석한 조건(카테고리·기간·수집 수·최소 검색량·제외 키워드)을 저장해 두면
  매일 DAILY_HOUR_KST 시 이후 한 번 같은 분석을 돌려 날짜별로 쌓는다. (gunicorn worker 1개 → 프로세스 안 스레드로 충분)
- 블랙리스트: 카테고리별로 '정확히 같은 키워드'만 결과에서 뺀다 (예: 베개 카테고리의 '베개').
  기존 '제외 키워드'는 글자가 포함되면 빼는 방식이라 '베개'를 넣으면 '메모리폼베개'까지 사라져서 따로 둠.
- 광고 위키(역추천)는 GET /api/daily/latest 로 최신 결과를 가져간다 (로그인 세션 필요).

저장: $DATA_DIR/daily/ (Railway Volume /data) — jobs.json, blacklist.json, <job id>/<YYYY-MM-DD>.json
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from flask import jsonify, render_template, request, session

from auth import DATA_DIR

DAILY_HOUR_KST = 5          # 이 시각(한국) 이후 하루 1번
CHECK_EVERY_SEC = 600       # 스케줄러 확인 주기
KEEP_DAYS = 120             # 결과 파일 보관 일수
KST = timezone(timedelta(hours=9))

ROOT = DATA_DIR / "daily"
JOBS = ROOT / "jobs.json"
BLACK = ROOT / "blacklist.json"
_lock = threading.Lock()
_running = set()
_run_analysis = None


def _today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ── 블랙리스트 ─────────────────────────────────────────────────────
def get_blacklist(cid):
    return _read(BLACK, {}).get(str(cid), [])


def set_black(cid, keyword, on=True):
    with _lock:
        b = _read(BLACK, {})
        cur = [k for k in b.get(str(cid), []) if k != keyword]
        if on:
            cur.append(keyword)
        b[str(cid)] = cur
        _write(BLACK, b)
    return b[str(cid)]


# ── 매일 수집 작업 ─────────────────────────────────────────────────
# 광고 위키가 결과를 어느 브랜드 몫으로 가져갈지 (11-14). 값은 광고 위키 brand 코드와 같아야 함.
BRANDS = {"moniv": "모니브", "holdlab": "홀드랩"}

PARAM_KEYS = ("cid", "cat_name", "days", "max_pages", "top_n", "min_monthly", "exclude_keywords")


def jobs():
    return _read(JOBS, [])


def save_job(params, by):
    with _lock:
        js = jobs()
        cid = str(params.get("cid") or "")
        j = next((x for x in js if x["cid"] == cid), None)      # 카테고리당 1개 (다시 누르면 조건만 갱신)
        if not j:
            j = {"id": uuid.uuid4().hex[:8], "created_at": _today(), "by": by, "last_date": None, "last_error": None}
            js.append(j)
        j.update({k: params.get(k) for k in PARAM_KEYS})
        j["cid"] = cid
        _write(JOBS, js)
    return j


def delete_job(jid):
    with _lock:
        _write(JOBS, [x for x in jobs() if x["id"] != jid])


def results(jid, limit=2):
    """최근 결과 limit 개 [(날짜, data)] 최신 먼저"""
    d = ROOT / jid
    files = sorted(d.glob("*.json"), reverse=True)[:limit] if d.exists() else []
    return [(f.stem, _read(f, {})) for f in files]


def run_job(jid):
    """작업 1개 실행 → 오늘 날짜 파일로 저장 (같은 날 다시 돌리면 덮어씀 = 그날 최신)."""
    if jid in _running:
        return
    _running.add(jid)
    try:
        j = next((x for x in jobs() if x["id"] == jid), None)
        if not j:
            return
        err = None
        try:
            data = _run_analysis({k: j.get(k) for k in PARAM_KEYS if j.get(k) is not None})
            for k in data.get("keywords", []):
                k.pop("series", None)                 # 일별 그래프 데이터는 저장 안 함 (용량)
            data["collected_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
            _write(ROOT / jid / f"{_today()}.json", data)
            for f in sorted((ROOT / jid).glob("*.json"))[:-KEEP_DAYS]:
                f.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
        with _lock:
            js = jobs()
            for x in js:
                if x["id"] == jid:
                    x["last_date"], x["last_error"] = _today(), err
            _write(JOBS, js)
    finally:
        _running.discard(jid)


def _scheduler():
    time.sleep(60)
    while True:
        try:
            if datetime.now(KST).hour >= DAILY_HOUR_KST:
                for j in jobs():
                    if j.get("last_date") != _today():
                        run_job(j["id"])
                        time.sleep(20)          # 네이버 요청 간격
        except Exception as e:  # noqa: BLE001
            print("[매일 수집] 오류", e)
        time.sleep(CHECK_EVERY_SEC)


def _with_change(today, prev):
    """오늘 결과에 어제 대비 급상승 점수 순위 변화(rank_change: None=새로 등장) 붙이기."""
    def order(d):
        ks = sorted(d.get("keywords", []), key=lambda k: (-(k.get("rise_score") or 0), -(k.get("growth") or 0)))
        return {k["keyword"]: i for i, k in enumerate(ks, 1)}, ks
    now_rank, ks = order(today)
    prev_rank, _ = order(prev) if prev else ({}, [])
    for k in ks:
        p = prev_rank.get(k["keyword"])
        k["rise_rank"] = now_rank[k["keyword"]]
        k["rank_change"] = (p - k["rise_rank"]) if p else None
    return ks


# ── 라우트 ─────────────────────────────────────────────────────────
def init(app, run_analysis):
    global _run_analysis
    _run_analysis = run_analysis

    @app.post("/daily/save")
    def daily_save():
        j = save_job(request.get_json(force=True), session.get("user") or "")
        if not results(j["id"], 1):
            threading.Thread(target=run_job, args=(j["id"],), daemon=True).start()   # 첫 결과는 바로
        return jsonify(ok=True, job=j)

    @app.post("/daily/<jid>/edit")
    def daily_edit(jid):
        """매일 수집 화면에서 조건(기간·수집 개수·분석 개수·최소 월간 검색량·제외 키워드) 바꾸기 → 바로 다시 수집."""
        b = request.get_json(force=True)
        with _lock:
            js = jobs()
            j = next((x for x in js if x["id"] == jid), None)
            if not j:
                return jsonify(ok=False, error="작업이 없어요"), 404
            before = {k: j.get(k) for k in PARAM_KEYS}
            try:
                j["days"] = max(31, min(int(b.get("days", j.get("days") or 90)), 365))
                j["max_pages"] = max(1, min(int(b.get("max_pages", j.get("max_pages") or 5)), 25))
                j["top_n"] = max(10, min(int(b.get("top_n", j.get("top_n") or 100)), 500))
                j["min_monthly"] = max(0, int(b.get("min_monthly", j.get("min_monthly") or 0)))
            except (TypeError, ValueError):
                return jsonify(ok=False, error="숫자로 넣어 주세요"), 400
            if "brand" in b:
                j["brand"] = b.get("brand") if b.get("brand") in BRANDS else ""
            if "exclude_keywords" in b:
                j["exclude_keywords"] = str(b.get("exclude_keywords") or "").strip()
            _write(JOBS, js)
            changed = before != {k: j.get(k) for k in PARAM_KEYS}
        if changed:          # 브랜드만 바꾼 거면 다시 분석 안 함
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
        return jsonify(ok=True, job=j, rerun=changed)

    @app.post("/daily/<jid>/delete")
    def daily_delete(jid):
        delete_job(jid)
        return jsonify(ok=True)

    @app.post("/daily/<jid>/run")
    def daily_run(jid):
        threading.Thread(target=run_job, args=(jid,), daemon=True).start()
        return jsonify(ok=True)

    @app.post("/blacklist")
    def blacklist_set():
        d = request.get_json(force=True)
        kw = (d.get("keyword") or "").strip()
        if not d.get("cid") or not kw:
            return jsonify(error="cid·keyword 필요"), 400
        return jsonify(ok=True, blacklist=set_black(d["cid"], kw, bool(d.get("on", True))))

    @app.get("/blacklist/<cid>")
    def blacklist_get(cid):
        return jsonify(blacklist=get_blacklist(cid))

    @app.get("/daily")
    def daily_page():
        rows = []
        for j in jobs():
            rs = results(j["id"], 2)
            today = rs[0] if rs else None
            prev = rs[1][1] if len(rs) > 1 else None
            rows.append({"job": j, "date": today[0] if today else None,
                         "collected_at": today[1].get("collected_at") if today else None,
                         "keywords": _with_change(today[1], prev) if today else [],
                         "blacklist": get_blacklist(j["cid"]), "running": j["id"] in _running})
        return render_template("daily.html", rows=rows, hour=DAILY_HOUR_KST, brands=BRANDS)

    @app.get("/api/daily/latest")
    def daily_latest():
        """광고 위키용: 매일 수집 작업마다 브랜드 + 최신 결과 (급상승 점수순, 어제 대비 변화 포함).
        아직 결과가 없는 작업도 brand 매핑용으로 넣음 (keywords=None)."""
        out = []
        for j in jobs():
            rs = results(j["id"], 2)
            out.append({"id": j["id"], "cid": j["cid"], "cat_name": j.get("cat_name"), "brand": j.get("brand") or "",
                        "date": rs[0][0] if rs else None,
                        "keywords": _with_change(rs[0][1], rs[1][1] if len(rs) > 1 else None) if rs else None})
        return jsonify(jobs=out)

    threading.Thread(target=_scheduler, daemon=True).start()
