# 작업 로그

## 2026-06-30 Groq 전용 전환

- `server.py`에서 이전 AI SDK, 이전 모델명, 이전 API 키 환경변수, 이전 생성 함수를 제거함.
- 실시간 AI 호출은 Groq OpenAI 호환 `/openai/v1/chat/completions` API만 사용하도록 정리함.
- Groq 기본 모델은 `openai/gpt-oss-20b`로 설정하고, `GROQ_MODEL` 환경변수로만 변경 가능하게 함.
- Groq 응답은 일반 채팅 응답으로 받고 서버 내부 JSON 추출/복구 파서가 처리하도록 구성함.
- 좌표를 모르는 실제 장소는 `lat`, `lng`를 `null`로 받을 수 있게 하고, 서버의 기존 좌표 보정 로직이 처리하도록 함.
- 프론트의 테스트 체크박스를 `Groq 실시간 응답 테스트`로 변경하고 payload 필드를 `live_groq`로 바꿈.
- `README.md`, `.env.example`, `render.yaml`, `requirements.txt`를 Groq 전용 기준으로 수정함.
- 기본 생성 모드는 무료 요청 한도를 아끼기 위해 로컬 빠른 일정으로 유지하고, 체크박스를 켰을 때만 Groq API를 호출하도록 유지함.
- `py -m py_compile app.py`와 `py -m py_compile codex_test/server.py` 문법 검사를 통과함.
- 로컬 빠른 생성은 `provider=local`, route 14개, 이동정보 11개로 정상 확인함.
- 실제 Groq 호출은 `provider=groq`, `model=openai/gpt-oss-20b`, route 5개, 이동정보 4개로 정상 확인함.

## 2026-06-30 Groq 무료 한도 소모 절감

- Groq 무료 한도는 모델별 분당 요청 수, 분당 토큰 수, 하루 요청/토큰 수 제한이 있어 `reset=...` 메시지가 나올 수 있음을 확인함.
- 기존 Groq 호출은 API의 JSON 강제 모드에서 검증 실패 시 400 오류가 날 수 있었음.
- 실시간 Groq 호출에서 API JSON 강제 모드를 제거해 버튼 1회당 Groq API 요청이 1회만 나가고, 응답 JSON 처리는 서버 내부 파서가 맡도록 수정함.
- 요청 본문에서 큰 JSON schema를 제거하고, 여행 기간에 따라 `max_tokens`를 동적으로 낮춰 분당 토큰 한도에 덜 걸리도록 조정함.
- 프론트에서 이전 테스트 별칭인 `strict=1`을 제거하고 `live=1`만 Groq 실시간 호출 옵션으로 유지함.
- 문법 검사와 로컬 빠른 생성 경로를 다시 통과함.

## 2026-06-30 Groq 빈 응답 방어

- `openai/gpt-oss-20b`가 reasoning 토큰을 쓰다가 최종 `message.content`를 비워 반환하는 경우를 줄이기 위해 `reasoning_effort=low`, `reasoning_format=hidden`, `max_completion_tokens`를 적용함.
- GPT-OSS 모델 권장 방식에 맞춰 system 메시지를 제거하고 user 메시지 하나에 지시문과 여행 프롬프트를 함께 넣도록 변경함.
- Groq 응답에서 `content`, `output_text`, `reasoning_content`, `reasoning`, `text`, `refusal`, `finish_reason`을 순서대로 확인하는 안전 추출 함수를 추가함.
- `live=1` 실시간 경로에서도 Groq 빈 응답, JSON 파싱 실패, 출력 길이 제한 같은 생성 실패가 사용자 화면의 400 오류로 터지지 않고 fallback 일정으로 내려오도록 변경함.
- 실제 Groq 호출 없이 빈 응답 예외를 시뮬레이션해 `provider=groq`, `fallback=True` 응답이 정상 생성되는 것을 확인함.

## 2026-06-30 GPT-OSS 유지 및 실시간 오류 숨김 제거

- 기본 Groq 모델을 다시 `openai/gpt-oss-20b`로 고정하고, reasoning 모델을 다른 모델로 자동 대체하지 않도록 복구함.
- GPT-OSS가 생성해야 하는 JSON을 `trip_theme`와 `route_points` 중심으로 줄이고, 이동정보는 서버가 계산하도록 바꿔 응답 실패 가능성을 낮춤.
- `reasoning_effort=low`, `reasoning_format=hidden`, `max_completion_tokens` 설정은 유지해 GPT-OSS의 빈 최종 응답 가능성을 낮춤.
- `live=1` 실시간 호출에서 Groq 오류를 fallback으로 숨기지 않고 실제 오류로 드러나게 되돌림.
- `py -m py_compile codex_test/server.py` 통과, 로컬 빠른 생성 정상 확인.

## 2026-06-30 외부 공유 안정 모드

- 외부 공유 링크에서 API 한도나 Groq 응답 불안정으로 오류가 뜨지 않도록 실시간 호출 잠금장치를 추가함.
- 기본 링크와 `?live=1`은 Groq API를 호출하지 않고 로컬 빠른 일정으로 동작하도록 변경함.
- 실제 GPT-OSS 호출은 `?live=1&ai=groq` 링크에서만 실행되도록 프론트 payload에 `confirm_groq`를 추가함.
- 서버는 `live_groq=true`여도 `confirm_groq`가 없으면 `provider=local` 응답을 반환하고, API 호출을 하지 않음.
- `?live=1` 페이지 응답 200, `live_groq=true/confirm_groq=false` API 응답 200 및 로컬 일정 생성을 확인함.
