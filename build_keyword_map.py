"""
카테고리 키워드 매핑 생성기 (1회 실행)
category_cache.json을 읽어 Claude API로 keyword_map.json 생성
"""
import json, os, time
from pathlib import Path
import anthropic

BASE = Path(__file__).parent
CAT_FILE = BASE / "category_cache.json"
OUT_FILE = BASE / "keyword_map.json"

def main():
    cats = json.loads(CAT_FILE.read_text(encoding="utf-8"))
    print(f"카테고리 {len(cats)}개 로드")

    # 카테고리 목록을 텍스트로 정리 (cid|path 형태)
    cat_lines = "\n".join(f"{c['cid']}|{c['path']}" for c in cats)

    client = anthropic.Anthropic()

    prompt = f"""당신은 네이버 쇼핑 카테고리 전문가입니다.
아래는 네이버 쇼핑인사이트의 카테고리 목록입니다 (CID|경로 형태):

{cat_lines}

위 카테고리들을 기반으로, 사람들이 쇼핑할 때 자주 검색하는 한국어 키워드를 최대한 많이 생성하고,
각 키워드에 가장 적합한 카테고리 CID를 매핑해주세요.

규칙:
- 상품명, 브랜드 카테고리명, 동의어, 유사어, 줄임말 모두 포함
- 한 키워드는 가장 구체적인(하위) 카테고리 CID 1개에만 매핑
- 존재하는 CID만 사용 (위 목록에 있는 숫자만)
- 최소 500개 이상의 키워드-CID 쌍 생성

반드시 아래 JSON 형식으로만 응답하세요 (다른 설명 없이):
{{"키워드1": CID숫자, "키워드2": CID숫자, ...}}"""

    print("Claude API 호출 중...")
    msg = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    print(f"응답 길이: {len(raw)}자")

    # JSON 파싱
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    mapping = json.loads(raw)
    print(f"매핑 생성: {len(mapping)}개 키워드")

    # CID 유효성 검증
    valid_cids = {c["cid"] for c in cats}
    mapping = {k: v for k, v in mapping.items() if v in valid_cids}
    print(f"유효 CID 검증 후: {len(mapping)}개")

    OUT_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장 완료: {OUT_FILE}")

    # 샘플 출력
    samples = list(mapping.items())[:20]
    print("\n[샘플]")
    cid_to_path = {c["cid"]: c["path"] for c in cats}
    for kw, cid in samples:
        print(f"  {kw} → {cid_to_path.get(cid, '?')} ({cid})")

if __name__ == "__main__":
    main()
