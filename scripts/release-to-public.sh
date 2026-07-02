#!/usr/bin/env bash
# 검증 끝난 현재 dev 상태(보통 hwirys에서 테스트 통과한 브랜치)를
# zusik-py(public)에 깨끗한 릴리스 커밋 하나로 발행한다.
#
# 사용법: scripts/release-to-public.sh vX.Y.Z ["릴리스 메모"]
#
#  - 현재 HEAD 의 트리를 그대로 공개한다(추적 파일만 → 시크릿/미추적 자동 제외).
#  - git switch 없이 plumbing(commit-tree)으로 main 을 진전시키므로 작업 트리를
#    건드리지 않는다 → 같은 디렉터리에서 도는 봇에 영향 없음.
#  - 공개 히스토리에는 dev 의 잔커밋이 섞이지 않고 "릴리스당 커밋 1개"만 쌓인다.
set -euo pipefail

VER="${1:?사용법: $0 vX.Y.Z [메시지]}"
MSG="${2:-Release $VER}"
PUB_ACCT="zusik-py"      # 공개/운영 (release 리모트)
DEV_ACCT="hwirys"        # 테스트/개발 (origin 리모트)
PUB_AUTHOR="zusik-py"
PUB_EMAIL="297802550+zusik-py@users.noreply.github.com"

cd "$(git rev-parse --show-toplevel)"

# 커밋 안 된 변경이 있으면 중단 — 공개본은 항상 커밋·검증된 상태에서만 낸다
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "오류: 커밋 안 된 변경이 있습니다. dev 브랜치에 먼저 커밋/검증하세요." >&2
  exit 1
fi

# 태그 선검사 — update-ref 이후 git tag 가 실패하면 로컬 main 만 진전된 채 남던 풋건 방지
if git rev-parse -q --verify "refs/tags/$VER" >/dev/null; then
  echo "오류: 태그 $VER 이(가) 이미 존재합니다. 같은 버전 재발행 금지." >&2
  exit 1
fi

# 현재 트리를 main(릴리스 라인) 위 새 커밋으로 — 작업트리 미변경
TREE="$(git rev-parse HEAD^{tree})"
NEW="$(GIT_AUTHOR_NAME=$PUB_AUTHOR  GIT_AUTHOR_EMAIL=$PUB_EMAIL \
       GIT_COMMITTER_NAME=$PUB_AUTHOR GIT_COMMITTER_EMAIL=$PUB_EMAIL \
       git commit-tree "$TREE" -p main -m "$MSG")"
git update-ref refs/heads/main "$NEW"
git tag "$VER" "$NEW"   # 이미 있으면 실패 → 같은 버전 덮어쓰기 방지

# 공개 계정으로 전환해 푸시 + GitHub 릴리스.
# 어디서 실패해도(set -e) trap 이 dev 계정을 복원한다 — 이전엔 중간 실패 시
# 셸이 zusik-py 로 남아 이후 gh 명령이 엉뚱한 계정으로 나갔다.
gh auth switch -u "$PUB_ACCT" >/dev/null
trap 'gh auth switch -u "$DEV_ACCT" >/dev/null || true' EXIT
git push release main
git push release "$VER"
NOTES="$(awk -v v="${VER#v}" '
  $0 ~ ("^## \\[" v "\\]") {f=1; next}
  f && /^## \[/ {exit}
  f && $0 ~ ("^\\[" v "\\]:") {exit}
  f {print}' CHANGELOG.md)"
# 릴리스 생성 실패는 숨기지 않는다 — '이미 존재' 재실행만 허용, 인증/네트워크 오류는 중단
if ! OUT="$(gh release create "$VER" --repo "$PUB_ACCT/zusik" --title "$VER" --notes "${NOTES:-$MSG}" 2>&1)"; then
  if grep -qi "already exists" <<<"$OUT"; then
    echo "릴리스 $VER 이미 존재 — 태그/푸시만 갱신됨"
  else
    echo "오류: GitHub 릴리스 생성 실패 — 태그는 푸시됨. 수동 복구: gh release create $VER --repo $PUB_ACCT/zusik" >&2
    echo "$OUT" >&2
    exit 1
  fi
fi

echo "발행 완료 → https://github.com/$PUB_ACCT/zusik/releases/tag/$VER"
