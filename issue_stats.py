"""
issue_stats.py

Slack 채널에 보고된 내용을 분석하여 장애(Defect) / 기능 요청 / 제품 문의
3가지 유형으로 분류하고 통계 리포트를 생성한다.

출력 컬럼:
    작성자, 내용, 이슈 유형, 보고일, 제품명(채널명), 제품버전, 심각도

이슈 유형:
    장애        - 오류, 버그, 동작 이상, Crash 등
    기능 요청   - 신규 기능/개선 요청
    제품 문의   - 사용법/스펙/일반 문의
    해당없음    - 잡담 등 위 3가지에 해당하지 않는 메시지(리포트에서 제외)

심각도 (장애 이슈에만 부여):
    High   - Crash, 비정상 종료, 데이터 손실 등 심각한 이슈
    Medium - 기능 사용 / 사용성 관련 문제
    Low    - 그 외 사용에 지장이 없는 오류

사용법:
    1) .env 에 SLACK_TOKEN, CHANNEL_NAMES(JSON: {"채널ID": "채널명"}) 설정
    2) OpenAI 키를 open_api_real_key.txt 에 저장 (또는 OPENAI_API_KEY 환경변수)
    3) python issue_stats.py --start 2026-01-01 --end 2026-06-30

출력:
    issue_stats_{start}_{end}.csv   (Excel 호환, utf-8-sig)
    issue_stats_{start}_{end}.md    (유형/심각도별 요약 통계)
"""

import os
import re
import csv
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

load_dotenv()

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))

# 기본 분석 대상 채널. .env 의 CHANNEL_NAMES 가 있으면 그것을 우선 사용한다.
DEFAULT_CHANNELS = {
    "C030NC1BMFW": "C030NC1BMFW",
    "C04H8AMJA9H": "C04H8AMJA9H",
    "C02Q1TECSRZ": "C02Q1TECSRZ",
    "C07AG7L0FMH": "C07AG7L0FMH",
    "C02PMJGP8EB": "C02PMJGP8EB",
    "C1S0Z4ZKN": "C1S0Z4ZKN",
    "C08K8AG4R9R": "C08K8AG4R9R",
    "C02Q263KBR8": "C02Q263KBR8",
    "C023A2F2YAV": "C023A2F2YAV",
}

ISSUE_TYPES = ["장애", "기능 요청", "제품 문의"]
SEVERITIES = ["High", "Medium", "Low"]


def get_openai_client():
    """open_api_real_key.txt 또는 OPENAI_API_KEY 에서 키를 읽어 클라이언트를 만든다."""
    key = None
    key_path = "open_api_real_key.txt"
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OpenAI 키를 찾을 수 없습니다. open_api_real_key.txt 또는 "
            "OPENAI_API_KEY 환경변수를 설정하세요."
        )
    return OpenAI(api_key=key)


def load_channels():
    """CHANNEL_NAMES(JSON) 환경변수가 있으면 사용, 없으면 DEFAULT_CHANNELS 사용."""
    raw = os.environ.get("CHANNEL_NAMES", "").strip()
    if raw:
        try:
            channels = json.loads(raw)
            if isinstance(channels, dict) and channels:
                return channels
        except json.JSONDecodeError:
            print("[경고] CHANNEL_NAMES 파싱 실패 - 기본 채널 목록을 사용합니다.")
    return DEFAULT_CHANNELS


def slack_call(func, **kwargs):
    """rate limit(429) 발생 시 Retry-After 만큼 대기 후 재시도하는 래퍼."""
    for attempt in range(5):
        try:
            return func(**kwargs)
        except SlackApiError as e:
            if e.response.status_code == 429:
                delay = int(e.response.headers.get("Retry-After", "5"))
                print(f"    rate limited - {delay}s 대기")
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("Slack API 재시도 횟수 초과")


def fetch_user_map(client):
    """{user_id: real_name} 매핑 생성."""
    users = {}
    cursor = None
    while True:
        resp = slack_call(client.users_list, cursor=cursor, limit=200)
        for m in resp["members"]:
            profile = m.get("profile", {})
            name = (
                profile.get("real_name")
                or profile.get("display_name")
                or m.get("name")
                or m.get("id")
            )
            users[m["id"]] = name
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return users


