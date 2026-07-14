#!/bin/bash
# 議事録レコーダー セットアップ(初回のみ実行)
set -e
cd "$(dirname "$0")"

echo "== 🎙️ 議事録レコーダー セットアップ =="

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 が見つかりません。https://www.python.org/downloads/ からインストールしてから再実行してください。"
  exit 1
fi

echo "[1/3] Python仮想環境を作成中..."
python3 -m venv .venv

echo "[2/3] ライブラリをインストール中...(数分かかります)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet "notebooklm-py[browser]" fastapi uvicorn python-multipart imageio-ffmpeg firebase-admin

echo "[3/3] ログイン用ブラウザをダウンロード中..."
.venv/bin/playwright install chromium

echo ""
echo "✅ セットアップ完了!"
echo "   ./start.sh で起動してください。"
