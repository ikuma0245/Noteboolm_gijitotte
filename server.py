"""
議事録レコーダー ローカルサーバー
録音(ブラウザ) → NotebookLMへ音声を自動アップロード → 議事録を生成して返す

起動: python server.py  →  http://localhost:8765
事前に `notebooklm login` で認証しておくこと(README参照)
"""

import asyncio
import datetime
import pathlib
import subprocess
import sys
import tempfile

import httpx
import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from notebooklm import NotebookLMClient

BASE = pathlib.Path(__file__).parent
app = FastAPI(title="Gijiroku Server")

DEFAULT_PROMPT = """このソースの音声内容をもとに、以下の形式で議事録を作成してください。

# 議事録
## 会議概要
## 議題と議論の要点
## 決定事項
## TODO・アクションアイテム(担当者・期限が分かれば明記)
## 次回への持ち越し事項

聞き取りにくい箇所は文脈から自然に補ってください。"""


def convert_to_m4a(src: pathlib.Path) -> pathlib.Path:
    """webm等をNotebookLM対応のm4a(AAC)に変換。ffmpegが無ければそのまま返す。"""
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return src
    dst = src.with_suffix(".m4a")
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "128k", str(dst)],
        capture_output=True,
    )
    return dst if result.returncode == 0 and dst.exists() else src


def auth_error(e: Exception) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=f"NotebookLM認証エラー: {e} — 画面の「再認証」ボタンから再ログインしてください。",
    )


# ---- 認証(Web画面から完結できるようにする) ----

NOTEBOOKLM_CLI = str(pathlib.Path(sys.executable).parent / "notebooklm")
_login_proc: "asyncio.subprocess.Process | None" = None


async def _run_cli(*args: str, timeout: float = 90.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        NOTEBOOKLM_CLI, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


@app.get("/api/auth/status")
async def auth_status():
    """保存済み認証の有無をチェック。ログイン進行中かどうかも返す。"""
    code, _ = await _run_cli("auth", "check", timeout=30.0)
    logging_in = _login_proc is not None and _login_proc.returncode is None
    return {"ok": code == 0, "logging_in": logging_in}


@app.post("/api/auth/refresh")
async def auth_refresh():
    """保存済みブラウザプロファイルでCookieを裏で更新(画面には何も出ない)。"""
    code, out = await _run_cli("auth", "refresh", timeout=120.0)
    return {"ok": code == 0, "detail": out[-500:]}


@app.post("/api/auth/login")
async def auth_login():
    """このMac上にログイン用ブラウザを開く。完了は /api/auth/status のポーリングで検知。"""
    global _login_proc
    if _login_proc is not None and _login_proc.returncode is None:
        return {"started": True, "already_running": True}
    _login_proc = await asyncio.create_subprocess_exec(
        NOTEBOOKLM_CLI, "login",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return {"started": True, "already_running": False}


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/notebooks")
async def list_notebooks():
    try:
        async with NotebookLMClient.from_storage() as client:
            nbs = await client.notebooks.list()
            return [{"id": nb.id, "title": nb.title} for nb in nbs]
    except Exception as e:
        raise auth_error(e)


@app.post("/api/upload")
async def upload(
    file: UploadFile,
    title: str = Form("会議"),
    notebook_id: str = Form(""),
    prompt: str = Form(""),
):
    today = datetime.date.today().strftime("%Y-%m-%d")
    source_title = f"{today} {title}"
    prompt = (prompt or DEFAULT_PROMPT).strip()

    # 一時保存
    suffix = pathlib.Path(file.filename or "rec.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        audio_path = pathlib.Path(tmp.name)

    # webm → m4a 変換(NotebookLMの対応形式に合わせる)
    if suffix == ".webm":
        audio_path = convert_to_m4a(audio_path)

    try:
        async with NotebookLMClient.from_storage(
            upload_timeout=httpx.Timeout(900.0), chat_timeout=600.0
        ) as client:
            # ノートブック決定(未指定なら新規作成)
            if notebook_id:
                nb_id = notebook_id
            else:
                nb = await client.notebooks.create(source_title)
                nb_id = nb.id

            # 音声をソースとしてアップロード → 解析完了まで待機
            src = await client.sources.add_file(nb_id, audio_path, title=source_title)
            await client.sources.wait_until_ready(nb_id, src.id, timeout=900.0)

            # 議事録生成(アップロードしたソースのみに限定して質問)
            result = await client.chat.ask(nb_id, prompt, source_ids=[src.id])
            minutes = result.answer

            # 議事録をノートとしても保存(失敗しても無視)
            try:
                await client.notes.create(nb_id, f"議事録: {source_title}", minutes)
            except Exception:
                pass

            return {
                "minutes": minutes,
                "notebook_id": nb_id,
                "notebook_url": f"https://notebooklm.google.com/notebook/{nb_id}",
                "source_title": source_title,
            }
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "auth" in msg.lower() or "login" in msg.lower() or "401" in msg:
            raise auth_error(e)
        raise HTTPException(status_code=500, detail=f"処理に失敗しました: {msg}")
    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    print("議事録レコーダー: http://localhost:8765 をブラウザで開いてください")
    uvicorn.run(app, host="127.0.0.1", port=8765)
