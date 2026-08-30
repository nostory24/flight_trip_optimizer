# Flight Trip Optimizer v3.19

## 이름 있는 여행 자동저장

한 번 여행 이름을 지정해 저장한 뒤에는 다음 변경사항을 자동저장합니다.

- 출발지 / 방문 도시
- 도시별 도착일 / 출발일
- `＋ 날짜 추가`로 만든 재방문 일정
- 정확 도착 체크
- 날짜 유연성
- Top N
- 자동 생성된 physical route / ticket pattern
- 저장 항공편
- 수하물 설정

Streamlit은 입력값 변경 시 자동 rerun되므로,
현재 이름이 지정된 여행은 rerun 시 Cloud DB의 동일 trip_id에 자동 갱신됩니다.

새 작업처럼 아직 이름이 없는 여행은 자동저장하지 않습니다.
사이드바에 `자동저장 ON`과 최근 자동저장 시간을 표시합니다.

## 배포
GitHub app.py 교체 → Commit/Push → Render 자동배포.
DATABASE_URL 변경 불필요.
