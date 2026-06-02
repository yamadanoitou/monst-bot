# QUEUE.md — タスク待ち行列

> 3エージェント共有。`AGENTS.md` のライフサイクル §4 に従って claim → in_progress → done。
> 完了タスクは削除せず DONE セクションへ移動する。

形式:
```
### #<番号> [P0|P1|P2|P3] <タイトル>
- owner: codex | claude | gemini
- status: open | in_progress | review | blocked | done
- accept: <受け入れ条件>
- blocked_by: <他タスク番号> または なし
- branch: <ブランチ名 or ->
- notes: <任意の補足>
```

**owner の意味:**
- `codex`: Codex CLI が実装。ローカルで PR まで持っていく
- `claude`: Claude Code が設計・レビュー・状態整理
- `gemini`: Claude Code が `gemini -p` で headless 実行して結果を `research/` に保存。複雑な対話が必要なときだけ伊藤さんが `agy` 対話モードに切り替える

優先度の意味:
- **P0**: これがないと撮りたい絵が成立しない（OCR・LLM事実根拠化）
- **P1**: 自律運用の安定性・効率に直結
- **P2**: 中期で必要、なくても今は止まらない
- **P3**: 仕上げ・最適化

---

## OPEN

### #1 [P0] ランクOCR実装
- owner: codex
- status: review
- accept: `monst_autonomous_agent.observe()` が返す `facts.rank` に現在ランクが整数で入る。`screens/` の rank 表示ROIを最低3パターン（home / quest start / result）で検証
- blocked_by: なし
- branch: `feat/ocr-rank` （実装済み、ROI 修正のため再 push 必要）
- notes: Codex が 2026-06-02 23:12 にROI再調整完了。Android ステータスバー時計を避け、home rank は中央ランク円 `x=430-630,y=155-320` / 数字 `x=500-575,y=235-285` を読む。`MONST_OCR_MAX_RANK=999` で時計などの大きすぎる誤値を拒否。実機 observe で `facts.rank == 3` を確認。手元素材は home のみで、quest start / result の追加実画像検証は未実施

### #2 [P0] スタミナOCR実装
- owner: codex
- status: review
- accept: `facts.stamina.current` と `facts.stamina.max` に整数。`stamina_full` 検出より優先して読み取る
- blocked_by: なし
- branch: `feat/ocr-stamina` （実装済み、ROI 修正のため再 push 必要）
- notes: Codex が 2026-06-02 23:12 にROI再調整完了。home stamina は左上オレンジカプセル `x=80-400,y=160-265` / tight `x=120-370,y=180-245` を読む。実機 observe で `facts.stamina = {current: 202, max: 101}` を確認。split fallback も左上カプセルに合わせて更新。手元素材は home のみで、stamina_out 実画像検証は未実施

### #3 [P0] OCRライブラリ比較リサーチ
- owner: gemini
- status: done
- accept: ✅ `research/2026-06_ocr_jp_digits_benchmark.md` 作成済み
- blocked_by: なし
- branch: `research/ocr-libs`
- notes: **結論: RapidOCR (onnx) 推奨**。CPU 150ms 前後、`pip install rapidocr_onnxruntime` のみ、メモリ ~150MB。Tesseract は軽量だが装飾フォントに弱く前処理必須、PaddleOCR は精度最高だが Windows で重い、EasyOCR は CPU で遅すぎる。詳細は research ファイル参照。#1 #2 (OCR 実装) はこれを受けて RapidOCR で進められる

### #4 [P1] Ollama keep_alive 永続化
- owner: codex
- status: done
- accept: ✅ `_call_local_llm()` payload に `keep_alive: "24h"` 配置確認・main へマージ済み
- blocked_by: なし
- branch: `feat/ollama-keep-alive` （main にマージ済み 2026-06-02 23:20）
- notes: 2026-06-02 23:20 main マージ完了。effect 計測（戦略ティック2回目以降のレイテンシ低下）は次のセッションで確認

### #5 [P1] 戦略ティック頻度設計
- owner: claude
- status: open
- accept: 戦略ティックを「毎サイクル」から「N回ごと or M分ごと」に下げる方針案を設計書増分として作成し、Codex 実装用の Issue 化
- blocked_by: なし
- branch: `docs/strategy-tick-cadence`
- notes: Reactor はキャッシュで動くので、LLM 戦略呼びは「エスカレーション・トリガー §3」に絞る。設計書 §3 の原則に戻す方向

