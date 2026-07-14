"""
議事録レコーダー ローカルサーバー
録音(ブラウザ) → NotebookLMへ音声を自動アップロード → 議事録を生成して返す

起動: python server.py  →  http://localhost:8765
事前に `notebooklm login` で認証しておくこと(README参照)
"""

import asyncio
import datetime
import json
import mimetypes
import pathlib
import subprocess
import sys
import tempfile

import httpx
import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
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


# ---- Firebase(録音・議事録の共有保存 + 管理画面) ----
# フォルダ直下に firebase-key.json(サービスアカウント鍵)を置くと有効になる。
# 無ければこの機能は静かにオフになり、アプリ本体はこれまで通り動く。

FIREBASE_KEY = BASE / "firebase-key.json"


def fb_ready() -> bool:
    try:
        import firebase_admin
        if firebase_admin._apps:
            return True
        if not FIREBASE_KEY.exists():
            return False
        from firebase_admin import credentials, storage

        info = json.loads(FIREBASE_KEY.read_text())
        cred = credentials.Certificate(str(FIREBASE_KEY))
        pid = info["project_id"]
        firebase_admin.initialize_app(cred, {"storageBucket": f"{pid}.firebasestorage.app"})
        # 新形式バケットが無い古いプロジェクトは appspot.com に切り替え
        try:
            if not storage.bucket().exists():
                firebase_admin.delete_app(firebase_admin.get_app())
                firebase_admin.initialize_app(cred, {"storageBucket": f"{pid}.appspot.com"})
        except Exception:
            pass
        return True
    except Exception:
        return False


def _fb_save(audio_path: pathlib.Path, meta: dict) -> str:
    """音声をStorageへ、議事録+メタ情報をFirestoreへ保存してドキュメントIDを返す。"""
    from firebase_admin import firestore, storage

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = meta["source_title"].replace("/", "_")
    blob_path = f"recordings/{safe_title}_{ts}{audio_path.suffix}"
    blob = storage.bucket().blob(blob_path)
    blob.upload_from_filename(
        str(audio_path),
        content_type=mimetypes.guess_type(audio_path.name)[0] or "audio/mp4",
    )

    db = firestore.client()
    doc = db.collection("meetings").document()
    doc.set({
        "title": meta["title"],
        "source_title": meta["source_title"],
        "recorder": meta["recorder"],
        "minutes": meta["minutes"],
        "notebook_id": meta["notebook_id"],
        "notebook_url": meta["notebook_url"],
        "audio_path": blob_path,
        "duration_sec": meta["duration_sec"],
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    return doc.id


def _require_fb():
    if not fb_ready():
        raise HTTPException(
            status_code=503,
            detail="Firebase未設定です。firebase-key.json をこのフォルダに置いてください(README参照)。",
        )


@app.get("/api/firebase/status")
async def firebase_status():
    return {"configured": fb_ready()}


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "static" / "admin.html")


@app.get("/api/meetings")
async def meetings_list():
    _require_fb()

    def _list():
        from firebase_admin import firestore
        db = firestore.client()
        docs = (
            db.collection("meetings")
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(300)
            .stream()
        )
        out = []
        for d in docs:
            x = d.to_dict() or {}
            created = x.get("created_at")
            out.append({
                "id": d.id,
                "title": x.get("title", ""),
                "recorder": x.get("recorder", ""),
                "duration_sec": x.get("duration_sec", 0),
                "created_at": created.isoformat() if created else None,
                "notebook_url": x.get("notebook_url", ""),
            })
        return out

    return await asyncio.to_thread(_list)


@app.get("/api/meetings/{meeting_id}")
async def meeting_detail(meeting_id: str):
    _require_fb()

    def _get():
        from firebase_admin import firestore
        d = firestore.client().collection("meetings").document(meeting_id).get()
        if not d.exists:
            raise HTTPException(status_code=404, detail="会議が見つかりません")
        x = d.to_dict() or {}
        created = x.get("created_at")
        x["created_at"] = created.isoformat() if created else None
        x["id"] = d.id
        return x

    return await asyncio.to_thread(_get)


@app.get("/api/meetings/{meeting_id}/audio")
async def meeting_audio(meeting_id: str):
    _require_fb()

    def _open():
        from firebase_admin import firestore, storage
        d = firestore.client().collection("meetings").document(meeting_id).get()
        if not d.exists:
            raise HTTPException(status_code=404, detail="会議が見つかりません")
        path = (d.to_dict() or {}).get("audio_path")
        if not path:
            raise HTTPException(status_code=404, detail="音声がありません")
        blob = storage.bucket().blob(path)
        if not blob.exists():
            raise HTTPException(status_code=404, detail="音声ファイルが見つかりません")
        return blob.open("rb"), mimetypes.guess_type(path)[0] or "audio/mp4"

    fh, media_type = await asyncio.to_thread(_open)
    return StreamingResponse(fh, media_type=media_type)


@app.delete("/api/meetings/{meeting_id}")
async def meeting_delete(meeting_id: str):
    _require_fb()

    def _delete():
        from firebase_admin import firestore, storage
        ref = firestore.client().collection("meetings").document(meeting_id)
        d = ref.get()
        if not d.exists:
            raise HTTPException(status_code=404, detail="会議が見つかりません")
        path = (d.to_dict() or {}).get("audio_path")
        if path:
            try:
                storage.bucket().blob(path).delete()
            except Exception:
                pass
        ref.delete()

    await asyncio.to_thread(_delete)
    return {"deleted": True}


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
    recorder: str = Form(""),
    duration_sec: float = Form(0),
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

            notebook_url = f"https://notebooklm.google.com/notebook/{nb_id}"

            # Firebaseへ共有保存(未設定・失敗でも本体の結果は返す)
            firebase_id = None
            firebase_error = None
            if fb_ready():
                try:
                    firebase_id = await asyncio.to_thread(_fb_save, audio_path, {
                        "title": title,
                        "source_title": source_title,
                        "recorder": recorder,
                        "minutes": minutes,
                        "notebook_id": nb_id,
                        "notebook_url": notebook_url,
                        "duration_sec": duration_sec,
                    })
                except Exception as e:
                    firebase_error = str(e)[:300]

            return {
                "minutes": minutes,
                "notebook_id": nb_id,
                "notebook_url": notebook_url,
                "source_title": source_title,
                "firebase_saved": firebase_id is not None,
                "firebase_id": firebase_id,
                "firebase_error": firebase_error,
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
