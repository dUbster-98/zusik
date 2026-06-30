from __future__ import annotations
"""Git 업데이트 Discord 알림 + 업데이트 수락/거부 버튼.

커밋 시 @everyone 핑 → 사용자가 업데이트/스킵 선택.
"""

import os
import sys
import subprocess

import discord
from discord.ui import Button, View

# 환경변수 — scripts/ 이동: CWD·import 경로를 저장소 루트(스크립트 상위)로
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

token = os.environ.get("DISCORD_BOT_TOKEN", "")
if not token:
    sys.exit(0)

_OWNER_ID = int(os.environ.get("DISCORD_OWNER_ID", "0"))


def _is_owner(interaction: discord.Interaction) -> bool:
    """명령 실행자가 봇 소유자인지 확인."""
    if _OWNER_ID == 0:
        # OWNER_ID 미설정 시 서버 관리자만 허용
        return interaction.user.guild_permissions.administrator
    return interaction.user.id == _OWNER_ID


commit_hash = sys.argv[1] if len(sys.argv) > 1 else "unknown"
commit_msg = sys.argv[2] if len(sys.argv) > 2 else ""
changed_files = sys.argv[3] if len(sys.argv) > 3 else ""


class UpdateView(View):
    def __init__(self):
        super().__init__(timeout=300)  # 5분 대기

    @discord.ui.button(label="업데이트 적용", style=discord.ButtonStyle.green, emoji=None)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if not _is_owner(interaction):
            await interaction.response.send_message("권한 없음 (소유자만 가능)", ephemeral=True)
            return
        await interaction.response.send_message("**업데이트 적용 중...**")

        # git pull + 봇 재시작
        try:
            pull = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
            subprocess.run(["sudo", "systemctl", "restart", "zusik"],
                           capture_output=True, text=True, timeout=10)
            await interaction.channel.send(
                f"**업데이트 완료**\n"
                f"```\n{pull.stdout[:500]}\n```\n"
                f"봇이 재시작되었습니다."
            )
        except Exception as e:
            await interaction.channel.send(f"**업데이트 실패**: {e}")

        self.stop()

    @discord.ui.button(label="나중에", style=discord.ButtonStyle.grey, emoji=None)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("**업데이트 건너뜀** — `/업데이트` 명령으로 나중에 적용 가능")
        self.stop()

    @discord.ui.button(label="변경사항 보기", style=discord.ButtonStyle.blurple, emoji=None)
    async def details(self, interaction: discord.Interaction, button: Button):
        try:
            diff = subprocess.run(["git", "log", "-1", "--stat"], capture_output=True, text=True, timeout=10)
            detail = diff.stdout[:1800] if diff.stdout else "변경사항 없음"
        except Exception:
            detail = "조회 실패"
        await interaction.response.send_message(f"```\n{detail}\n```")


client = discord.Client(intents=discord.Intents.default())


@client.event
async def on_ready():
    for guild in client.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                view = UpdateView()

                files_preview = changed_files[:200] if changed_files else "없음"

                await ch.send(
                    f"@everyone\n"
                    f"**새 업데이트가 있습니다**\n"
                    f"────────────────\n"
                    f"커밋: `{commit_hash}`\n"
                    f"내용: {commit_msg}\n"
                    f"변경: {files_preview}\n"
                    f"────────────────\n"
                    f"업데이트를 적용하시겠습니까?",
                    view=view,
                )

                # 5분 대기 후 자동 종료
                await view.wait()
                await client.close()
                return
    await client.close()


try:
    client.run(token, log_handler=None)
except Exception:
    pass
