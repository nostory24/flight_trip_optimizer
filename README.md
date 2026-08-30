# Flight Trip Optimizer v3.19.7

## 저장/불러오기 핵심 버그 수정

원인
Streamlit은 코드 실행 순서상 사이드바가 여행 일정 폼보다 먼저 실행됩니다.
기존에는 사이드바 `저장` 버튼 클릭 순간 DB 저장을 실행해서,
그 실행에서 사용자가 보고 있는 최신 도시/날짜가 아직 draft에 반영되기 전의
이전 상태가 저장될 수 있었습니다.

수정
1. 사이드바 저장 버튼은 DB 저장을 하지 않고 `저장 요청`만 기록
2. 여행 일정 전체 widget을 읽음
3. 최신 `draft_generated` snapshot 생성
4. 화면 도시 sequence와 draft 도시 sequence가 정확히 같은지 검증
5. 그 뒤에만 DB 저장
6. 불일치하면 저장 자체를 중단
7. 새 여행/불러오기 시 남아 있는 저장 요청 제거

즉 IST, JTR, ATH가 현재 화면에 있으면
그 3개 도시 snapshot이 만들어진 뒤에만 저장됩니다.

배포
GitHub app.py 교체 → Commit/Push → Render 자동배포.
