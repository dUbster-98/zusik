#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# agy(Antigravity CLI) 로그인 영속화 문제 해결
#
# 증상: agy 에 한 번 로그인해도 종료 후 재실행하면 매번 다시 로그인(OAuth)을 요구.
#       agy -p "..." 비대화형 자동화가 불가능.
# 원인: agy 는 OAuth 토큰을 Secret Service API(org.freedesktop.secrets, D-Bus)에 저장한다.
#       WSL2/헤드리스 리눅스엔 이 API 를 구현한 데몬(gnome-keyring 등)이 없어 저장이 실패하고
#       토큰이 메모리에만 남았다가 종료 시 사라진다.
# 해결: Secret Service 데몬이 없으면 pass-secret-service(pass 백엔드)를 systemd user 서비스 +
#       D-Bus activation 으로 띄워 빈자리를 채운다. 이미 키링이 있으면(데스크톱 세션) 건너뛴다.
#
# 출처: https://woohyun.dev/antigravity-cli-fix-auth-persistancy/
#       grimsteel/pass-secret-service · freedesktop Secret Service 명세
#
# 사용: bash scripts/fix_agy_auth.sh        (apt 단계에서 sudo 필요)
# ──────────────────────────────────────────────────────────────────────────
set -uo pipefail

if [ -t 1 ]; then C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else C_G=; C_Y=; C_R=; C_B=; C_0=; fi
info() { printf '%s\n' "${C_G}>${C_0} $*"; }
warn() { printf '%s\n' "${C_Y}!${C_0} $*"; }
err()  { printf '%s\n' "${C_R}x${C_0} $*" >&2; }
step() { printf '\n%s\n' "${C_B}== $* ==${C_0}"; }

PSS_REPO="grimsteel/pass-secret-service"
BIN="$HOME/.local/bin/pass-secret-service"

step "환경 진단 — Secret Service 데몬 존재 여부"

# D-Bus session bus 확인 (없으면 아무 것도 못 함)
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  warn "DBUS_SESSION_BUS_ADDRESS 가 비어 있습니다 — D-Bus 세션 버스가 없습니다."
  warn "systemd 사용자 세션이 필요합니다. (WSL: /etc/wsl.conf 에 [boot] systemd=true 후 wsl --shutdown)"
  exit 1
fi

# org.freedesktop.secrets 가 이미 누군가에게 점유돼 있으면(=키링 동작) 이 fix 불필요
if busctl --user list 2>/dev/null | awk '{print $1}' | grep -qx "org.freedesktop.secrets" \
   && busctl --user status org.freedesktop.secrets >/dev/null 2>&1; then
  OWNER="$(busctl --user list 2>/dev/null | awk '$1=="org.freedesktop.secrets"{print $6}')"
  info "Secret Service 가 이미 동작 중입니다 (소유: ${OWNER:-알수없음})."
  info "이 머신엔 키링이 있으므로 ${C_B}pass-secret-service 설치는 불필요${C_0}하며, 설치하면 충돌합니다."
  cat <<EOF

