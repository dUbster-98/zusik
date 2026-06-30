# 기여 가이드

zusik에 기여해 주셔서 감사합니다. 개발 환경, 코드 규칙, PR 절차를 정리합니다.

## 개발 환경

```bash
git clone https://github.com/zusik-py/zusik.git
cd zusik
./setup.sh            # uv 로 .venv + 의존성 + 검증 (대화형 .env 마법사 포함)
```

자세한 설치·설정은 [docs/SETUP.md](docs/SETUP.md), 파라미터는 [docs/CONFIGURATION.md](docs/CONFIGURATION.md),
내부 동작은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)를 참고하세요.

## 변경 전 확인

- 모든 변경은 테스트 게이트를 통과해야 합니다(systemd ExecStartPre, CI와 동일 기준).
  ```bash
  python3 tests/test_bot.py     # 전체 통과 / 0 실패
  ```
- Python 3.8 호환을 유지하세요. 모든 모듈 상단에 `from __future__ import annotations`를 붙입니다.
- 매매 동작(손절/익절/사이징)을 바꾸면 손실-행동 회귀 테스트(`LossPatternRegressionTests` 등)에
  가드를 추가하고, 되돌렸을 때 그 테스트가 깨지는지 확인하세요.

## 코드 규칙

- 간결하고 목적성 있는 주석을 답니다(무엇을 하는지보다 왜 하는지를 설명하고, 날짜 스탬프와 이모지는 쓰지 않습니다).
- 리소스 경로는 `zusik/paths.py`(`data_path()`/`config_path()`)를 사용합니다. `__file__` 기반 경로는 쓰지 않습니다.
- import는 절대경로 `from zusik.<sub>.<mod>` 형식으로 작성합니다(strategies 내부는 상대경로 `.base` 가능).
- lint는 CI와 동일한 명령으로 확인합니다: `ruff check --select=E9,F63,F7,F82 .`

## PR 절차

1. 브랜치를 생성하고 변경한 뒤 `python3 tests/test_bot.py` 통과 여부를 확인합니다.
2. PR을 생성하면 CI(다중 OS/버전, ruff, 수익률 검증)와 Security(gitleaks/pip-audit/무결성) 검사가 자동으로 실행됩니다.
3. 코드를 변경했다면 `python3 security_lock.py generate`로 무결성 기준선(`security_manifest.json`)을 갱신한 뒤 커밋하세요.

## 보안

취약점은 공개 이슈로 올리지 말고 [SECURITY.md](SECURITY.md) 절차에 따라 비공개로 신고해 주세요.

## 면책

실거래 자동매매 봇입니다. 기여 및 사용에 따른 금전 손실 위험은 전적으로 사용자 책임입니다(MIT, 무보증).
