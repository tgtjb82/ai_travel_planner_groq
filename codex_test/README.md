# AI 국내여행 플래너

Streamlit 없이 실행되는 국내여행 일정 생성 웹앱입니다. 기본 모드는 빠른 로컬 일정으로 즉시 보여주고, 화면의 `Groq 실시간 응답 테스트`를 켜면 Groq API로 실시간 일정을 생성합니다.

## 주요 기능

- Groq API 기반 국내 여행 일정 생성
- 날짜별 일정표, 동선 지도, 상세 일정 표시
- 겹친 장소 마커 선택 패널
- 장소별 이름, 주소, 특징, 방문 팁 표시
- 구간별 거리, 도보/대중교통/택시 예상 시간과 요금 표시
- 마우스 휠 지도 확대/축소

## 로컬 실행

```powershell
cd codex_test
py server.py
```

브라우저에서 `http://localhost:8000` 또는 `http://127.0.0.1:8000`으로 접속합니다.

## 환경변수

루트 `.env` 또는 `codex_test/.env`에 아래 값을 넣습니다.

```env
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-20b
```

## Render 배포

1. GitHub 저장소에 `codex_test` 폴더 내용을 올립니다.
2. Render에서 `New > Blueprint` 또는 Web Service를 선택합니다.
3. `codex_test/render.yaml`을 기준으로 배포합니다.
4. Render Environment Variables에 `GROQ_API_KEY`를 Secret으로 등록합니다.
5. 배포 후 Render가 제공하는 `https://...onrender.com` 주소로 접속합니다.

## 실시간 Groq 테스트

사이트에서 `Groq 실시간 응답 테스트` 체크박스를 켠 뒤 일정을 생성하면 실제 Groq API를 호출합니다. 체크하지 않으면 무료 요청 한도를 아끼기 위해 로컬 빠른 일정이 표시됩니다.
