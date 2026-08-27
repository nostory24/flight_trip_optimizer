# Flight Trip Optimizer v3.7

결과 탭에서 항공권별 수하물 중량/추가비용/총액 포함 여부를 수정할 수 있습니다.

수하물 반영 후 자동으로:
- SQLite 저장
- 기본총액 계산
- 수하물 비용 합산
- 적용총액 계산
- Top N 재정렬
- 기본순위 대비 순위변동 표시
- CSV 결과 갱신

실행:
```bash
pip install -r requirements.txt
streamlit run app.py
```
