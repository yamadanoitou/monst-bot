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

[2026-06-02 22:29] [codex] claim #4 — `feat/ollama-keep-alive` ブランチを作成し、Ollama keep_alive 永続化タスクを開始。

[2026-06-02 22:30] [codex] complete #4 — `_call_local_llm()` の Ollama payload に `keep_alive` を追加。既定値は `MONST_OLLAMA_KEEP_ALIVE=24h`。`python -m py_compile monst_autonomous_agent.py` と payload 捕捉テストで `keep_alive: "24h"` を確認。

[2026-06-02 22:31] [codex] claim #1 — `feat/ocr-rank` ブランチを作成し、RapidOCR ベースの rank OCR 実装を開始。research の Implementation hint を参照済み。

[2026-06-02 22:35] [codex] complete #1-impl — `observe()` に RapidOCR rank 抽出を追加。`facts.rank` と `facts.ocr.rank` に値/ROI/raw text/score/status を保存。`requirements.txt` に `rapidocr-onnxruntime>=1.4.4` を追加。`python -m py_compile monst_autonomous_agent.py`、`observe --dry-run`、fake OCR helper test は通過。実機スクショ3パターン検証は未実施。

[2026-06-02 22:36] [codex] claim #2 — `feat/ocr-stamina` ブランチを作成し、RapidOCR ベースの stamina current/max OCR 実装を開始。

[2026-06-02 22:38] [codex] complete #2-impl — `observe()` に RapidOCR stamina 抽出を追加。`facts.stamina.current/max` と `facts.ocr.stamina` に値/ROI/raw text/score/status を保存。full ROI の `45/120` パースと左右分割 fallback を実装。`python -m py_compile monst_autonomous_agent.py`、`observe --dry-run`、fake OCR helper test は通過。実機ROI微調整は未実施。

[2026-06-02 22:50] [claude] goal-axis — 伊藤さん発言「ゴール自体は機能として、かな。どう面白くするかは私の腕前」を受けて方針確認。**機能完成が一義**、コンテンツ加工は伊藤さんの腕。Claude は「撮れる絵」根拠で設計判断を引っ張らない。自動録画は機能として歓迎されたため QUEUE #12 を起票。

[2026-06-02 22:52] [claude] new-task #12 — 自動録画パイプライン (scrcpy ベース) を QUEUE に追加。`MONST_RECORDINGS_DIR` / `MONST_RECORDINGS_RETENTION_DAYS` 環境変数で保存先と保持日数を制御。録画失敗で bot を落とさない方針を accept に明記。Codex の次の独立タスク候補。

[2026-06-02 23:00] [codex] heads-up — Codex はブランチ切ったまま 3 タスク連続で実装 (feat/ollama-keep-alive → feat/ocr-rank → feat/ocr-stamina) を stacking 形式で完成させた。

[2026-06-02 23:05] [user] device-access — 伊藤さん発言「実機検証は、基本的に常にPixel8aは繋ぎっぱなしにしてモンストのホーム画面開いてるから別に自由にやってもらってかまわない」。**Codex / Claude Code は ADB を自由に叩いてよい**（observe / 画面キャプチャ系は無条件、タップ系は伊藤さん不在時は避ける）。クラウド routine は引き続き実機にアクセス不可。

[2026-06-02 23:10] [claude] adb-setup — ぼくの shell に adb がなかったため `winget install Google.PlatformTools` で platform-tools をインストール。`adb devices` で Pixel 8a `47021JEKB21521 device` 認識確認。`ADB_PATH` 経由で agent コードから呼べる状態。

[2026-06-02 23:15] [claude] review-fail #1 #2 — feat/ocr-stamina ブランチで `python monst_autonomous_agent.py observe` を実機実行。**結果**: `screen_state=home` 取得 OK、`facts.rank=2259`（Android ステータスバーの時計 "22:59" を誤読）、`facts.stamina={current:None, max:None}`（候補ゼロ）。Codex の ROI 座標が実機の Monst UI に合っていない。実画像（`screens/journal/20260602_225933_home.png`）で確認した実値: ランク=3、スタミナ=202/101。**#1 #2 を review → open に差し戻し**。推奨 ROI と再 observe 期待値を QUEUE.md の各 notes に記載。Codex は実画像を Read して ROI 詰め直し → 再 push。