def resolve_names(text, user_map):
    """메시지 내 <@U123> 멘션을 실제 이름으로 치환."""
    if not text:
        return ""

    def repl(match):
        uid = match.group(1)
        return "@" + user_map.get(uid, uid)

    return re.sub(r"<@([A-Z0-9]+)>", repl, text)


def collect_threads(client, channel_id, oldest_ts, latest_ts, user_map):
    """채널의 기간 내 상위 메시지와 각 스레드를 수집하여 항목 리스트로 반환.

    각 항목: {author, ts, date, text(스레드 전체 결합)}
    """
    items = []
    cursor = None
    while True:
        resp = slack_call(
            client.conversations_history,
            channel=channel_id,
            oldest=str(oldest_ts),
            latest=str(latest_ts),
            inclusive=True,
            limit=200,
            cursor=cursor,
        )
        for msg in resp.get("messages", []):
            if msg.get("subtype"):  # 채널 join/leave 등 시스템 메시지 제외
                continue
            ts = float(msg["ts"])
            author = user_map.get(msg.get("user", ""), msg.get("user", "unknown"))
            text = resolve_names(msg.get("text", ""), user_map)

            # 스레드가 있으면 답글까지 합쳐 문맥 구성
            if msg.get("thread_ts") and msg.get("reply_count", 0) > 0:
                text += "\n--- 스레드 답글 ---"
                r_cursor = None
                while True:
                    rep = slack_call(
                        client.conversations_replies,
                        channel=channel_id,
                        ts=msg["thread_ts"],
                        limit=200,
                        cursor=r_cursor,
                    )
                    for rmsg in rep.get("messages", [])[1:]:
                        r_author = user_map.get(
                            rmsg.get("user", ""), rmsg.get("user", "unknown")
                        )
                        r_text = resolve_names(rmsg.get("text", ""), user_map)
                        text += f"\n[{r_author}] {r_text}"
                    r_cursor = rep.get("response_metadata", {}).get("next_cursor")
                    if not r_cursor:
                        break
                    time.sleep(0.5)

            items.append(
                {
                    "author": author,
                    "ts": ts,
                    "date": datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d"),
                    "text": text.strip(),
                }
            )
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.5)
    return items


CLASSIFY_SYSTEM_PROMPT = """당신은 소프트웨어 제품 지원 로그를 분석하는 전문가입니다.
주어진 Slack 메시지(스레드 포함)를 읽고 아래 JSON 스키마에 맞춰 분류하세요.

이슈 유형(issue_type) 판단:
- "장애": 오류, 버그, 동작 이상, Crash, 비정상 종료, 데이터 손실 등 제품 결함 보고
- "기능 요청": 신규 기능 추가나 기능 개선 요청
- "제품 문의": 사용법/스펙/설치/일반 질문 등 문의
- "해당없음": 위 3가지에 해당하지 않는 잡담/공지/일정 등

심각도(severity)는 issue_type 이 "장애" 인 경우에만 부여(그 외에는 빈 문자열 ""):
- "High": Crash, 비정상 종료, 데이터 손실 등 심각한 이슈
- "Medium": 기능 사용 또는 사용성 관련 문제
- "Low": 그 외 사용에 지장이 없는 오류

product_version: 메시지에서 제품 버전(예: v1.2.3, 2024.1 등)이 언급되면 추출, 없으면 "".
summary: 보고 내용을 한국어 한 문장으로 요약.

반드시 아래 JSON 형식으로만 응답하세요:
{"issue_type": "...", "severity": "...", "product_version": "...", "summary": "..."}"""


