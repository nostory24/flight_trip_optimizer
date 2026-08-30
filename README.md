# Flight Trip Optimizer v3.19.1

수정:
- 마지막 방문 도시를 사이드트립으로 처리하는 발권 후보 추가
- 예: ICN -> IST -> ATH -> JTR -> ICN 일정이면 추가로
  ICN -> IST -> ATH -> JTR -> ATH -> ICN 대안을 생성
- 따라서 체크리스트에:
  - ATH -> JTR 편도
  - JTR -> ATH 편도
  - ATH <-> JTR 왕복
  - ATH -> ICN 편도
  가 필요한 발권 패턴에서 다시 등장
- 임의로 오래된 도시를 재방문하는 후보는 만들지 않음
- 사용자가 직접 선택해서 검색결과를 넣는 v3.19 방식 유지
- Cloud DB/여행 저장/자동저장 유지

GitHub app.py 교체 -> Commit/Push -> Render 자동배포.
