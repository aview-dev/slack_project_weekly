# Slack-Project_weekly

이 프로젝트는 매주 Slack의 project 채널에서의 대화를 요약해 노션에 전달해주는 시스템입니다.

## 🚀 프로젝트 구조

- `app.py': 각 채널을 순회해 메시지를 json으로 저장해주는 역할을 수행합니다.
- `llm.py`: app.py를 통해 생성된 json을 LLM에게 전달해 요약본을 생성 후 노션 DataBase에 해당 내용을 업로드합니다.
- 'verify_mapping.py & Verify_permissions.py': 실제 user name을 추출하는 역할을 수행합니다.

## 🛠 환경 구성

1. pip install -r requirements.txt 수행해 구동에 필요한 라이브러리를 다운 받습니다.
2. .env 파일에 각종 인증 토큰 정보를 넣습니다. sample.env를 참고하세요.
3. llm.py의 api_key_path에 GPT API 토큰의 경로를 입력합니다.

## 📊 이슈 통계 (issue_stats.py)

`issue_stats.py`는 Slack 채널에 보고된 내용을 LLM으로 분석하여 **장애 / 기능 요청 / 제품 문의** 3가지 유형으로 분류하고 통계 리포트를 생성합니다. 위 3가지에 해당하지 않는 잡담/공지 등은 "해당없음"으로 처리되어 리포트에서 제외됩니다.

### 분류 기준

- **장애**: 오류, 버그, 동작 이상, Crash, 비정상 종료, 데이터 손실 등 제품 결함 보고
- **기능 요청**: 신규 기능 추가 또는 기능 개선 요청
- **제품 문의**: 사용법/스펙/설치/일반 질문 등 문의

### 심각도 (장애 이슈에만 부여)

- **High**: Crash, 비정상 종료, 데이터 손실 등 심각한 이슈
- **Medium**: 기능 사용 또는 사용성 관련 문제
- **Low**: 그 외 사용에 지장이 없는 오류

### 환경 구성

1. `.env` 파일에 `SLACK_TOKEN`, `CHANNEL_NAMES`(JSON 형식: `{"채널ID": "채널명"}`)를 설정합니다. `CHANNEL_NAMES`가 없으면 기본 채널 목록을 사용합니다.
2. OpenAI API 키를 `open_api_real_key.txt` 파일에 저장하거나 `OPENAI_API_KEY` 환경변수로 설정합니다.

### 실행 방법

```bash
python issue_stats.py --start 2026-01-01 --end 2026-06-30
```

### 출력물

- `issue_stats_{start}_{end}.csv`: 이슈 상세 목록 (작성자, 내용, 이슈 유형, 보고일, 제품명(채널명), 제품버전, 심각도). Excel 호환(utf-8-sig).
- `issue_stats_{start}_{end}.md`: 유형별 / 심각도별 / 채널별 요약 통계.
