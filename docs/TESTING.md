# 테스트 & 헬스체크

이 문서는 두 가지를 설명합니다.

1. **테스트**: 코드가 의도대로 동작하는지 검증합니다. 네트워크 없이, 가짜 시세/계좌로 실행합니다.
2. **헬스체크**: 실제 KIS, AI, 메신저 연결이 살아있는지 확인합니다. 셋업 직후나 장 시작 전에 씁니다.

> 파이썬을 처음 본다면: "테스트"는 미리 짜둔 시나리오로 코드를 자동 실행해 "버그가 생겼는지"를
> 알려주는 안전장치입니다. 직접 코드를 읽을 필요 없이 `통과/실패`만 보면 됩니다.

---

## 1. 테스트 실행

```bash
python3 tests/test_bot.py
```

- 마지막 줄이 **`모든 테스트 통과 — 배포 가능`** 이면 정상입니다.
- 한 개라도 실패하면 **봇이 기동되지 않습니다.** `tests/test_bot.py` 는 systemd 의
  `ExecStartPre` 로 등록돼, 재시작할 때마다 가장 먼저 실행됩니다(게이트).
- 운영 서버는 파이썬 3.8 을 쓰므로, 배포 전 확인은 운영과 같은 인터프리터로 돌리세요:

  ```bash
  /usr/bin/python3 tests/test_bot.py
  ```

- 네트워크/실계좌가 **필요 없습니다**. 모든 외부 연동(KIS, 메신저, AI)은 가짜 객체로 대체됩니다.
  그래서 장중에 돌려도 실제 주문이 나가지 않습니다.

CI 에서도 push/PR 마다 자동 실행됩니다 (`.github/workflows/ci.yml`).

---

## 2. 무엇을 검증하나 (테스트 구성)

`tests/test_bot.py` 는 스모크 점검(임포트, 메서드 존재, 드라이런)을 먼저 하고, 이어서 시나리오/회귀
unittest 묶음을 실행합니다. 주요 그룹:

| 테스트 클래스 | 검증 내용 |
|---|---|
| `TradingBotRuntimeTests` | 런타임 계약 — 매수/매도 핸들러 시그니처, 잔고 갱신, 라우팅 |
| `TradingBotScenarioTests` | 실거래 흐름 시나리오: 진입 후 급등 익절, 헷지 풀체인, AI 신호가 실제 매수 게이트를 막는지(end-to-end) |
| `LossPatternRegressionTests` | "전략이 손실 방식대로 행동하지 않는가" — 손절 0% 승률 패턴 억제, 본전 보호, 트레일링 |
| `CrashSurgeResponseTests` | 급락/급등 즉시 대응 (crash_instant · surge profit-take) |
| `OrderSafetyTests` | 주문 관문 — 초과 매도, 워시트레이딩, 조작가, 현금초과 차단 (fail-closed) |
| `SecurityHardeningTests` | 보안 — eval 제거 파서, CLI env 시크릿 제거, 정정주문 검증, 암호화폐 주문 가드 |
| `AiSignalIntegrationTests` | AI 신호(크로스시그널, 데일리 편향) → 게이트/사이징 반영, 만료/캐시 |
| `TradingRecordTests` | 진짜 `PortfolioTracker` 로 체결 기록 — 수수료 반영 실현손익, 매도패턴 분류, 결정 로그 |

> 가짜 객체 위치: `FakeKISClient`(시세/주문), `FakeStrategy`(신호), `FakeTracker`(기록) —
> 모두 `tests/test_bot.py` 상단에 정의돼 있어, 실거래 흐름을 네트워크 없이 재현합니다.

---

## 3. 회귀 테스트 추가하기 (손실 패턴 가드)

이 저장소의 원칙: 손실로 이어지던 행동을 고치면, 그 행동이 되살아날 때 깨지는 테스트를 함께 추가합니다. 메서드 존재 여부를 확인하는 계약 테스트가 아니라 "전략이 손실 방식대로 행동하지 않는가"를 검증합니다.

1. `LossPatternRegressionTests`(손익 행동) 또는 의미가 맞는 클래스에 `test_...` 메서드를 추가합니다.
2. 수정 전 동작이 되살아나면 실패하도록 단언(assert)을 작성합니다. 가드가 실제로 막는지 확인하는 것이 핵심입니다.
3. 클래스가 새것이면 `run_runtime_unittests()` 의 `suite.addTests(...)` 목록에 등록합니다.
4. `python3 tests/test_bot.py` 로 통과 확인합니다.

예시는 위 표의 각 클래스 안 테스트들을 참고하세요.

---

## 4. 헬스체크 (실제 연결 확인)

테스트가 코드 로직을 점검한다면, 헬스체크는 지금 실제로 KIS와 AI가 응답하는지를 확인합니다.
셋업 직후와 장 시작 전에 사용을 권장합니다.

```bash
python main.py --healthcheck
```

출력 예시:

```
zusik 헬스체크 (모의투자)
----------------------------------------------------
[ OK ] Python — 3.8.10
[ OK ] .env KIS 키 — 설정됨
[ OK ] KIS API (모의투자) — 토큰 발급 + 시세 OK (삼성전자 71,000원)
[ OK ] AI — agy, codex, claude — 전부 응답 OK
[WARN] 메신저 — 미설정 — 알림을 받으려면 .env 에 DISCORD_WEBHOOK_URL 등을 설정
[ OK ] 경로 쓰기 — logs/ data/ 쓰기 가능
----------------------------------------------------
결과: 통과 (경고 1건) — 매매 가능, 위 경고 확인 권장
```