[2026-06-02 23:20] [claude] merge-pass #4 — `feat/ollama-keep-alive` を main へ no-ff マージ。AGENTS.md §3「差分100行未満・テスト緑なら省略可」適用。QUEUE #4 を DONE へ移動。

[2026-06-02 23:30] [claude] review-pass + merge #1 #2 — Codex の ROI 再調整 (d89b022) を独立 observe で検証。`facts.rank=3`（4 ROI 全部 score 1.00）、`facts.stamina={current:202, max:101}`。**実機検証パス**。`feat/ocr-stamina` を main へ no-ff マージ（stacked branch なので #1 の `feat/ocr-rank` も同時に取り込まれる）。QUEUE #1 #2 を DONE へ移動。**P0 三本（#1 #2 #3）すべて完了**。次の Codex の自然な claim は #12 自動録画 or #10 座標ハードコード巻き直し。

[2026-06-02 23:22] [claude] e2e-first-run — `python monst_autonomous_agent.py once startup` を実機実行。**パイプライン自体は通過**: observe (rank=3, stamina={202,101}) → Ollama qwen3.5:9b 呼び出し → JSON 応答 → directive parse → execute → return。所要 ~100秒（うち 101秒は wait_stamina の sleep）。**ただし LLM の戦略判断にバグ**: `current=202 max=101` を「上限超過」と解釈して `wait_stamina` を選んだ（実際は豊富にあるからすぐ farm すべき）。reasoning は「スタミナが 101 の上限を超えているため wait_stamina」。原因は `hard_rules` の wait_stamina 発動条件が曖昧（"If stamina is out, choose wait_stamina" だけ）。**QUEUE #13 を新規 P0 で起票**してオーバーキャップ時の挙動を明文化させる。それまで autopilot は farm_quest を自力で選べないので寝かせる。

[2026-06-02 23:08] [codex] reclaim #1 #2 — Claude Code の実機 review-fail を受け、`feat/ocr-stamina` に main を merge して ROI 再調整を開始。対象スクショは `screens/journal/20260602_225933_home.png`。

[2026-06-02 23:12] [codex] complete #1 #2 retry — 実画像 `screens/journal/20260602_225933_home.png` と実機 observe でROI再調整。rank は中央ランク円、stamina は左上オレンジカプセルへ変更。`MONST_OCR_MAX_RANK=999` で時計誤読を拒否。`ADB_PATH=...platform-tools\adb.exe python monst_autonomous_agent.py observe` で `facts.rank=3` / `facts.stamina={current:202,max:101}` を確認。

[2026-06-02 21:34] [user] setup — Antigravity CLI (`agy`) 1.0.4 を `C:\Users\yamad\AppData\Local\agy\bin` にインストール。**ターミナル再起動 + 初回 Google サインインは伊藤さん側で実施待ち**。

[2026-06-02 21:30] [claude] review — `monst_autonomous_agent.py` が 606→1415 行に成長。新規追加：`screen_journal` 連携、LLM provider 抽象化（Ollama + OpenAI 互換）、`candidate_taps_from_observation`、`learn_step` / `propose_learning_tap`、`collect_teacher_demo`、`reactor_step` / `reactor_loop`、`runtime_policy.json`。設計書とのドリフト3点を指摘済み：(1) OCR が放置されたまま、(2) `replay_touch` 既存資産を使わず `DEMO_BATTLE_SHOTS` で別系統を立てた、(3) 座標ハードコードが welcome quest 用に膨らんだ。

[2026-06-02 21:15] [codex] feature-progress — welcome quest 突破用に `screen_journal.py`（556行、SQLite + perceptual hash + action attempts DB）と reactor cache 系を実装。welcome_home / welcome_info_dialog / welcome_quest_list / welcome_stage_panel / play_type_select / deck / battle_* の state ごとに座標候補リストをハードコード。教師デモ用 `DEMO_BATTLE_SHOTS` 3パターンも追加。**※このエントリは Claude Code が git 履歴から推定して記述。Codex が直接書いたものではない**。

---

## 運用メモ

- このファイルが200行を超えたら、Claude Code が古いエントリを `WORKLOG_archive_<YYYY-MM>.md` に切り出す
- 日次サマリは Claude Code が `/schedule` ルーティンで生成する予定（合意後）
- エージェント間の同期問題が起きたらこのファイルの直近10行を見て調整
