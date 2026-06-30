## 변경 내용
<!-- 무엇을, 왜 바꿨는지 간단히 -->

## 체크리스트
- [ ] `python3 tests/test_bot.py` 통과 (전체/0 실패)
- [ ] Python 3.8 호환 (`from __future__ import annotations`)
- [ ] 매매 동작 변경 시 손실-행동 회귀 테스트 추가 (되돌리면 깨지는지 확인)
- [ ] 코드 변경 시 `python3 security_lock.py generate` 로 무결성 기준선 갱신
- [ ] 시크릿/개인정보 미포함, `.env` 미커밋
