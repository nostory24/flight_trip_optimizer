# Flight Trip Optimizer v3.10.3

Hotfix
- `TypeError: Object of type Leg is not JSON serializable` 수정
- DB 저장, 여행 저장, JSON 내보내기 모두 동일한 `_jsonable()` 직렬화 경로 사용
- dataclass/날짜/list/set/기타 객체에 대한 안전한 fallback 추가
- 화면 상단에 `Version 3.10.3` 표시 추가

배포:
1. GitHub의 `app.py`를 v3.10.3으로 교체
2. Commit / Push
3. Render 자동배포 완료 대기
4. 화면 상단에 `Version 3.10.3`이 보이는지 확인

`DATABASE_URL`은 변경할 필요 없습니다.