def classify(oai_client, text):
    """LLM으로 메시지를 분류하여 dict 반환. 실패 시 해당없음 처리."""
    if not text or len(text.strip()) < 2:
        return {"issue_type": "해당없음", "severity": "", "product_version": "", "summary": ""}
    try:
        resp = oai_client.chat.completions.create(
            model="gpt-4o",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": text[:4000]},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        itype = data.get("issue_type", "해당없음")
        if itype not in ISSUE_TYPES:
            itype = "해당없음"
        sev = data.get("severity", "") or ""
        if itype != "장애":
            sev = ""
        elif sev not in SEVERITIES:
            sev = "Low"
        return {
            "issue_type": itype,
            "severity": sev,
            "product_version": data.get("product_version", "") or "",
            "summary": data.get("summary", "") or "",
        }
    except Exception as e:  # noqa: BLE001
        print(f"    [분류 실패] {e}")
        return {"issue_type": "해당없음", "severity": "", "product_version": "", "summary": ""}


def write_csv(rows, path):
    fields = ["작성자", "내용", "이슈 유형", "보고일", "제품명(채널명)", "제품버전", "심각도"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_summary(rows, path, start, end):
    by_type = {t: 0 for t in ISSUE_TYPES}
    by_sev = {s: 0 for s in SEVERITIES}
    by_channel = {}
    for r in rows:
        by_type[r["이슈 유형"]] = by_type.get(r["이슈 유형"], 0) + 1
        if r["이슈 유형"] == "장애" and r["심각도"] in by_sev:
            by_sev[r["심각도"]] += 1
        ch = r["제품명(채널명)"]
        by_channel.setdefault(ch, {t: 0 for t in ISSUE_TYPES})
        by_channel[ch][r["이슈 유형"]] += 1

    lines = []
    lines.append(f"# 이슈 통계 리포트 ({start} ~ {end})\n")
    lines.append(f"- 총 이슈 건수: **{len(rows)}**\n")
    lines.append("## 이슈 유형별 통계\n")
    lines.append("| 이슈 유형 | 건수 |")
    lines.append("| --- | ---: |")
    for t in ISSUE_TYPES:
        lines.append(f"| {t} | {by_type[t]} |")
    lines.append("")
    lines.append("## 장애 심각도별 통계\n")
    lines.append("| 심각도 | 건수 |")
    lines.append("| --- | ---: |")
    for s in SEVERITIES:
        lines.append(f"| {s} | {by_sev[s]} |")
    lines.append("")
    lines.append("## 채널(제품)별 통계\n")
    lines.append("| 제품명(채널명) | 장애 | 기능 요청 | 제품 문의 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for ch, cnt in sorted(by_channel.items()):
        lines.append(f"| {ch} | {cnt['장애']} | {cnt['기능 요청']} | {cnt['제품 문의']} |")
    lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Slack 채널 이슈 통계 생성")
    parser.add_argument("--start", default="2026-01-01", help="시작일 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-06-30", help="종료일 YYYY-MM-DD (포함)")
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=KST)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=1)
    oldest_ts = start_dt.timestamp()
    latest_ts = end_dt.timestamp()

    token = os.environ.get("SLACK_TOKEN")
    if not token:
        raise RuntimeError("SLACK_TOKEN 환경변수가 필요합니다 (.env 설정).")

    slack = WebClient(token=token)
    oai = get_openai_client()
    channels = load_channels()

    print(f"[1/3] 사용자 목록 로딩...")
    user_map = fetch_user_map(slack)
    print(f"    {len(user_map)}명 로딩 완료")

    all_rows = []
    for cid, cname in channels.items():
        print(f"[2/3] 채널 수집: {cname} ({cid})")
        try:
            items = collect_threads(slack, cid, oldest_ts, latest_ts, user_map)
        except SlackApiError as e:
            print(f"    [채널 접근 실패] {e.response.get('error')}")
            continue
        print(f"    {len(items)}개 메시지/스레드 분석 중...")
        for it in items:
            c = classify(oai, it["text"])
            if c["issue_type"] == "해당없음":
                continue
            all_rows.append(
                {
                    "작성자": it["author"],
                    "내용": c["summary"] or it["text"][:200],
                    "이슈 유형": c["issue_type"],
                    "보고일": it["date"],
                    "제품명(채널명)": cname,
                    "제품버전": c["product_version"],
                    "심각도": c["severity"],
                }
            )

    all_rows.sort(key=lambda r: r["보고일"])
    csv_path = f"issue_stats_{args.start}_{args.end}.csv"
    md_path = f"issue_stats_{args.start}_{args.end}.md"
    write_csv(all_rows, csv_path)
    write_summary(all_rows, md_path, args.start, args.end)
    print(f"[3/3] 완료 - 총 {len(all_rows)}건")
    print(f"    CSV: {csv_path}")
    print(f"    요약: {md_path}")


if __name__ == "__main__":
    main()
