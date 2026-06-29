# 공개 배포 방법

이 프로젝트는 Streamlit 없이 `codex_test/server.py`로 실행됩니다. 실시간 AI 일정 생성을 사용하려면 Groq API 키가 필요합니다.

## 로컬 실행

```powershell
py app.py
```

또는:

```powershell
py codex_test/server.py
```

접속 주소는 `http://localhost:8000` 또는 `http://127.0.0.1:8000`입니다.

## 필요한 환경변수

루트 `.env` 또는 배포 서비스의 Environment Variables에 아래 값을 넣습니다.

```env
GROQ_API_KEY=발급받은_Groq_API_키
GROQ_MODEL=openai/gpt-oss-20b
```

## Render 배포

1. GitHub 저장소에 프로젝트를 업로드합니다.
2. Render에서 `New > Blueprint`를 선택하고 GitHub 저장소를 연결합니다.
3. `render.yaml` 설정으로 배포합니다.
4. Environment Variables에 `GROQ_API_KEY`를 Secret으로 등록합니다.
5. 배포가 끝나면 Render가 제공하는 `https://...onrender.com` 링크로 접속합니다.

## 발표 전 확인

- 사이트 첫 화면이 열리는지 확인합니다.
- 기본 생성은 로컬 빠른 일정으로 즉시 나와야 합니다.
- 실제 Groq 호출을 테스트하려면 `Groq 실시간 응답 테스트`를 체크하고 일정을 생성합니다.
- 무료 한도를 아끼려면 발표 직전에는 체크박스를 끈 상태로 UI만 먼저 확인합니다.
