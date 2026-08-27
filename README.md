# Flight Trip Optimizer v3.10

## 여행 데이터 저장/불러오기 추가

사이드바에 `여행 데이터` 관리 기능을 추가했습니다.

- 현재 작업에 이름 붙여 저장
- 저장된 여행 불러오기
- 현재 여행 자동 업데이트
- 새 여행 시작
- 여행 이름 변경
- 여행 삭제
- 여러 여행을 PostgreSQL/SQLite에 각각 보관

예:
- 2026 터키-그리스
- 일본 2027
- 테스트 일정

## Cloud DB 동작

Render에 `DATABASE_URL`이 설정되어 있으면 모든 여행이 PostgreSQL에 저장됩니다.
따라서 같은 Render 앱을 PC/휴대폰에서 열어도 같은 저장된 여행 목록을 볼 수 있습니다.

`DATABASE_URL`이 없으면 로컬 SQLite를 사용합니다.

## 업그레이드 방법

GitHub의 기존 v3.9 파일을 v3.10 파일로 교체해서 push하면 됩니다.
Render가 GitHub 자동배포에 연결되어 있다면 자동으로 재배포됩니다.

기존 Render의 `DATABASE_URL`은 그대로 유지되므로 다시 설정할 필요가 없습니다.

## 실행

```bash
pip install -r requirements.txt
streamlit run app.py
```
