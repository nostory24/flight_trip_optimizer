# Flight Trip Optimizer v3.17

- 도시별 `＋ 날짜 추가` 지원
- 동일 도시 재방문은 사용자가 명시적으로 추가한 경우에만 허용
- 예: ATH → JTR → ATH → ICN 가능
- 반복 방문 날짜/정확도착 조건 Cloud DB 저장 및 복원
- 체크리스트는 실제 입력된 physical itinerary만 기반으로 생성
- 프로그램 임의 재방문은 계속 금지

GitHub app.py 교체 → Commit/Push → Render 자동배포.
