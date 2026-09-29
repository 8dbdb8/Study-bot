# StudyBot

StudyBotは、Discordのボイスチャンネル滞在時間と資格学習の結果を記録する個人用Botです。

現在は次の資格取得ロードマップを想定しています。

1. 情報セキュリティマネジメント（SG）
2. 基本情報技術者試験（FE）
3. 医療情報技師

SGでは、過去問道場の問題数・正答率・分野を記録し、累計成績、要復習候補、学習計画を確認できます。

## 主な機能

- ボイスチャンネル「勉強部屋」の滞在時間を自動記録
- テキストチャンネル「勉強ログ」の投稿を学習ログとして保存
- `/sglog` からSG過去問道場の問題数・正答率・分野を正確に登録
- `/status` でSGの累計成績を表示
- `/plan` でSG合格までの週次学習計画を作成
- Ollamaによる学習ログ分析と次回メニューの提案
- SQLiteによるローカル保存

## 動作環境

- Windows 10またはWindows 11
- Python 3.11以降
- Discord Botとそのトークン
- Ollama（AI分析機能を使う場合）
- Ollamaモデル `qwen3:8b`（AI分析機能を使う場合）

Gitは更新時に便利ですが、ZIPで保存した場合は必須ではありません。

## 1. Discord Botを準備する

1. [Discord Developer Portal](https://discord.com/developers/applications) でApplicationを作成します。
2. `Bot` 画面からBotを追加し、トークンを取得します。
3. `Privileged Gateway Intents` の `Message Content Intent` を有効にします。
4. `OAuth2` のURL Generatorで `bot` と `applications.commands` を選択します。
5. Botに `View Channels`、`Send Messages`、`Read Message History` の権限を付けてサーバーへ招待します。

トークンはパスワードと同じ扱いです。他人へ渡したり、GitHubへ登録したりしないでください。

## 2. Discordサーバーにチャンネルを作る

次の名前でチャンネルを作成します。

- ボイスチャンネル：`勉強部屋`
- テキストチャンネル：`勉強ログ`

Botはこの名前を使って対象チャンネルを判定します。

## 3. StudyBotを保存する

Gitを使用する場合：

```powershell
git clone https://github.com/8dbdb8/Study-bot.git
cd Study-bot
```

Gitを使用しない場合は、GitHubの `Code` からZIPを保存して展開します。

## 4. 初回セットアップを実行する

展開したフォルダ内の `setup_studybot.bat` をダブルクリックします。

このスクリプトは次の処理を行います。

- `.venv` にPython仮想環境を作成
- `requirements.txt` から必要なライブラリを導入
- `data` フォルダを作成
- `.env.example` から `.env` を作成
- Ollamaが導入済みなら `qwen3:8b` を準備

既存の `.env` は上書きしません。

## 5. Discordトークンを設定する

セットアップ後に `.env` をテキストエディタで開き、次の値を実際のBotトークンへ置き換えます。

```dotenv
DISCORD_TOKEN=replace_with_your_discord_bot_token
```

引用符は不要です。`.env` は `.gitignore` に登録されているため、GitHubには送信されません。

## 6. Ollamaを準備する

AI分析機能を使う場合は、[Ollama for Windows](https://ollama.com/download/windows) を導入します。

セットアップ時にモデルを取得できなかった場合は、PowerShellで次を実行します。

```powershell
ollama pull qwen3:8b
```

Ollamaがなくても `/sglog`、学習時間、DB保存などの基本機能は利用できます。AIを使用するコマンドだけ利用できなくなります。

## 7. Botを起動する

`start_studybot.bat` をダブルクリックします。

起動スクリプトは自身が置かれているフォルダを自動的に使用するため、任意の保存場所で動作します。ログは `studybot.log` に保存されます。

起動後、Discordで `/hello` を実行して応答を確認してください。

## SG過去問道場の記録

Discordで `/sglog` を実行します。

1. 学習した分野を1つ選択します。
2. `問題数と正答率を入力` を押します。
3. 問題数、正答率、任意のメモを入力します。

正答率は小数第2位を四捨五入し、小数第1位で保存されます。複数分野を解いた場合は分野ごとに登録してください。

## データの保存場所

- 学習データ：`data/study.db`
- 実行ログ：`studybot.log`
- Discordトークン：`.env`

これらはGitHubへ送信されません。PCを移行する場合、過去の学習履歴を引き継ぐには `data/study.db` を別途バックアップしてください。

## 更新方法

Gitで保存した場合は、Botを終了してからプロジェクトフォルダで次を実行します。

```powershell
git pull --ff-only
.\setup_studybot.bat
```

その後、`.\start_studybot.bat` で再起動します。セットアップを再実行しても既存の `.env` とDBは維持されます。

## テスト

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## セキュリティ上の注意

- `.env` やDiscord BotトークンをGitへ追加しないでください。
- `data/study.db` には学習履歴が入るため、公開しないでください。
- `studybot.log` を共有するときは、内容を確認してください。
- トークンが漏れた場合はDiscord Developer Portalですぐに再発行してください。
