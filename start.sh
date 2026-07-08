#!/bin/bash
# 議事録レコーダー 起動(毎回これだけ)
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "先に ./setup.sh を実行してください(初回のみ)"
  exit 1
fi

# 1秒後にブラウザを自動で開く(サーバー起動を待つ)
( sleep 1; open "http://localhost:8765" ) &

.venv/bin/python server.py
