# monst-bot — モンスト ノマクエ放置周回

> 3エージェント自律運用契約は `AGENTS.md`、タスク待ちは `QUEUE.md`、日誌は `WORKLOG.md`。
> 旧設計書 / 旧引継書は `docs/archive/` 配下に退避済み（参照のみ）。
> このファイルは常設ルールのみ。

## ゴール

実機 Pixel 8a を ADB 経由で操作し、モンストのノマクエを無人で安定周回する。
**完全カンストは目指さない**（スタミナがスループット上限）。
目標は「手を触れずにランクを淡々と上げ続けるbotが落ちずに回ること」。

## 技術スタック

| レイヤー | 採用 |
|---------|------|
| 言語 | Python 3.12 |
| 画像認識 | OpenCV (テンプレマッチ) |
| 端末操作 | ADB (`input tap` / `input swipe` / `screencap`) |
| 端末 | Pixel 8a (1080x2400) USB接続 / root不要 |

## ディレクトリ構成

```
monst-bot/
├── monst_rank_bot.py    本体。CLI: shot / run / templates / calibrate
├── monst_autonomous_agent.py
│                         自律エージェント層。戦略ティック / 指令JSON / 日誌4層
├── screen_journal.py    観測 RAG。SQLite + phash + action attempts
├── AGENTS.md            3エージェント契約 + 設計上の決定（不可侵）
├── QUEUE.md             タスク待ち行列
├── WORKLOG.md           追記式日誌
├── docs/archive/        旧設計書・旧引継書（参照のみ）
├── requirements.txt
├── templates/           判定用テンプレ画像 (要採取・git管理外)
├── screens/             screen.png / unknown_*.png (git管理外)
└── logs/                実行ログ (git管理外)
```

## 設計上の鉄則 (HANDOVER.md §2 から抜粋)

1. バトル中の操作は**ランダム方向フリック連打**。座標決め打ち狙撃はしない（脆い）。
2. **エミュレータ禁止 / root化禁止 / メモリ改変禁止**。入力は ADB 正規経路のみ。
3. スタミナ切れは**無料回復待ち**で対応。**オーブ・林檎は絶対自動消費しない**。
4. 検出対策のため、遅延・座標ジッターのランダム性は必ず維持。機械的にしすぎない。
5. ボタンはテンプレで探し、座標決め打ちは最小限。

## 開発ループ

1. 端末でテストしたい画面を出す
2. `python monst_rank_bot.py shot` で `screens/screen.png` を採取
3. その画像から固有要素を切り出して `templates/<name>.png` に保存
4. `python monst_rank_bot.py templates` で整備状況確認
5. `python monst_rank_bot.py run` で挙動確認、止まった画面を `screens/unknown_*` に残させる
6. 未知画面のテンプレを足す → 1 に戻る

## 自律エージェントモード

`monst_autonomous_agent.py` が自律エージェントの v1 ハーネス。
設計上の決定は `AGENTS.md §9` に集約済み（旧設計書は `docs/archive/` へ退避）。

```bash
python monst_autonomous_agent.py once manual --dry-run --no-llm
python monst_autonomous_agent.py observe --dry-run
python monst_autonomous_agent.py once startup
python monst_autonomous_agent.py autopilot
python monst_autonomous_agent.py daily-summary
python monst_autonomous_agent.py learn-tap 540 1040 welcome icon
python monst_autonomous_agent.py learn-step
python monst_autonomous_agent.py collect-demo 12
python monst_autonomous_agent.py reactor-step
python monst_autonomous_agent.py reactor-loop 40 --llm-on-unknown
```

- `logs/autonomous_memory.json`: 目標台帳 / 戦略ログ / 学び / facts。
- `logs/director_events.jsonl`: Director 用の判断・実行イベント。
- `logs/last_strategy_prompt.json`: 直近の Ollama 入力確認用。
- `logs/daily_summary_YYYY-MM-DD.md`: `gpt-oss:20b` で生成する一日のまとめ。
- `logs/observation_journal.sqlite3`: 画面、類似画面、タップ試行、LLM判断を蓄積する観測RAG DB。
- LLM は `MONST_OLLAMA_URL` と `MONST_STRATEGY_LLM_MODEL` / `MONST_SUMMARY_LLM_MODEL` で差し替え可能。
- 戦略モデル既定は `qwen3.5:9b`。調査時点で `qwen3.6:9b` は Ollama 公式タグとして存在せず、`qwen3.6:9b` の pull は `file does not exist`。
- 日誌まとめモデル既定は `gpt-oss:20b`。
- OpenAI互換のローカルサーバーを使う場合は `MONST_LLM_PROVIDER=openai`、
  `MONST_LLM_URL=http://localhost:1234/v1/chat/completions`、
  `MONST_STRATEGY_LLM_MODEL=<local-strategy-model-name>`、
  `MONST_SUMMARY_LLM_MODEL=<local-summary-model-name>` を指定する。
- デフォルトはローカルLLM必須。Ollama 未接続時は勝手に周回せず停止し、`last_strategy_error` をメモリへ残す。
- `--no-llm` はテスト専用。`--allow-fallback` を付けた時だけ Ollama 失敗時に内蔵フォールバック戦略へ逃がす。
- 現アカウントでプレイ可能なのはウェルカムクエストのみ。`welcome_quest` を `super_shortcut` ルートで実行する。
- ノマクエ・降臨・編成変更は、ウェルカムクエスト後に解放状態を facts で確認してから追加する。
- 画面操作を覚えさせるときは `learn-tap` / `learn-step` / `collect-demo` を使い、before/after スクショと座標結果を必ずDBに残す。
- 初期学習中はテンポ優先で `collect-demo` を使う。Qwen を毎手呼ばず、教師デモ操作を高速に集めてからRAG材料として渡す。
- 実行時の基本は `reactor-step` / `reactor-loop`。既知状態はCVリアクターが方針キャッシュを即実行し、未知画面だけ `--llm-on-unknown` でQwenに渡す。
- 操作主体は「ローカルLLMが所有する方針」。CVリアクターはその方針キャッシュを低レイテンシで実行する手足であり、未知画面の新判断はQwenへ戻す。
- `logs/runtime_policy.json`: 方針所有者、実行者、未知画面ポリシーを明示する。今後Qwenによるルール更新/承認ログの受け皿にする。
- バトル判定はテンプレだけに寄せない。`monst_rank_bot.py` は HPバーの緑、下部BATTLE UIの橙、右下メニューの青をROIで複合判定し、`battle` state を返す。
- よほど難しいクエストでなければ、バトル中は安全ゾーン内の雑ショットでよい。精密狙撃より「止まらずクリアし、判断材料を貯める」を優先する。

## やらないこと

- root化 / APK改造 / メモリ書き換え / パケット改ざん
- エミュレータ前提への切り替え
- 入力の機械的最適化（一定間隔ピッタリ連打など）
- 課金アイテムの自動消費
- 公開リポジトリへのテンプレ画像コミット（ゲーム画面なので `.gitignore` 済み）

## 完了の定義

- 無人で数時間以上、周回→クリア→もう一度 のループが継続する
- スタミナ切れで停止せず、待機→自動再開できる
- 想定外の画面で無限ループ/クラッシュせず、ログに状況が残る

## 関連メモリ

- このプロジェクトは捨て垢運用前提、BANリスク承知の上で進めている。
- 規約上の自動操作禁止リスクは運用者が負う。
