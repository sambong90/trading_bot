# CLAUDE.md

업비트 트레이딩 봇. Python 3.11, Flask, APScheduler, PostgreSQL, K8s.

## 전략 문서
코드 수정 전 반드시 확인. 수치 충돌 시 1번 우선.
1. research/data/strategies/core_logic_distilled.md (구현 명세)
2. research/data/strategies/master_strategy_filtered.md (검증 지침)
3. research/data/strategies/master_strategy.md (원본 지식)

## 출력 규칙
- 표, 구분선, 이모지, 특수 기호 금지
- 설명 없이 코드만. 서술/확인 요청/중간 보고 금지
- 변경 요약: 파일명:내용 한 줄씩 최대 5줄
- bash 출력 50줄 이상이면 tail -20
- 정상 동작은 PASS 한 단어

## 코딩 규칙
- 전체 파일 재작성 금지. 변경 블록만 최소 Diff
- grep/find로 위치 특정 후 해당 라인만 읽기. 읽은 파일 재읽기 금지
- 관련 파일 일괄 수정. 하나씩 나누지 마라
- 외부 I/O는 반드시 try/except. except pass 금지
- 매직 넘버 금지. config.py 상수 사용
- 상세 기준: docs/CODING_STANDARDS.md

## 금지
- 매매 로직 무단 수정 (strategy.py, risk.py, pattern_recognizer.py)
- master_strategy.md 구버전 수치 직접 사용
- logs/, __pycache__/, .venv/ 읽기
- kubectl rollout restart 직접 실행

## 배포
git push만. GHA가 빌드-GHCR-rollout 자동 처리. gh run view로 확인.
맥 호스트 스크립트(scripts/host/db_backup.sh, launchd plist 등)는 맥 워크스페이스 체크아웃에서 직접 실행. 봇 코드와 달리 이미지 배포로 안 바뀜. 데스크톱서 수정-push 후 맥 워크스페이스에서 git pull 해야 반영. 이 pull은 동기화이지 직접수정 금지 규칙과 무관.

## 참조 문서
- docs/CODING_STANDARDS.md: 네이밍, 에러 처리, 테스트, 워크플로우
- docs/INFRA_GUIDE.md: K8s, Secret, CI/CD, 봇 설정
- docs/CHANGELOG.md: 버그 이력, DYN_THR 정책, 수집 스케줄
