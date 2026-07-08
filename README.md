# 議事録レコーダー(録音 → NotebookLM → 議事録 全自動)

ブラウザで録音 → 音声ファイルをNotebookLMに自動アップロード → 議事録を生成して画面に表示、まで全自動。

## 仕組み

- 非公式ライブラリ [notebooklm-py](https://github.com/teng-lin/notebooklm-py) でNotebookLMの内部APIを操作
- 認証はブラウザログインのCookieを再利用(学校アカウントでログイン)
- **注意**: 非公式APIのため、Googleの仕様変更で突然動かなくなる可能性・利用規約上のリスクがあります。自己責任・個人利用の範囲で。

## セットアップ(初回のみ)

```bash
cd gijiroku-server

# 1. 仮想環境を作って依存をインストール
python3 -m venv .venv
source .venv/bin/activate
pip install "notebooklm-py[browser]" fastapi uvicorn python-multipart imageio-ffmpeg

# 2. NotebookLMに認証(ブラウザが開くので学校アカウントでログイン)
notebooklm login
notebooklm auth check --test   # "status": "ok" ならOK

# すでにChromeで学校アカウントにログイン済みなら、こちらでも可:
# notebooklm login --browser-cookies chrome
```

## 使い方(毎回)

```bash
cd gijiroku-server
source .venv/bin/activate
python server.py
```

→ ブラウザで **http://localhost:8765** を開く

1. 会議名を入力し、アップロード先ノートブックを選択(新規作成も可)
2. 「録音開始」→ 会議 → 「録音停止 & 送信」
3. 自動で NotebookLM に音声がアップロードされ、議事録が画面に表示される
   - 議事録はノートブック内の「ノート」も自動保存
   - 録音ファイルは万一に備えてローカルにも自動ダウンロードされる

## トラブルシューティング

- **401 / 認証エラー**: 画面に出る「🔑 再認証」ボタンを押すだけでOK。まず裏でCookieを更新し、ダメならログイン用ブラウザが自動で開きます(ターミナル操作は不要)
- **アップロードが遅い/タイムアウト**: 長時間録音は処理に数分かかります。15分でタイムアウトする設計です
- **Cookieがすぐに切れて煩わしい**: `notebooklm auth refresh --quiet` を cron / launchd に登録すると維持できます
- **議事録の形式を変えたい**: 画面の「議事録プロンプトをカスタマイズ」を編集(自動保存されます)
