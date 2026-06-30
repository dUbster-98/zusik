from __future__ import annotations

"""브로커 선택 — KIS 외 다른 증권사 Open API 를 고를 수 있게 하는 팩토리.

`.env` 의 `BROKER` 값으로 어느 증권사 클라이언트를 쓸지 정한다(기본 kis). main.py 는
`create_broker()` 만 부르고, 실제 클래스 선택은 여기서 한다.

**안전 고지**: 지원 브로커는 KIS·토스 두 곳이다(둘 다 라이브 검증). 토스는 샌드박스가 없어 주문이
기본 dry-run 이며 TOSS_LIVE_ORDERS=true 일 때만 실제 전송한다(KIS_VIRTUAL 의 토스판 안전장치).
새 브로커를 붙이려면 KISClient 를 서브클래스해 네트워크 메서드만 오버라이드하고(toss_client.py 참고)
create_broker 에 분기를 추가한다.
"""

import logging
import os

logger = logging.getLogger(__name__)

# 각 브로커의 Open API 현황. 둘 다 라이브 검증된 지원 브로커.
BROKER_INFO = {
    "kis": {
        "name": "한국투자증권 (KIS)",
        "portal": "https://apiportal.koreainvestment.com",
        "auth": "OAuth (access token, 24h)",
        "markets": "국내 + 미국 주식",
        "status": "지원 (이 봇이 사용하는 검증된 기본 브로커)",
        "ready": True,
    },
    "toss": {
        "name": "토스증권 (Toss)",
        "portal": "https://developers.tossinvest.com/docs",
        "auth": "OAuth 2.0 (client_credentials)",
        "markets": "국내(KRX) + 미국 주식",
        "status": "지원 — OAuth2·시세·잔고·주문(국내+미국) 라이브 검증. 샌드박스가 없어 주문은 "
                  "기본 dry-run(안전), TOSS_LIVE_ORDERS=true 일 때만 실제 전송",
        "ready": True,
    },
}

# 브로커별 .env 자격증명 변수명 — 여러 브로커 키를 .env 에 함께 보관할 수 있게 분리.
# 활성 브로커(BROKER) 한 곳만 실제로 쓰이고, 브로커별 변수가 없으면 KIS_* 로 폴백한다
# (기존 .env 호환: 한 곳만 쓰면 KIS_* 만 채워도 됨).
BROKER_ENV = {
    "kis":  {"key": "KIS_APP_KEY",    "secret": "KIS_APP_SECRET",     "account": "KIS_ACCOUNT_NO"},
    "toss": {"key": "TOSS_CLIENT_ID", "secret": "TOSS_CLIENT_SECRET",  "account": "TOSS_ACCOUNT_NO"},
}


def account_no_required(name: str) -> bool:
    """사용자가 계좌번호를 직접 입력해야 하는가.

    KIS 는 모든 호출에 계좌(CANO)가 필요하다. 토스는 OAuth2 토큰으로 `GET /api/v1/accounts`
    에서 accountSeq 를 자동 탐색하므로 client_id/secret 만 있으면 된다(계좌번호 입력 불필요).
    """
    return (name or "kis").strip().lower() == "kis"


# 브로커별 변수명 별칭 — 포털 용어가 제각각이라(토스는 'API Key') 흔한 변형을 함께 받는다.
_ENV_ALIASES = {
    "toss": {
        "key": ("TOSS_CLIENT_ID", "TOSS_CLIENT_API_KEY", "TOSS_API_KEY"),
        "secret": ("TOSS_CLIENT_SECRET", "TOSS_SECRET_KEY", "TOSS_API_SECRET"),
        "account": ("TOSS_ACCOUNT_NO",),
    },
}


def _first_env(names) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


def resolve_broker_credentials(name: str) -> dict:
    """활성 브로커의 자격증명을 .env 에서 해석.

    브로커별 변수(BROKER_ENV + 별칭)를 우선 읽고, 없으면 KIS_* 로 폴백한다. 그래서 여러 브로커
    키를 .env 에 함께 둬도 충돌 없이 활성 브로커 것만 쓰이고, 한 곳만 쓰는 사용자는 KIS_* 만
    채워도 동작한다(하위호환). account_prod·is_virtual 은 공통(KIS_*)을 따른다.
    """
    key = (name or "kis").strip().lower()
    env = BROKER_ENV.get(key, BROKER_ENV["kis"])
    al = _ENV_ALIASES.get(key, {})
    g = os.getenv

    def pick(field, kis_var):
        names = (env[field], *al.get(field, ()))
        return _first_env(names) or g(kis_var, "") or ""

    return {
        "app_key": pick("key", "KIS_APP_KEY"),
        "app_secret": pick("secret", "KIS_APP_SECRET"),
        "account_no": pick("account", "KIS_ACCOUNT_NO"),
        "account_prod": g("KIS_ACCOUNT_PROD", "01"),
        "is_virtual": g("KIS_VIRTUAL", "true").strip().lower() == "true",
    }


def create_broker(name: str, *, app_key: str, app_secret: str, account_no: str,
                  account_prod: str = "01", is_virtual: bool = False):
    """`BROKER` 값에 맞는 증권사 클라이언트를 생성(kis/toss). 알 수 없는 이름이면 ValueError."""
    key = (name or "kis").strip().lower()
    if key == "kis":
        from zusik.clients.kis_client import KISClient
        return KISClient(app_key=app_key, app_secret=app_secret, account_no=account_no,
                         account_prod=account_prod, is_virtual=is_virtual)
    if key == "toss":
        # 토스는 주문 기본 dry-run(TOSS_LIVE_ORDERS=true 로 실주문).
        from zusik.clients.toss_client import TossClient
        return TossClient(app_key=app_key, app_secret=app_secret, account_no=account_no,
                          account_prod=account_prod, is_virtual=is_virtual)
    raise ValueError(f"알 수 없는 BROKER='{name}'. 지원: {', '.join(BROKER_INFO)} (기본 kis)")


def list_brokers_text() -> str:
    """브로커 현황을 사람이 읽는 표로(설정/문서/헬스체크용)."""
    lines = ["사용 가능한 브로커 (BROKER=):"]
    for key, info in BROKER_INFO.items():
        mark = "검증" if info["ready"] else "실험"
        lines.append(f"  - {key:<8} [{mark}] {info['name']} — {info['markets']}")
        lines.append(f"      {info['status']}")
        lines.append(f"      포털: {info['portal']}")
    return "\n".join(lines)
