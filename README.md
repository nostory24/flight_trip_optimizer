# Flight Trip Optimizer v3.10.2

Hotfix
- v3.10/v3.10.1의 Cloud DB 판별 변수 오류 수정
- v3.9 실제 DB 구조인 `DB_MODE + SQLAlchemy engine`에 맞춰 여행 저장 기능 전면 수정
- PostgreSQL/SQLite 모두 동일한 SQLAlchemy engine으로 처리
- `CLOUD_DB`, `USE_POSTGRES`, `pg_connect` 의존 제거
- 여행 저장/불러오기/이름변경/삭제 유지

GitHub의 app.py를 이 버전으로 교체해서 push하면 됩니다.
Render의 DATABASE_URL은 변경할 필요 없습니다.
