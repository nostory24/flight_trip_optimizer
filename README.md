# Flight Trip Optimizer v3.19.4

Hotfix:
- `StreamlitAPIException: st.session_state.search_result_paste_text cannot be modified after the widget ... is instantiated` 수정
- 저장 성공 시 text_area 값을 같은 실행에서 직접 수정하지 않음
- 대신 clear flag 설정 → st.rerun() → 다음 실행에서 widget 생성 전에 입력창/추출결과 초기화
- v3.19.3의 완료 항공권 선택목록 자동 제외 기능 유지

배포:
GitHub app.py 교체 → Commit/Push → Render 자동배포.