### #6 [P1] daily_summary を memory に逆流させる設計
- owner: claude
- status: open
- accept: `daily_summary` が出した「今日のまとめ」を `autonomous_memory.json` の `learnings` に1〜3行の蒸留として書き戻す仕様を設計。Codex 実装用に Issue 化
- blocked_by: なし
- branch: `docs/memory-feedback-loop`
- notes: 「読み返すから学習してるAIに見える」の核（設計書 §5）。蒸留は GPT-OSS の同じセッションで JSON 出力させると省コスト

### #7 [P2] replay_touch を welcome 突破後に切り替える設計
- owner: claude
- status: open
- accept: 設計書 §6-2 の `replay_touch` 路線を、現状の `DEMO_BATTLE_SHOTS` と共存させる plan を起草。難関クエスト判定と切替トリガーの仕様を Codex に渡せる粒度まで落とす
- blocked_by: なし
- branch: `docs/replay-touch-integration`
- notes: 既存 `monst_rank_bot.record_touch` / `replay_touch` を使う前提。welcome quest 完了後の最初の壁で初発動する想定

### #8 [P2] 運極ターゲット候補リサーチ
- owner: gemini
- status: open
- accept: `research/2026-06_ungoku_starter_targets.md`。新規アカ・無課金・3〜6ヶ月運用前提で、初運極にすべき降臨キャラの候補3〜5体。各候補について「ドロップ元クエストの周回難易度」「現環境での有用性」「捨て垢でも作りやすいか」を列挙
- blocked_by: なし
- branch: `research/ungoku-targets`
- notes: モンスト Wiki / 攻略サイト / Reddit を漁る前提

### #9 [P2] ノマクエ「壁」判定基準リサーチ
- owner: gemini
- status: open
- accept: `research/2026-06_noma_wall_criteria.md`。ノマクエの章別難易度・推奨ランクの公式/非公式情報を整理。「何連敗で諦めて強化に回るか」の基準値を提案（連敗数・XP/スタミナ効率しきい値など）
- blocked_by: なし
- branch: `research/noma-wall`
- notes: 設計書 §12 オープンクエスチョンの一つ

### #10 [P3] 座標ハードコードのテンプレ巻き直し
- owner: codex
- status: open
- accept: `monst_autonomous_agent.candidate_taps_from_observation()` の welcome_* 系座標を、テンプレマッチで動的に発見する形に書き換え。welcome 突破後も使い回せるパターンにする
- blocked_by: #1, #2
- branch: `refactor/welcome-coords-to-template`
- notes: welcome 突破が先か巻き直しが先かは Codex 判断。welcome を抜けた直後にやるのが理想

### #11 [P3] runtime_policy.json と日誌4層の整理
- owner: claude
- status: open
- accept: `runtime_policy.json` を日誌4層（goal_ledger / strategy_log / learnings / facts）と整合させる。重複や役割の被りを洗い出して統合 or 分離方針を AGENTS.md または設計書に追記
- blocked_by: なし
- branch: `docs/policy-vs-journal`
- notes: 真実源が分散しはじめている問題への対処

### #12 [P2] 自動録画パイプライン ⭐ Codex 次の独立タスク
- owner: codex
- status: open
- accept: `monst_autonomous_agent.py` のセッション開始時に scrcpy をサブプロセスで起動し、Pixel 8a の画面を 1 時間ごとローテーションで mp4 録画する。出力先は環境変数 `MONST_RECORDINGS_DIR`（既定 `./recordings`）、保持日数は `MONST_RECORDINGS_RETENTION_DAYS`（既定 7 日）で指定。bot 停止時に scrcpy も停止する
- blocked_by: なし
- branch: `feat/auto-recording`
- notes: scrcpy 前提（`scoop install scrcpy` などで導入。未インストール時は起動時に警告だけ出して bot 本体は継続）。`recordings/` は重いので `.gitignore` に追加。ファイル名は `YYYY-MM-DD/HH-MM.mp4`。**録画失敗で bot 本体を落とさないこと**（録画は付帯機能）

---

## IN_PROGRESS

(なし)

---

## REVIEW

(なし)

---

## BLOCKED

(なし)

---

## DONE

- #3 [P0] OCRライブラリ比較リサーチ — 2026-06-02 完了。`research/2026-06_ocr_jp_digits_benchmark.md`。**結論: RapidOCR (onnx) 採用**。#1 #2 のブロッカー解除済み
- #4 [P1] Ollama keep_alive 永続化 — 2026-06-02 23:20 main へマージ。`_call_local_llm()` の payload に `keep_alive: "24h"` 配置。effect 計測は次回セッション
