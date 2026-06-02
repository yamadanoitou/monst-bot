# WORKLOG.md — 追記式日誌

> 3エージェント共有。**追記のみ**。古いエントリは削除しない。
> エントリは時系列・降順（新しいものを下に追記）。
> 形式: `[YYYY-MM-DD HH:MM] [agent] action — detail`。

エスカレーションは `ESCALATE:` プレフィックスを付ける。Claude Code が拾って翌朝サマリに含める。

---

## 2026-06-02

[2026-06-02 21:35] [claude] init — AGENTS.md / QUEUE.md / WORKLOG.md 初版起草。Codex / agy / claude の3者契約として登録。伊藤さん承認後に運用開始。

[2026-06-02 21:50] [claude] compress — ドキュメント圧縮。`monst_autonomous_agent_design.md` と `HANDOVER.md` を `docs/archive/` へ退避。AGENTS.md §9「設計上の決定（不可侵）」を新設して、活用マップとバトル3段階を吸収。CLAUDE.md の参照も更新。ドキュメントは 4ファイル（CLAUDE / AGENTS / QUEUE / WORKLOG）に圧縮完了。

[2026-06-02 21:34] [user] setup — Antigravity CLI (`agy`) 1.0.4 を `C:\Users\yamad\AppData\Local\agy\bin` にインストール。**ターミナル再起動 + 初回 Google サインインは伊藤さん側で実施待ち**。

[2026-06-02 21:30] [claude] review — `monst_autonomous_agent.py` が 606→1415 行に成長。新規追加：`screen_journal` 連携、LLM provider 抽象化（Ollama + OpenAI 互換）、`candidate_taps_from_observation`、`learn_step` / `propose_learning_tap`、`collect_teacher_demo`、`reactor_step` / `reactor_loop`、`runtime_policy.json`。設計書とのドリフト3点を指摘済み：(1) OCR が放置されたまま、(2) `replay_touch` 既存資産を使わず `DEMO_BATTLE_SHOTS` で別系統を立てた、(3) 座標ハードコードが welcome quest 用に膨らんだ。

[2026-06-02 21:15] [codex] feature-progress — welcome quest 突破用に `screen_journal.py`（556行、SQLite + perceptual hash + action attempts DB）と reactor cache 系を実装。welcome_home / welcome_info_dialog / welcome_quest_list / welcome_stage_panel / play_type_select / deck / battle_* の state ごとに座標候補リストをハードコード。教師デモ用 `DEMO_BATTLE_SHOTS` 3パターンも追加。**※このエントリは Claude Code が git 履歴から推定して記述。Codex が直接書いたものではない**。

---

## 運用メモ

- このファイルが200行を超えたら、Claude Code が古いエントリを `WORKLOG_archive_<YYYY-MM>.md` に切り出す
- 日次サマリは Claude Code が `/schedule` ルーティンで生成する予定（合意後）
- エージェント間の同期問題が起きたらこのファイルの直近10行を見て調整