${C_B}그래도 로그인이 반복된다면${C_0} 원인은 '데몬 부재'가 아니라 다음 중 하나입니다:
  - 키링이 ${C_B}잠겨 있음${C_0}(SSH/헤드리스): \`echo -n '로그인암호' | gnome-keyring-daemon --unlock\` 으로 해제,
    또는 데스크톱 로그인 세션에서 한 번 \`agy\` 로그인.
  - 봇을 ${C_B}systemd 서비스(zusik.service)로 실행${C_0} → 그 환경엔 사용자 D-Bus/키링이 없음.
    이 경우 agy 호출도 키링에 못 닿습니다. 해결: 서비스가 사용자 버스를 쓰도록
    (User= 세션 + DBUS_SESSION_BUS_ADDRESS 전달) 하거나, 데스크톱 세션에서 봇을 띄우세요.
EOF
  exit 0
fi

warn "org.freedesktop.secrets 데몬이 없습니다 — agy 토큰이 영속화되지 않는 환경입니다."
info "pass-secret-service(pass 백엔드)를 설치해 Secret Service 빈자리를 채웁니다."
printf '%s ' "계속할까요? (apt 단계에서 sudo 필요) [y/N]:"; read -r ANS || true
case "${ANS:-N}" in [Yy]*) ;; *) info "중단."; exit 0;; esac

# ── 아키텍처 (릴리스 자산 선택) ──
case "$(uname -m)" in
  x86_64)        ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64 ;;
  *) err "지원되지 않는 아키텍처: $(uname -m) — 수동 설치 필요(${PSS_REPO})"; exit 1 ;;
esac

# ── 1) pass 설치 ──
step "1) pass (standard unix password manager) 설치"
if command -v pass >/dev/null 2>&1; then
  info "pass 이미 설치됨"
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update && sudo apt-get install -y pass || { err "pass 설치 실패"; exit 1; }
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y pass || { err "pass 설치 실패"; exit 1; }
elif command -v pacman >/dev/null 2>&1; then
  sudo pacman -S --noconfirm pass || { err "pass 설치 실패"; exit 1; }
else
  err "지원되는 패키지 매니저(apt/dnf/pacman) 없음 — pass 수동 설치 필요"; exit 1
fi

# ── 2) GPG 키 (없으면 생성) ──
step "2) GPG 키 확인/생성 (pass 암호화용)"
KEYID="$(gpg --list-secret-keys --keyid-format=long 2>/dev/null | awk '/^sec/{print $2}' | cut -d/ -f2 | head -1)"
if [ -n "$KEYID" ]; then
  info "기존 GPG 키 사용: $KEYID"
else
  GPG_ID="agy-secret-service@$(hostname -s 2>/dev/null || echo localhost)"
  warn "GPG 키가 없어 새로 만듭니다 (passphrase 없음 — 자동화용)."
  warn "트레이드오프: 디스크 읽기 권한자가 토큰을 복호화 가능. 운영 환경은 별도 판단 권장."
  gpg --batch --passphrase '' --quick-generate-key "$GPG_ID" default default never \
    || { err "GPG 키 생성 실패"; exit 1; }
  KEYID="$(gpg --list-secret-keys --keyid-format=long "$GPG_ID" 2>/dev/null | awk '/^sec/{print $2}' | cut -d/ -f2 | head -1)"
  info "생성된 키: $KEYID ($GPG_ID)"
fi

# ── 3) pass 초기화 ──
step "3) pass 초기화"
if [ -f "$HOME/.password-store/.gpg-id" ]; then
  info "pass 이미 초기화됨 ($(cat "$HOME/.password-store/.gpg-id"))"
else
  pass init "$KEYID" || { err "pass init 실패"; exit 1; }
fi

# ── 4) pass-secret-service 바이너리 설치 ──
step "4) pass-secret-service 바이너리 설치 (${ARCH})"
mkdir -p "$HOME/.local/bin"
if command -v gh >/dev/null 2>&1; then
  gh release download --repo "$PSS_REPO" --pattern "pass-secret-service-${ARCH}" \
     --output "$BIN" --clobber || warn "gh 다운로드 실패 — API 폴백 시도"
fi
if [ ! -x "$BIN" ]; then
  URL="$(curl -fsSL "https://api.github.com/repos/${PSS_REPO}/releases/latest" \
        | grep -oE "https://[^\"]*pass-secret-service-${ARCH}" | head -1)"
  [ -n "$URL" ] || { err "릴리스 자산 URL 을 찾지 못함 — 수동 설치: https://github.com/${PSS_REPO}/releases"; exit 1; }
  curl -fsSL "$URL" -o "$BIN" || { err "바이너리 다운로드 실패"; exit 1; }
fi
chmod +x "$BIN"
info "설치: $("$BIN" --version 2>/dev/null || echo "$BIN")"

# ── 5) systemd user unit + D-Bus activation ──
step "5) systemd user 서비스 + D-Bus activation 등록"
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/dbus-1/services"
cat > "$HOME/.config/systemd/user/pass-secret-service.service" <<EOF
[Unit]
Description=org.freedesktop.secrets agent for pass
PartOf=graphical-session.target

[Service]
Type=dbus
BusName=org.freedesktop.secrets
ExecStart=%h/.local/bin/pass-secret-service
EOF
cat > "$HOME/.local/share/dbus-1/services/org.freedesktop.secrets.service" <<EOF
[D-BUS Service]
Name=org.freedesktop.secrets
Exec=$HOME/.local/bin/pass-secret-service
SystemdService=pass-secret-service.service
EOF
systemctl --user daemon-reload || warn "systemctl --user daemon-reload 실패"

# ── 6) 검증 ──
step "6) 검증"
if busctl --user list 2>/dev/null | awk '{print $1}' | grep -qx "org.freedesktop.secrets"; then
  info "org.freedesktop.secrets 등록됨 (activatable — 첫 호출 시 자동 기동)."
  dbus-send --session --print-reply --dest=org.freedesktop.secrets \
    /org/freedesktop/secrets org.freedesktop.DBus.Properties.Get \
    string:org.freedesktop.Secret.Service string:Collections >/dev/null 2>&1 \
    && info "Secret Service 응답 정상 (Collections 조회 OK)" \
    || warn "응답 확인 실패 — 첫 agy login 후 다시 확인하세요."
else
  warn "등록 확인 실패 — ~/.local/share/dbus-1/services 경로/내용 확인."
fi

cat <<EOF

${C_B}완료.${C_0} 이제 한 번만 로그인하면 토큰이 영속화됩니다:
  ${C_B}agy${C_0}            # 브라우저로 OAuth 1회
  exit
  ${C_B}agy -p "OK"${C_0}    # 재실행 — 로그인 요구 없이 바로 응답되면 성공
저장 확인: ${C_B}pass ls${C_0}
EOF
