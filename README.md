# Flight Trip Optimizer v3.9 — Cloud DB

## 저장 방식
- `DATABASE_URL` 환경변수가 있으면 PostgreSQL(Cloud DB) 사용
- 없으면 기존처럼 로컬 SQLite 사용
- Render에 `DATABASE_URL`을 설정하면 PC/휴대폰/다른 브라우저에서 동일 데이터 공유
- 현재 여행 데이터 JSON 내보내기 지원
- SQLite 모드에서는 일일 로컬 백업 유지

## Render 설정
1. 무료 PostgreSQL 공급자(예: Neon 등)에서 DB 생성
2. 제공받은 PostgreSQL connection string 복사
3. Render Web Service > Environment에 추가
   - Key: `DATABASE_URL`
   - Value: PostgreSQL connection string
4. 재배포
5. 앱 사이드바에 `☁️ Cloud DB 연결됨` 표시 확인

## 로컬 실행
```bash
pip install -r requirements.txt
streamlit run app.py
```

로컬에서 Cloud DB를 테스트하려면 `DATABASE_URL` 환경변수를 설정한 뒤 실행합니다.
