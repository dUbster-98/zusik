#!/bin/bash
# Zusik 봇 systemd 서비스 설치 — 현재 환경(사용자/경로/python)을 자동 감지해 어떤 PC 에서도 동작.
# 하드코딩 경로 없음. deploy/zusik.service 템플릿의 placeholder 를 채워 설치한다.
set -e

echo "=== Zusik 봇 서비스 설치 ==="

# ── 현재 환경 자동 감지 ──
DIR="$(cd "$(dirname "$0")/.." && pwd)"               # 레포 루트 (이 스크립트의 상위)
RUN_USER="${SUDO_USER:-$(whoami)}"                    # sudo 로 실행돼도 실제 사용자
HOME_DIR="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
HOME_DIR="${HOME_DIR:-$HOME}"
PY="$DIR/.venv/bin/python"                            # uv/venv 우선 (setup.sh 가 만든 환경)
[ -x "$PY" ] || PY="/usr/bin/python3"                 # 없으면 시스템 python

echo "  사용자: $RUN_USER"
echo "  경로  : $DIR"
echo "  python: $PY"

# ── 디렉터리 + 보안 권한 ──
mkdir -p "$DIR/logs" "$DIR/data"
if [ -f "$DIR/.env" ]; then chmod 600 "$DIR/.env"; echo "[보안] .env 권한 600"; fi
chmod 700 "$DIR/data"; echo "[보안] data/ 권한 700"

# ── 템플릿 → 실제 유닛 파일 생성 후 설치 ──
# 메인 서비스 + 비정상종료 알림(OnFailure oneshot) + 워치독(oneshot + timer)
install_unit() {  # $1 = 템플릿/설치 유닛명 (동일)
  local tmp; tmp="$(mktemp)"
  sed -e "s|__USER__|$RUN_USER|g" \
      -e "s|__DIR__|$DIR|g" \
      -e "s|__HOME__|$HOME_DIR|g" \
      -e "s|__PY__|$PY|g" \
      "$DIR/deploy/$1" > "$tmp"
  sudo cp "$tmp" "/etc/systemd/system/$1"
  rm -f "$tmp"
}

install_unit zusik.service
install_unit zusik-notify-failure.service
install_unit zusik-watchdog.service
install_unit zusik-watchdog.timer            # 치환 불요지만 동일 경로로 설치
install_unit zusik-calibrate.service         # 청산 파라미터 재학습 oneshot
install_unit zusik-calibrate.timer           # 주 1회(일요일 장마감) 자동 실행

sudo systemctl daemon-reload
sudo systemctl enable zusik
sudo systemctl enable --now zusik-watchdog.timer   # 메인과 독립 5분 감시 시작
sudo systemctl enable --now zusik-calibrate.timer  # 주간 청산 파라미터 재학습(stale 방지)

echo ""
echo "=== 설치 완료 ==="
echo "  sudo systemctl start zusik             # 시작"
echo "  sudo systemctl stop zusik              # 중지"
echo "  sudo systemctl status zusik            # 상태"
echo "  systemctl status zusik-watchdog.timer  # 워치독 타이머"
echo "  systemctl list-timers zusik-calibrate  # 다음 캘리브레이션 예정"
echo "  journalctl -u zusik -f                 # 로그"
echo "  journalctl -u zusik-watchdog -f        # 워치독 로그"
echo "  journalctl -u zusik-calibrate -f       # 캘리브레이션 로그"
echo "  sudo systemctl start zusik-calibrate   # 즉시 1회 재학습(수동)"
