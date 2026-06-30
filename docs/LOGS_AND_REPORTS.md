# 로그 & 리포트 위치

봇이 남기는 것은 **세 갈래**로 나뉩니다. 로그(`logs/`), 상태 데이터(`data/`), 사람이 읽는 리포트(`reports/` 및 메신저)입니다.

| 갈래 | 위치 | 무엇 | git |
|---|---|---|---|
| 로그 | `logs/` | 실행 로그, 오류, 매매 사유 | 제외(.gitignore) |
| 상태 | `data/` | 거래, 포지션, 자산곡선 등 JSON 상태 | 제외(.gitignore) |
| 리포트 | `reports/`, 메신저 | 사람이 읽는 요약(HTML)과 알림 | 제외(예시는 `docs/examples/`) |

---

## 1. 로그 (logs/)

| 파일 | 내용 | 회전 |
|---|---|---|
| `logs/bot.log` | 전체 실행 로그 | 자정 회전, 14일 보관 |
| `logs/errors.log` | WARNING 이상(진짜 오류만) | 크기 회전 |
| `logs/decisions.log` | **매수/매도 '왜'** 한 곳에 | 누적 |

실시간으로 보려면 (systemd 운영 시):

```bash
journalctl -u zusik -f          # 전체 실시간
journalctl -u zusik -f | grep -E "SELL|BUY|매도|매수"   # 매매만
tail -f logs/decisions.log      # 매매 사유만
```

상세도는 `config.yaml: log_level` (INFO=평시 / DEBUG=verbose).

---

## 2. 상태 데이터 (data/)

봇이 재시작해도 이어지도록 상태를 JSON 으로 보관합니다(원자적 쓰기). 주요 파일:

- `trades.json` — 모든 매수/매도 + 실현손익 + `sell_pattern`(EOD 집계의 원천)
- `positions.json` — 활성 포지션(트랜치, 트레일링, peak)
- `equity_curve.json` — 일일 자산 스냅샷(월간 리포트의 원천)
- `risk_state.json`, `reward_state.json`, `mode_state.json`, `api_costs.json` 등

> `data/` 는 개인 계좌 정보라 git 에서 제외됩니다. 백업은 이 폴더만 챙기면 됩니다.

---

## 3. 리포트 (reports/ + 메신저)

리포트는 **HTML + PDF** 두 형식으로 생성됩니다(모두 effective 기준, T+2 팬텀 보정).
HTML은 외부 폰트/JS 없이 바로 열 수 있는 단일 파일이며, PDF는 시스템의 헤드리스
Chrome/Chromium(또는 wkhtmltopdf)으로 변환합니다. 해당 백엔드가 없으면 HTML만 생성됩니다
(한글은 시스템의 Noto/맑은고딕 등으로 렌더).

### 투자결과 종합 — `reports/results.{html,pdf}`

누적 실효 수익률, 월별 성과, 매도 패턴별 손익(무엇이 돈을 벌었나)을 한곳에 정리한 종합 리포트입니다.

- 예시: **[docs/examples/results_report_example.html](examples/results_report_example.html)**, `results_report_example.pdf`
- 생성: `python3 scripts/results_report.py` (HTML+PDF) / `--no-pdf` / `--example`

### 월간 요약 — `reports/monthly/{YYYY-MM}.{html,pdf}`

매달 **마지막 날** 장 마감 후 자동 생성. 수익률, 시작/종료 자산, 입금, 실현손익, 순증, 최대낙폭, 기록일수.

- 예시: **[docs/examples/monthly_report_example.html](examples/monthly_report_example.html)**
- 아무 달이나 즉시 생성:

  ```bash
  python3 scripts/monthly_report.py                 # 이번 달 (HTML+PDF)
  python3 scripts/monthly_report.py --year 2026 --month 5 --no-pdf
  ```

### 메신저 알림 (Discord / Telegram / Slack)

실시간·정기 알림은 설정한 메신저로 전송됩니다(설정: [SETUP.md](SETUP.md) §메신저):

- 매매 즉시 알림(매수/매도/손익), 위험 경보 및 긴급 홀딩
- 장 마감 **EOD 매도 패턴 리포트**(`ambiguous_take` 등 패턴별 승률과 손익)
- **실효 수익** 분해(실현/미실현/환율)
- 매월 1일 **지난 달 월간 요약**, 매달 말일 **월간 HTML 저장 통지**(파일 경로 포함)

### 온디맨드 분석 스크립트 (읽기 전용)

```bash
python3 scripts/ambiguous_review.py        # 모호익절 라우팅 누적 효과(승률·손익)
python3 scripts/missed_surge_review.py     # 놓친 급등(기회비용)
python3 scripts/earnings_backtest.py       # 장기 수익 백테스트
python3 scripts/sell_timing_review.py      # 매도 타이밍 사후분석 (결과: data/sell_timing.json)
python3 scripts/selection_alpha_review.py  # 종목선택 지수대비 alpha (결과: data/selection_alpha.json)
```

`sell_timing_review` / `selection_alpha_review`는 KIS 시세로 **팔고 난 뒤 놓친 상승/막은
하락**(패턴별)과 **선택 종목의 지수 대비 초과수익(alpha)**, **놓친 최고종목**을 계산해
`data/*.json`에 캐시한다. 이 캐시가 있으면 **투자결과 종합 리포트**에 해당 섹션이 자동 표시된다
(없으면 생략). 매매 타이밍과 선택을 데이터로 개선하는 피드백 루프이므로, 주기적으로 실행하기를 권장한다.

---

## 통합 상태 한눈에 — `data/status.json` + `--status`

봇은 매 사이클 **`data/status.json`** 스냅샷을 갱신합니다. effective 자산, 수익률, 종목별
손익, 보유 현황, **켜진 토글 목록**, KR/US 개장 여부, WS 활성, 무결성, 최근 결정을 담습니다. 여러 JSON과 로그에 흩어진 상태를
한 파일로 모은 단일 소스입니다(effective 기준이라 T+2 팬텀이 없음).

```bash
python main.py --status      # 한 화면 대시보드 출력 + data/status.json 갱신
```

웹 대시보드(`zusik-web`, Rust)는 이 파일을 `/api/status`로 그대로 서빙하면 됩니다
(`data/`를 read-only 마운트 중이므로, `equity_curve` 직접 파싱 대신 status.json 단일 소스를 권장).

## 요약 — "어디서 보나"

| 보고 싶은 것 | 어디 |
|---|---|
| 전체 상태 한눈에 | `python main.py --status` / `data/status.json` |
| 지금 무슨 일이 일어나나 | `journalctl -u zusik -f` 또는 `logs/bot.log` |
| 왜 사고팔았나 | `logs/decisions.log` / 메신저 매매 알림 |
| 오류만 | `logs/errors.log` |
| 지난 투자결과 전체 | `reports/results.{html,pdf}` (`scripts/results_report.py`) |
| 이번 달 최종 수익 | `reports/monthly/{YYYY-MM}.{html,pdf}` (예시: docs/examples/) |
| 어떤 패턴이 돈을 버나 | 투자결과 리포트 패턴 표 / 메신저 EOD 리포트 / `scripts/ambiguous_review.py` |
