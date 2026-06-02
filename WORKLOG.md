# WORKLOG.md — 追記式日誌

> 3エージェント共有。**追記のみ**。古いエントリは削除しない。
> エントリは時系列・降順（新しいものを下に追記）。
> 形式: `[YYYY-MM-DD HH:MM] [agent] action — detail`。

エスカレーションは `ESCALATE:` プレフィックスを付ける。Claude Code が拾って翌朝サマリに含める。

---

## 2026-06-02

[2026-06-02 21:35] [claude] init — AGENTS.md / QUEUE.md / WORKLOG.md 初版起草。Codex / agy / claude の3者契約として登録。伊藤さん承認後に運用開始。

[2026-06-02 21:50] [claude] compress — ドキュメント圧縮。`monst_autonomous_agent_design.md` と `HANDOVER.md` を `docs/archive/` へ退避。AGENTS.md §9「設計上の決定（不可侵）」を新設して、活用マップとバトル3段階を吸収。CLAUDE.md の参照も更新。ドキュメントは 4ファイル（CLAUDE / AGENTS / QUEUE / WORKLOG）に圧縮完了。

[2026-06-02 22:05] [claude] split — monst-bot を company-me から分離して https://github.com/yamadanoitou/monst-bot (public) に独立化。ローカル本体は `c:/Users/yamad/monst-bot/`。日次 routine (`trig_01QvJR6v7uRbUyRZDMR8F18e`, Sonnet 4.6, 22:00 JST, Gmail) も新 repo を参照。

[2026-06-02 22:20] [claude] contract-fix — agy `-p` の Windows stdout バグ ([gemini-cli issue #27466](https://github.com/google-gemini/gemini-cli/issues/27466)) を踏まえて contract を改訂。AGENTS.md §2 / §3 / §5 / 改訂履歴 を更新し、headless リサーチは `gemini -p`（Claude Code が Bash で直接実行）、対話用は `agy`（伊藤さんが手動）に役割分担。QUEUE.md の `owner: agy` を `owner: gemini` に統一。2026-06-18 の Antigravity 2.0 統合時に再評価。

[2026-06-02 22:25] [claude] codex-assignment — Codex の次の claim 候補を QUEUE #4 (Ollama keep_alive) に指名。**理由**: P0 OCR (#1 #2) は #3 (OCR ライブラリ比較) 完了待ちでブロック中。#4 は依存なしの P1 独立タスクで、#3 の結果を待つ間に進められる。**Codex への引き継ぎ**: 作業ディレクトリは `c:/Users/yamad/monst-bot/`（company-me/projects/monst-bot/ は廃止）。ブランチは `feat/ollama-keep-alive`。受け入れ条件は QUEUE #4 の accept 参照。完了後 #3 の結果を見て #1 / #2 へ進む。

[2026-06-02 22:32] [gemini] research-done #3 — Claude Code が `gemini -p` で OCR ライブラリ比較リサーチを実行・完了。`research/2026-06_ocr_jp_digits_benchmark.md` 保存。**結論: RapidOCR (onnxruntime) を採用**。CPU 150ms 前後、`pip install rapidocr_onnxruntime` のみで導入、メモリ ~150MB、装飾フォントにも強い。Tesseract は軽量だが前処理必須、PaddleOCR は Windows で重い、EasyOCR は CPU 推論が遅すぎる。**#1 #2 のブロッカー解除済み**。Codex は #4 を済ませてから #1 #2 に RapidOCR で着手する流れに更新。

[2026-06-02 22:29] [codex] claim #4 — `feat/ollama-keep-alive` ブランチを作成し、Ollama keep_alive 永続化タスクを開始。完了後に RapidOCR ベースで #1 → #2 の OCR 実装へ進む。

[2026-06-02 22:30] [codex] complete #4 — `_call_local_llm()` の Ollama payload に `keep_alive` を追加。既定値は `MONST_OLLAMA_KEEP_ALIVE=24h`。`python -m py_compile monst_autonomous_agent.py` と payload 捕捉テストで `keep_alive: "24h"` を確認。

[2026-06-02 21:34] [user] setup — Antigravity CLI (`agy`) 1.0.4 を `C:\Users\yamad\AppData\Local\agy\bin` にインストール。**ターミナル再起動 + 初回 Google サインインは伊藤さん側で実施待ち**。

[2026-06-02 21:30] [claude] review — `monst_autonomous_agent.py` が 606→1415 行に成長。新規追加：`screen_journal` 連携、LLM provider 抽象化（Ollama + OpenAI 互換）、`candidate_taps_from_observation`、`learn_step` / `propose_learning_tap`、`collect_teacher_demo`、`reactor_step` / `reactor_loop`、`runtime_policy.json`。設計書とのドリフト3点を指摘済み：(1) OCR が放置されたまま、(2) `replay_touch` 既存資産を使わず `DEMO_BATTLE_SHOTS` で別系統を立てた、(3) 座標ハードコードが welcome quest 用に膨らんだ。

[2026-06-02 21:15] [codex] feature-progress — welcome quest 突破用に `screen_journal.py`（556行、SQLite + perceptual hash + action attempts DB）と reactor cache 系を実装。welcome_home / welcome_info_dialog / welcome_quest_list / welcome_stage_panel / play_type_select / deck / battle_* の state ごとに座標候補リストをハードコード。教師デモ用 `DEMO_BATTLE_SHOTS` 3パターンも追加。**※このエントリは Claude Code が git 履歴から推定して記述。Codex が直接書いたものではない**。

---

## 運用メモ

- このファイルが200行を超えたら、Claude Code が古いエントリを `WORKLOG_archive_<YYYY-MM>.md` に切り出す
- 日次サマリは Claude Code が `/schedule` ルーティンで生成する予定（合意後）
- エージェント間の同期問題が起きたらこのファイルの直近10行を見て調整