- `[ OK ]` / `[WARN]` / `[FAIL]` 로 한눈에 파악할 수 있으며, 각 줄에 해결 힌트가 붙습니다.
- 시크릿 값은 출력하지 않습니다(설정됨/없음만). 캡처 화면을 공유해도 키가 노출되지 않습니다.
- 핵심 점검(파이썬, KIS 키, KIS API, 경로)이 실패하면 종료코드 1을 반환합니다. cron/CI에서 활용할 수 있습니다.
- AI 점검은 설치된 provider(`claude`/`codex`/`agy` 또는 `ANTHROPIC_API_KEY`)를 각각 실제 호출해
  개별 응답을 확인합니다. 죽은 provider는 cooldown까지 반영해 "불가"로 표시됩니다.

### 장 시작 전 자동 점검 (cron)

결과 요약을 메신저로도 받으려면 `--notify` 를 붙입니다. 시크릿은 포함되지 않습니다.

```bash
python main.py --healthcheck --notify
```

KR 장 시작(09:00) 20분 전에 매일 점검하고 Discord 로 결과를 받는 예시 (crontab):

```cron
# 평일 08:40 (KST) 헬스체크 → 결과를 메신저로 (/path/to/zusik 는 본인 설치 경로로)
40 8 * * 1-5 cd /path/to/zusik && python3 main.py --healthcheck --notify >> logs/healthcheck.log 2>&1
```

> 라이브 봇이 가동 중이어도 `--healthcheck` 는 주문을 내지 않습니다. 토큰과 시세 조회,
> AI 1회 호출만 하고 종료합니다. 단, `python3 main.py` 를 인자 없이 라이브 중에 또 켜면
> 중복 주문 위험이 있으니 금지합니다. 헬스체크는 `--healthcheck` 가 붙어 있어 안전합니다.

---

## 5. 장기 수익 백테스트 (이 전략이 실제로 돈을 버는가)

단위 테스트는 코드가 맞게 동작하는지, 헬스체크는 지금 연결되는지를 봅니다. 이와 별개로
긴 실제 기간에 실제로 수익을 내는지는 장기 백테스트로 검증합니다.

```bash
python3 scripts/earnings_backtest.py                     # KR 유니버스, 250봉(~1년), momentum_breakout
python3 scripts/earnings_backtest.py --days 750          # ~3년 장기
python3 scripts/earnings_backtest.py --strategy adaptive # 봇의 로컬 선택 전략 (느림)
python3 scripts/earnings_backtest.py --us                # US 티커도 포함
```

- `config.yaml` 의 종목 유니버스(`stocks` + `us_stocks`)를 장기 일봉으로 각각 시뮬레이션해
  포트폴리오 차원의 수익(총 수익률, 실현손익, 승률, 매도패턴)을 합산 리포트합니다.
- 단일 종목만 보려면: `python3 -m zusik.analysis.backtest --code 005930 --days 250 --strategy adaptive`
- 실데이터를 사용하고 시간이 걸리므로 빠른 단위 게이트(`tests/test_bot.py`)와 분리됩니다. KIS API 호출이
  필요하고 유니버스 크기에 비례해 느립니다. ExecStartPre/CI 기본 게이트 대상이 아닙니다.
- 종료코드: 포트폴리오 수익률 ≥ 0이면 0, 손실이면 1입니다. 나이틀리 cron으로 최근 기간 기준
  수익성 자동 게이트로 활용할 수 있습니다(예: 주말마다 `--days 250` 으로 회귀 확인).

```cron
# 매주 일요일 06:00 — 최근 1년 유니버스 수익성 회귀 점검 (/path/to/zusik 는 본인 경로)
0 6 * * 0 cd /path/to/zusik && python3 scripts/earnings_backtest.py --days 250 >> logs/earnings_backtest.log 2>&1
```

> 백테스트 수익이 실거래 수익을 보장하지는 않습니다(슬리피지, 체결, 실시간 신호 차이). 전략을
> 바꾼 뒤 수익이 악화됐는지를 같은 기간과 유니버스로 비교하는 용도로 쓰는 것이 안전합니다.
> 청산 파라미터 자체의 데이터 기반 보정은 `scripts/calibrate_from_history.py`(walk-forward)가 담당합니다.

---

## 6. 놓친 급등 분석 (기회비용)

"삼성전자가 급등했는데 진입을 놓치고 그 대신 손실 매매를 했다" 같은 상황을 데이터로 가시화합니다.
감시 종목의 급등 구간을 탐지하고, 그때 봇이 보유/진입했는지(`trades.json`)를 확인해 놓친 급등과
기회비용을 추정합니다. 같은 기간 실제 실현손익과 대비하면 어디서 방향을 잘못 잡았는지 드러납니다.

```bash
python3 scripts/missed_surge_review.py                  # KR 유니버스, 60일, +8% 급등
python3 scripts/missed_surge_review.py --days 120 --surge-pct 0.06
python3 scripts/missed_surge_review.py --us             # US 포함
```

출력 예시:

```
KR   삼성전자           2026-05-15   +10.7%      +107,208
...
놓친 급등 27건 · 포착 4건 · 놓친 기회비용 추정(등가중) +3,319,469원
같은 기간 실제 실현손익 +919,287원  ← 놓친 상승 대비 '방향' 점검
```

- 놓친 급등: 급등 시작 시점에 미보유 상태이고 그 구간에 매수도 하지 않은 경우(`held_on`/`bought_between`).
- 기회비용: `가정 포지션(--capital) × 상승률` 추정치입니다. 실제 진입가/체결과 다를 수 있습니다.
- 읽기 전용(KIS 시세 + `trades.json`)입니다. 실거래 동작을 바꾸지 않고 방향 점검 용도로만 씁니다.
  놓친 상승이 큰데 실현손익이 마이너스면 진입 타이밍/모멘텀 게이트(`fast_entry`)를 재점검하는 신호입니다.
