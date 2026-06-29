# 작업 로그

## 2026-06-30 Groq 전용 전환

- `server.py`에서 이전 AI SDK, 이전 모델명, 이전 API 키 환경변수, 이전 생성 함수를 제거함.
- 실시간 AI 호출은 Groq OpenAI 호환 `/openai/v1/chat/completions` API만 사용하도록 정리함.
- Groq 기본 모델은 `openai/gpt-oss-20b`로 설정하고, `GROQ_MODEL` 환경변수로만 변경 가능하게 함.
- Groq 응답은 우선 `json_schema`로 받고, 모델이 스키마 검증에 실패하면 같은 Groq API에서 `json_object` 모드로 한 번만 재시도하도록 구성함.
- 좌표를 모르는 실제 장소는 `lat`, `lng`를 `null`로 받을 수 있게 하고, 서버의 기존 좌표 보정 로직이 처리하도록 함.
- 프론트의 테스트 체크박스를 `Groq 실시간 응답 테스트`로 변경하고 payload 필드를 `live_groq`로 바꿈.
- `README.md`, `.env.example`, `render.yaml`, `requirements.txt`를 Groq 전용 기준으로 수정함.
- 기본 생성 모드는 무료 요청 한도를 아끼기 위해 로컬 빠른 일정으로 유지하고, 체크박스를 켰을 때만 Groq API를 호출하도록 유지함.
- `py -m py_compile app.py`와 `py -m py_compile codex_test/server.py` 문법 검사를 통과함.
- 로컬 빠른 생성은 `provider=local`, route 14개, 이동정보 11개로 정상 확인함.
- 실제 Groq 호출은 `provider=groq`, `model=openai/gpt-oss-20b`, route 5개, 이동정보 4개로 정상 확인함.

## 2026-06-30 Groq 무료 한도 소모 절감

- Groq 무료 한도는 모델별 분당 요청 수, 분당 토큰 수, 하루 요청/토큰 수 제한이 있어 `reset=...` 메시지가 나올 수 있음을 확인함.
- 기존 Groq 호출은 `json_schema` 검증 실패 시 `json_object`로 한 번 더 재시도할 수 있어 생성 버튼 1회가 최대 2회 API 요청이 될 수 있었음.
- 실시간 Groq 호출을 `json_object` 단일 요청으로 변경해 버튼 1회당 Groq API 요청이 1회만 나가도록 수정함.
- 요청 본문에서 큰 JSON schema를 제거하고, 여행 기간에 따라 `max_tokens`를 동적으로 낮춰 분당 토큰 한도에 덜 걸리도록 조정함.
- 프론트에서 이전 테스트 별칭인 `strict=1`을 제거하고 `live=1`만 Groq 실시간 호출 옵션으로 유지함.
- 문법 검사와 로컬 빠른 생성 경로를 다시 통과함.
