"""
議事録レコーダー ローカルサーバー
録音(ブラウザ) → NotebookLMへ音声を自動アップロード → 議事録を生成して返す

起動: python server.py  →  http://localhost:8765
事前に `notebooklm login` で認証しておくこと(README参照)
"""

import datetime
import pathlib
import subprocess
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
        detail=f"NotebookLM認証エラー: {e} — ターミナルで `notebooklm login` を実行して再認証してください。",
    )


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
