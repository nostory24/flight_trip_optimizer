# Flight Trip Optimizer v3.10.1

Hotfix:
- Render 실행 시 `NameError: USE_POSTGRES is not defined` 오류 수정
- v3.9에서 실제 사용하는 Cloud DB 판별 변수 `CLOUD_DB`와 통일
- 여행 저장/불러오기 기능은 그대로 유지

GitHub의 app.py를 이 버전으로 교체하여 push하면 Render가 재배포됩니다.
DATABASE_URL은 변경할 필요 없습니다.
