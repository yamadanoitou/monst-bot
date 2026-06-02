#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自律モンストエージェント v1 harness.

`monst_rank_bot.py` を ADB/OpenCV の「手」として使い、ここでは設計書の
戦略ティック / 指令JSON / 日誌4層 / 実行ティックを束ねる。

CLI:
  python monst_autonomous_agent.py once [trigger] [--dry-run]
  python monst_autonomous_agent.py loop [--dry-run]
  python monst_autonomous_agent.py observe
  python monst_autonomous_agent.py memory
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import monst_rank_bot as bot
import screen_journal


Action = Literal[
    "farm_quest",
    "attempt_clear",
    "feed_luck",
    "manage_team",
    "wait_stamina",
    "pull_gacha",
]
PriorityGoal = Literal["rank100", "ungoku", "noma"]
StopType = Literal["repeat", "stamina_below", "item_collected"]

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
MEMORY_PATH = LOG_DIR / "autonomous_memory.json"
EVENTS_PATH = LOG_DIR / "director_events.jsonl"
STRATEGY_PROMPT_PATH = LOG_DIR / "last_strategy_prompt.json"
STRATEGY_RESPONSE_PATH = LOG_DIR / "last_strategy_response.json"
RUNTIME_POLICY_PATH = LOG_DIR / "runtime_policy.json"

LLM_PROVIDER = os.environ.get("MONST_LLM_PROVIDER", "ollama").lower()
LLM_URL = os.environ.get(
    "MONST_LLM_URL",
    os.environ.get("MONST_OLLAMA_URL", "http://localhost:11434/api/chat"),
)
STRATEGY_LLM_MODEL = os.environ.get(
    "MONST_STRATEGY_LLM_MODEL",
    os.environ.get("MONST_LLM_MODEL", os.environ.get("MONST_OLLAMA_MODEL", "qwen3.5:9b")),
)
SUMMARY_LLM_MODEL = os.environ.get("MONST_SUMMARY_LLM_MODEL", "gpt-oss:20b")
LLM_TIMEOUT = int(os.environ.get("MONST_LLM_TIMEOUT", os.environ.get("MONST_OLLAMA_TIMEOUT", "300")))
DEFAULT_REPEAT = int(os.environ.get("MONST_DIRECTIVE_REPEAT", "3"))
MAX_DIRECTIVE_CYCLES = int(os.environ.get("MONST_DIRECTIVE_MAX_CYCLES", "50"))
MAX_WAIT_SEC = int(os.environ.get("MONST_AUTONOMY_MAX_WAIT_SEC", str(bot.STAMINA_WAIT)))
WELCOME_TARGETS = {"welcome_quest", "current_super_shortcut", "現在ホームのスーパーショートカット先"}

DEFAULT_RUNTIME_POLICY = {
    "version": 1,
    "policy_owner": "local_llm",
    "executor": "cv_reactor",
    "authority_model": (
        "The local LLM owns strategy, rule review, and unknown-screen decisions. "
        "The CV reactor executes cached rules for known safe states without asking every frame."
    ),
    "rule_sources": {
        "welcome_navigation": "teacher_demo + observation_journal",
        "battle": "teacher_demo + battle HUD ROI",
        "post_clear": "teacher_demo + templates",
    },
    "unknown_screen_policy": "pause or ask local LLM; never tap blind paid/shop/gacha areas",
}


@dataclass
class StopCondition:
    type: StopType = "repeat"
    count: int | None = None
    value: int | None = None
    item: str | None = None


@dataclass
class Directive:
    action: Action
    target: str
    team: list[str] = field(default_factory=list)
    stop_condition: StopCondition = field(default_factory=StopCondition)
    priority_goal: PriorityGoal = "rank100"


@dataclass
class StrategyOutput:
    diary_entry: str
    reasoning: str
    directive: Directive
    learning_entries: list[str] = field(default_factory=list)
    expected_next_trigger: str = "directive_complete"
    source: str = "llm"


@dataclass
class ExecutionReport:
    trigger: str
    directive: dict[str, Any]
    result: str
    note: str
    cycles_attempted: int = 0
    cycles_ok: int = 0
    started_at: str = ""
    ended_at: str = ""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_memory() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "goal_ledger": {
            "rank100": {
                "target_rank": 100,
                "current_rank": None,
                "status": "rank OCR not configured yet",
            },
            "ungoku": {
                "target": None,
                "luck": None,
                "status": "target selection pending",
            },
            "noma": {
                "cleared": [],
                "next": None,
                "blocked_by": None,
            },
        },
        "strategy_log": [],
        "learnings": [],
        "facts": {},
        "last_directive": None,
        "last_report": None,
    }


def load_memory() -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_PATH.exists():
        memory = default_memory()
        save_memory(memory)
        return memory
    with MEMORY_PATH.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    memory = default_memory()
    memory.update(loaded)
    return memory


def save_memory(memory: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    memory["updated_at"] = now_iso()
    MEMORY_PATH.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def load_runtime_policy() -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not RUNTIME_POLICY_PATH.exists():
        RUNTIME_POLICY_PATH.write_text(
            json.dumps(DEFAULT_RUNTIME_POLICY, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return dict(DEFAULT_RUNTIME_POLICY)
    try:
        return json.loads(RUNTIME_POLICY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        append_event("runtime_policy_error", {"path": str(RUNTIME_POLICY_PATH)})
        return dict(DEFAULT_RUNTIME_POLICY)


def append_event(kind: str, payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": now_iso(), "kind": kind, **payload}
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _tail_runs_csv(limit: int = 8) -> list[dict[str, str]]:
    path = LOG_DIR / "runs.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


def observe(dry_run: bool = False) -> dict[str, Any]:
    """画面から事実を抽出する。

    OCR はテンプレ採取後に差し込む前提。未実装値は None と reason を残し、
    LLM が事実を捏造しないようにする。
    """
    observation = screen_journal.observation_pack(dry_run=dry_run)
    screen_state = observation["current_screen"]["state"]
    facts = {
        "observed_at": now_iso(),
        "screen_state": screen_state,
        "observation": observation,
        "rank": None,
        "stamina": {"current": None, "max": None},
        "main_characters": [],
        "noma_progress": {"cleared": [], "next": None},
        "ungoku_candidates": [],
        "available_quests": [
            {
                "id": "welcome_quest",
                "name": "ウェルカムクエスト",
                "route": "super_shortcut",
                "status": "only_playable_now",
                "known_runner": "monst_rank_bot.run_one_cycle",
                "confidence": "high",
            }
        ],
        "last_runs": _tail_runs_csv(),
        "extraction_gaps": [
            "rank OCR",
            "stamina OCR",
            "character inventory OCR",
            "noma cleared-list OCR",
            "event quest candidate navigation",
            "unlock condition tracking after welcome quest",
            "ocr text extraction into screen journal",
        ],
    }
    append_event("observe", {"facts": facts})
    return facts


def _directive_from_dict(raw: dict[str, Any]) -> Directive:
    stop_raw = raw.get("stop_condition") or {}
    stop = StopCondition(
        type=stop_raw.get("type", "repeat"),
        count=stop_raw.get("count"),
        value=stop_raw.get("value"),
        item=stop_raw.get("item"),
    )
    target = raw.get("target", "welcome_quest")
    if target in WELCOME_TARGETS:
        target = "welcome_quest"
    return Directive(
        action=raw["action"],
        target=target,
        team=list(raw.get("team") or []),
        stop_condition=stop,
        priority_goal=raw.get("priority_goal", "rank100"),
    )


def parse_strategy_output(raw: dict[str, Any]) -> StrategyOutput:
    if not isinstance(raw, dict):
        raise ValueError("strategy output must be a JSON object")
    directive_raw = raw.get("directive")
    if not isinstance(directive_raw, dict):
        raise ValueError("strategy output missing directive object")
    directive = _directive_from_dict(directive_raw)
    if directive.action not in Action.__args__:  # type: ignore[attr-defined]
        raise ValueError(f"unsupported action: {directive.action}")
    if directive.priority_goal not in PriorityGoal.__args__:  # type: ignore[attr-defined]
        raise ValueError(f"unsupported priority_goal: {directive.priority_goal}")
    if directive.stop_condition.type not in StopType.__args__:  # type: ignore[attr-defined]
        raise ValueError(f"unsupported stop_condition.type: {directive.stop_condition.type}")
    return StrategyOutput(
        diary_entry=str(raw.get("diary_entry", "")).strip(),
        reasoning=str(raw.get("reasoning", "")).strip(),
        directive=directive,
        learning_entries=[str(v).strip() for v in raw.get("learning_entries", []) if str(v).strip()],
        expected_next_trigger=str(raw.get("expected_next_trigger", "directive_complete")).strip()
        or "directive_complete",
        source="llm",
    )


def fallback_strategy(trigger: str, facts: dict[str, Any]) -> StrategyOutput:
    if trigger == "stamina_out" or facts.get("screen_state") == "stamina_out":
        directive = Directive(
            action="wait_stamina",
            target="natural_recovery",
            stop_condition=StopCondition(type="repeat", count=1, value=bot.STAMINA_WAIT),
            priority_goal="rank100",
        )
        return StrategyOutput(
            diary_entry="スタミナ切れのため無料の自然回復を待つ。",
            reasoning="課金アイテムは使わず、回復後に既存の安定周回へ戻る。",
            directive=directive,
            learning_entries=["スタミナ切れ時は課金アイテムを使わず自然回復を選ぶ。"],
            expected_next_trigger="wait_complete",
            source="fallback",
        )
    directive = Directive(
        action="farm_quest",
        target="welcome_quest",
        stop_condition=StopCondition(type="repeat", count=DEFAULT_REPEAT),
        priority_goal="rank100",
    )
    return StrategyOutput(
        diary_entry=f"現在プレイ可能なウェルカムクエストを {DEFAULT_REPEAT} 周して進行を確認する。",
        reasoning="現アカウントではウェルカムクエストだけがプレイ可能なので、他クエストを選ばず解放待ちの進行を優先する。",
        directive=directive,
        learning_entries=["現時点ではウェルカムクエストだけがプレイ可能。ほかのノマクエ/降臨は解放後に扱う。"],
        expected_next_trigger="directive_complete",
        source="fallback",
    )


def build_strategy_prompt(trigger: str, facts: dict[str, Any], memory: dict[str, Any]) -> dict[str, Any]:
    recent_strategy = memory.get("strategy_log", [])[-8:]
    recent_learnings = memory.get("learnings", [])[-12:]
    last_report = memory.get("last_report")
    return {
        "role": "monst_account_manager",
        "mission": "新規モンストアカウントをローカルLLMが経営者として運用し、ランク100・運極・ノマ制覇を同時に追う。",
        "trigger": trigger,
        "goals": memory.get("goal_ledger", {}),
        "facts": facts,
        "retrieved_screen_context": facts.get("observation", {}),
        "last_execution_report": last_report,
        "recent_strategy_log": recent_strategy,
        "recent_learnings": recent_learnings,
        "allowed_actions": list(Action.__args__),  # type: ignore[attr-defined]
        "output_schema": {
            "diary_entry": "string",
            "reasoning": "string",
            "learning_entries": ["string"],
            "expected_next_trigger": "string",
            "directive": {
                "action": "farm_quest | attempt_clear | feed_luck | manage_team | wait_stamina | pull_gacha",
                "target": "string",
                "team": ["string"],
                "stop_condition": {
                    "type": "repeat | stamina_below | item_collected",
                    "count": "integer optional",
                    "value": "integer optional",
                    "item": "string optional",
                },
                "priority_goal": "rank100 | ungoku | noma",
            },
        },
        "hard_rules": [
            "Return JSON only.",
            "Do not invent facts. Unknown OCR values are unknown.",
            "Use retrieved_screen_context to learn from similar screens and past action outcomes.",
            "Do not spend paid currency or auto-consume orbs/apples.",
            "The only playable quest right now is welcome_quest via the super_shortcut route.",
            "Use target exactly 'welcome_quest' for playable quest directives.",
            "Do not choose noma or event quests until facts say they are unlocked.",
            "If stamina is out, choose wait_stamina.",
            "If the previous report says navigation_missing or manual_required, choose a currently executable recovery directive or explain the blockage in diary_entry.",
            "Write learning_entries only for reusable lessons, not raw facts.",
        ],
        "execution_contract": {
            "farm_quest/welcome_quest": "実装済み。super_shortcut から既存 bot の run_one_cycle を指定回数実行する。",
            "attempt_clear/welcome_quest": "実装済み。ただしバトルは既存ランダムフリック。",
            "current_super_shortcut": "welcome_quest の互換エイリアス。",
            "wait_stamina": "実装済み。無料自然回復だけ。",
            "feed_luck/manage_team/pull_gacha": "未実装。選ぶと manual_required として次ティックに戻る。",
            "任意クエスト名": "未実装。選ぶと navigation_missing として次ティックに戻る。",
        },
    }


def _read_http_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} from {req.full_url}: {body[:500]}") from e


def _call_local_llm(
    messages: list[dict[str, str]],
    model: str,
    json_mode: bool,
    think: bool | None = None,
) -> str:
    if LLM_PROVIDER == "ollama":
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": messages,
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1200},
        }
        if json_mode:
            body["format"] = "json"
        if think is not None:
            body["think"] = think
    elif LLM_PROVIDER in ("openai", "openai-compatible", "lmstudio", "openwebui"):
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
    else:
        raise RuntimeError(f"unsupported MONST_LLM_PROVIDER={LLM_PROVIDER!r}")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    response = _read_http_json(req)
    if LLM_PROVIDER == "ollama":
        return str(response.get("message", {}).get("content", ""))
    choices = response.get("choices") or []
    return str(choices[0].get("message", {}).get("content", "")) if choices else ""


def call_local_llm_strategy(prompt_payload: dict[str, Any]) -> StrategyOutput:
    system = (
        "あなたはモンスト新規アカウントを運用する戦略AIです。"
        "思考過程は出さず、事実は facts だけを信じ、JSONだけを返してください。"
    )
    user = "/no_think\n" + json.dumps(prompt_payload, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = _call_local_llm(messages, STRATEGY_LLM_MODEL, json_mode=True, think=False)
    STRATEGY_RESPONSE_PATH.write_text(
        json.dumps({
            "ts": now_iso(),
            "model": STRATEGY_LLM_MODEL,
            "content": content,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parse_strategy_output(json.loads(content))


def strategy_tick(
    trigger: str,
    dry_run: bool = False,
    use_llm: bool = True,
    allow_fallback: bool = False,
) -> StrategyOutput:
    memory = load_memory()
    facts = observe(dry_run=dry_run)
    memory["facts"] = facts
    prompt_payload = build_strategy_prompt(trigger, facts, memory)
    STRATEGY_PROMPT_PATH.write_text(
        json.dumps(prompt_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    strategy: StrategyOutput
    if use_llm:
        try:
            strategy = call_local_llm_strategy(prompt_payload)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, RuntimeError) as e:
            append_event("strategy_error", {"trigger": trigger, "error": str(e)})
            memory["last_strategy_error"] = {
                "ts": now_iso(),
                "trigger": trigger,
                "error": str(e),
                "llm_provider": LLM_PROVIDER,
                "llm_url": LLM_URL,
                "llm_model": STRATEGY_LLM_MODEL,
            }
            save_memory(memory)
            if not allow_fallback:
                raise RuntimeError(
                    f"local LLM strategy failed. provider={LLM_PROVIDER} url={LLM_URL} model={STRATEGY_LLM_MODEL}: {e}"
                ) from e
            append_event("strategy_fallback", {"trigger": trigger, "error": str(e)})
            strategy = fallback_strategy(trigger, facts)
    else:
        strategy = fallback_strategy(trigger, facts)

    memory.setdefault("strategy_log", []).append({
        "ts": now_iso(),
        "trigger": trigger,
        "diary_entry": strategy.diary_entry,
        "reasoning": strategy.reasoning,
        "learning_entries": strategy.learning_entries,
        "expected_next_trigger": strategy.expected_next_trigger,
        "source": strategy.source,
        "directive": asdict(strategy.directive),
    })
    memory["strategy_log"] = memory["strategy_log"][-200:]
    if strategy.learning_entries:
        learnings = memory.setdefault("learnings", [])
        for entry in strategy.learning_entries:
            learnings.append({"ts": now_iso(), "trigger": trigger, "entry": entry})
        memory["learnings"] = learnings[-300:]
    memory["last_directive"] = asdict(strategy.directive)
    save_memory(memory)
    append_event("strategy", {
        "trigger": trigger,
        "diary_entry": strategy.diary_entry,
        "reasoning": strategy.reasoning,
        "learning_entries": strategy.learning_entries,
        "expected_next_trigger": strategy.expected_next_trigger,
        "source": strategy.source,
        "directive": asdict(strategy.directive),
    })
    return strategy


def _repeat_count(directive: Directive) -> int:
    stop = directive.stop_condition
    if stop.type == "repeat" and stop.count:
        return max(1, min(int(stop.count), MAX_DIRECTIVE_CYCLES))
    return 1


def execute_directive(strategy: StrategyOutput, trigger: str, dry_run: bool = False) -> ExecutionReport:
    directive = strategy.directive
    started_at = now_iso()
    report = ExecutionReport(
        trigger=trigger,
        directive=asdict(directive),
        result="not_started",
        note="",
        started_at=started_at,
    )

    append_event("directive_start", {"directive": asdict(directive), "dry_run": dry_run})
    retrieved_context = load_memory().get("facts", {}).get("observation", {})
    screen_journal.record_llm_decision(
        trigger=trigger,
        directive=asdict(directive),
        retrieved_context=retrieved_context,
        prompt_path=STRATEGY_PROMPT_PATH,
        response_path=STRATEGY_RESPONSE_PATH,
        outcome="dry_run" if dry_run else None,
    )

    if dry_run:
        report.result = "dry_run"
        report.note = "directive accepted but not executed"
        report.ended_at = now_iso()
        _record_report(report)
        return report

    if directive.action == "wait_stamina":
        wait_sec = directive.stop_condition.value or bot.STAMINA_WAIT
        wait_sec = max(1, min(int(wait_sec), MAX_WAIT_SEC))
        bot.log.info(f"autonomous wait_stamina: {wait_sec}s")
        time.sleep(wait_sec)
        report.result = "ok"
        report.note = f"waited_{wait_sec}s"
        report.ended_at = now_iso()
        _record_report(report)
        return report

    if directive.action in ("feed_luck", "manage_team", "pull_gacha"):
        report.result = "manual_required"
        report.note = f"{directive.action} navigation is not implemented yet"
        report.ended_at = now_iso()
        _record_report(report)
        return report

    if directive.action in ("farm_quest", "attempt_clear"):
        if directive.target not in WELCOME_TARGETS:
            report.result = "navigation_missing"
            report.note = f"target route not implemented: {directive.target}"
            report.ended_at = now_iso()
            _record_report(report)
            return report

        count = _repeat_count(directive)
        for _ in range(count):
            before = screen_journal.record_screen(notes=f"before {directive.action}:{directive.target}")
            ts0 = time.time()
            result, note = bot.run_one_cycle()
            ts1 = time.time()
            after = screen_journal.record_screen(notes=f"after {directive.action}:{directive.target} result={result}")
            action_result = "success" if result == "ok" else "failure"
            screen_journal.record_action_attempt(
                before_screen_id=before.id,
                after_screen_id=after.id,
                action_type=directive.action,
                action=asdict(directive),
                result=action_result,
                note=f"{result}:{note}",
            )
            bot.append_run_csv(ts0, ts1, result, f"autonomous:{note}")
            report.cycles_attempted += 1
            if result == "ok":
                report.cycles_ok += 1
                continue
            report.result = result
            report.note = note
            report.ended_at = now_iso()
            _record_report(report)
            return report

        report.result = "ok"
        report.note = f"completed_{count}_cycles"
        report.ended_at = now_iso()
        _record_report(report)
        return report

    report.result = "unsupported_action"
    report.note = directive.action
    report.ended_at = now_iso()
    _record_report(report)
    return report


def _record_report(report: ExecutionReport) -> None:
    memory = load_memory()
    memory["last_report"] = asdict(report)
    save_memory(memory)
    append_event("directive_end", asdict(report))


def run_once(
    trigger: str,
    dry_run: bool = False,
    use_llm: bool = True,
    allow_fallback: bool = False,
) -> ExecutionReport:
    strategy = strategy_tick(
        trigger=trigger,
        dry_run=dry_run,
        use_llm=use_llm,
        allow_fallback=allow_fallback,
    )
    return execute_directive(strategy, trigger=trigger, dry_run=dry_run)


def run_loop(dry_run: bool = False, use_llm: bool = True, allow_fallback: bool = False) -> None:
    trigger = "startup"
    while True:
        report = run_once(
            trigger=trigger,
            dry_run=dry_run,
            use_llm=use_llm,
            allow_fallback=allow_fallback,
        )
        if dry_run:
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            return
        if report.result == "ok":
            trigger = "directive_complete"
            continue
        if report.result == "stamina_out":
            trigger = "stamina_out"
            continue
        trigger = report.result


def print_memory() -> None:
    print(json.dumps(load_memory(), ensure_ascii=False, indent=2))


def write_daily_summary(dry_run: bool = False) -> Path:
    memory = load_memory()
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = LOG_DIR / f"daily_summary_{today}.md"
    payload = {
        "date": today,
        "goal_ledger": memory.get("goal_ledger", {}),
        "facts": memory.get("facts", {}),
        "last_report": memory.get("last_report"),
        "strategy_log": memory.get("strategy_log", [])[-20:],
        "learnings": memory.get("learnings", [])[-30:],
    }
    if dry_run:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        append_event("daily_summary_dry_run", {"path": str(out_path)})
        return out_path

    system = (
        "あなたはYouTube企画用の記録係です。"
        "モンスト自律エージェントの一日を、日本語で短く、動画ナレーション素材としてまとめてください。"
    )
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    content = _call_local_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        SUMMARY_LLM_MODEL,
        json_mode=False,
        think=True,
    )
    out_path.write_text(content.strip() + "\n", encoding="utf-8")
    append_event("daily_summary", {"path": str(out_path), "model": SUMMARY_LLM_MODEL})
    return out_path


def candidate_taps_from_observation(observation: dict[str, Any]) -> list[dict[str, Any]]:
    current = observation.get("current_screen", {})
    similar = observation.get("similar_screens", [])
    states = {current.get("state")}
    states.update(item.get("state") for item in similar)
    candidates: list[dict[str, Any]] = []

    if "welcome_home" in states:
        candidates.extend([
            {
                "id": "welcome_center_icon",
                "x": 540,
                "y": 1160,
                "description": "中央のウェルカムクエストアイコン付近",
            },
            {
                "id": "welcome_event_button",
                "x": 540,
                "y": 1570,
                "description": "中央下のイベントボタン付近",
            },
            {
                "id": "welcome_banner",
                "x": 540,
                "y": 1910,
                "description": "下部のウェルカムクエストバナー中央",
            },
        ])

    if "welcome_info_dialog" in states:
        candidates.extend([
            {
                "id": "welcome_info_ok_center",
                "x": 540,
                "y": 1988,
                "description": "ウェルカムクエスト説明ダイアログ下部OKボタンの中央寄り",
            },
            {
                "id": "welcome_info_ok_low",
                "x": 540,
                "y": 2020,
                "description": "ウェルカムクエスト説明ダイアログ下部OKボタンの下寄り。直近では変化なしだった候補",
            },
            {
                "id": "welcome_info_scroll",
                "x": 875,
                "y": 1100,
                "description": "説明ダイアログ右側スクロール領域",
            },
        ])

    if "welcome_quest_list" in states:
        candidates.extend([
            {
                "id": "welcome_gimmick_1_new",
                "x": 510,
                "y": 1855,
                "description": "ウェルカムクエスト一覧下部のNEW付き「ギミックを学ぼう！その1」",
            },
            {
                "id": "welcome_gimmick_2",
                "x": 520,
                "y": 1565,
                "description": "「ギミックを学ぼう！その2」。その1クリア後に開放される候補",
            },
            {
                "id": "welcome_list_back",
                "x": 90,
                "y": 470,
                "description": "ウェルカムクエスト一覧の戻るボタン",
            },
        ])

    if "welcome_stage_panel" in states:
        candidates.extend([
            {
                "id": "welcome_stage_block_row",
                "x": 420,
                "y": 900,
                "description": "NEW表示のステージ「ブロック」行の中央。クエストに入る主候補",
            },
            {
                "id": "welcome_stage_block_detail",
                "x": 910,
                "y": 900,
                "description": "ステージ「ブロック」行右側の詳細ボタン",
            },
            {
                "id": "welcome_stage_header",
                "x": 485,
                "y": 680,
                "description": "上部の「ギミックを学ぼう！その1」見出し行",
            },
        ])

    if "play_type_select" in states:
        candidates.extend([
            {
                "id": "play_type_solo",
                "x": 300,
                "y": 1340,
                "description": "左側のソロプレイ。自律エージェントが単独でクエストを進める主候補",
            },
            {
                "id": "play_type_multiplayer",
                "x": 780,
                "y": 1340,
                "description": "右側のマルチ募集。今回は選ばない方がよい候補",
            },
            {
                "id": "play_type_back",
                "x": 90,
                "y": 470,
                "description": "プレイタイプ選択の戻るボタン",
            },
        ])

    if "deck" in states or "welcome_deck_select" in states:
        candidates.extend([
            {
                "id": "deck_sortie",
                "x": 540,
                "y": 1535,
                "description": "デッキ選択画面中央下の「出撃」ボタン。クエスト開始の主候補",
            },
            {
                "id": "deck_details",
                "x": 540,
                "y": 1375,
                "description": "デッキ詳細ボタン。出撃には不要な確認候補",
            },
            {
                "id": "deck_back",
                "x": 90,
                "y": 470,
                "description": "デッキ選択画面の戻るボタン",
            },
        ])

    if "battle" in states or "battle_welcome_block" in states:
        candidates.extend([
            {
                "id": "battle_shot_diagonal_right",
                "action": "swipe",
                "x": 540,
                "y": 700,
                "dx": 360,
                "dy": 240,
                "description": "バトル安全ゾーン中央から右下へ引っ張るショット候補。ブロック間で反射を狙う",
            },
            {
                "id": "battle_shot_diagonal_left",
                "action": "swipe",
                "x": 540,
                "y": 700,
                "dx": -360,
                "dy": 240,
                "description": "バトル安全ゾーン中央から左下へ引っ張るショット候補。左側の敵とブロックを狙う",
            },
            {
                "id": "battle_shot_vertical",
                "action": "swipe",
                "x": 540,
                "y": 700,
                "dx": 0,
                "dy": 430,
                "description": "バトル安全ゾーン中央から下へ引っ張る縦ショット候補。中央ラインを通す",
            },
        ])

    if "battle_gimmick_dialog" in states:
        candidates.extend([
            {
                "id": "battle_gimmick_ok",
                "action": "tap",
                "x": 540,
                "y": 1605,
                "description": "バトル中の出現ギミック説明ダイアログ中央下OKボタン",
            },
            {
                "id": "battle_gimmick_text_area",
                "action": "tap",
                "x": 540,
                "y": 1200,
                "description": "説明本文エリア。通常は進行しない候補",
            },
        ])

    if "battle_gauge_dialog" in states:
        candidates.extend([
            {
                "id": "battle_gauge_ok",
                "action": "tap",
                "x": 540,
                "y": 1605,
                "description": "バトル中のゲージ説明ダイアログ中央下OKボタン",
            },
            {
                "id": "battle_gauge_text_area",
                "action": "tap",
                "x": 540,
                "y": 1200,
                "description": "ゲージ説明本文エリア。通常は進行しない候補",
            },
        ])

    if "rank_up" in states:
        candidates.extend([
            {
                "id": "rank_up_continue",
                "action": "tap",
                "x": 540,
                "y": 1200,
                "description": "クリア後ランクアップ演出。画面中央タップで進める",
            },
        ])

    if "result" in states or "kyouka" in states or "koryaku_hint" in states:
        candidates.extend([
            {
                "id": "post_clear_ok_middle",
                "action": "tap",
                "x": 540,
                "y": 1440,
                "description": "クリア後のヒント/リザルト/報酬画面を進める中央OK候補",
            },
            {
                "id": "post_clear_ok_lower",
                "action": "tap",
                "x": 540,
                "y": 1680,
                "description": "クリア後のリザルト送りや報酬画面を進める下寄りOK候補",
            },
        ])

    if not candidates:
        candidates.extend([
            {"id": "screen_center", "action": "tap", "x": 540, "y": 1200, "description": "画面中央"},
            {"id": "lower_center", "action": "tap", "x": 540, "y": 1700, "description": "画面下寄り中央"},
            {"id": "back_button", "action": "tap", "x": 90, "y": 150, "description": "左上戻る"},
        ])
    return candidates


def propose_learning_tap(observation: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = {
        "task": "Choose exactly one tap or swipe candidate to learn how to operate the current Monst screen.",
        "rules": [
            "Return JSON only.",
            "Choose from candidate ids only.",
            "Prefer actions that are likely to enter or progress the welcome quest.",
            "In battle, choose a swipe shot candidate instead of tap candidates.",
            "Do not choose paid currency, shop, gacha, or friend buttons.",
        ],
        "observation": observation,
        "candidates": candidates,
        "output_schema": {
            "candidate_id": "string",
            "reason": "string",
            "expected_result": "string",
        },
    }
    content = _call_local_llm(
        [
            {"role": "system", "content": "You are a local game-control agent. Return JSON only. /no_think"},
            {"role": "user", "content": "/no_think\n" + json.dumps(prompt, ensure_ascii=False, indent=2)},
        ],
        STRATEGY_LLM_MODEL,
        json_mode=True,
        think=False,
    )
    raw = json.loads(content)
    candidate_id = raw.get("candidate_id")
    chosen = next((item for item in candidates if item["id"] == candidate_id), None)
    if chosen is None:
        raise RuntimeError(f"LLM chose unknown candidate_id={candidate_id!r}")
    return {
        "candidate": chosen,
        "reason": str(raw.get("reason", "")),
        "expected_result": str(raw.get("expected_result", "")),
        "raw": raw,
    }


def learn_step(dry_run: bool = False) -> dict[str, Any]:
    observation = screen_journal.observation_pack(dry_run=dry_run)
    candidates = candidate_taps_from_observation(observation)
    proposal = propose_learning_tap(observation, candidates)
    screen_journal.record_llm_decision(
        trigger="learn_step",
        directive={
            "action": proposal["candidate"].get("action", "tap"),
            "candidate": proposal["candidate"],
            "reason": proposal["reason"],
            "expected_result": proposal["expected_result"],
        },
        retrieved_context=observation,
        outcome="dry_run" if dry_run else None,
        note="LLM-selected tap candidate",
    )
    if dry_run:
        return {"observation": observation, "candidates": candidates, "proposal": proposal}
    candidate = proposal["candidate"]
    note = f"{candidate['id']} {proposal['reason']} expected={proposal['expected_result']}"
    if candidate.get("action", "tap") == "swipe":
        tap_result = screen_journal.swipe_probe(
            int(candidate["x"]),
            int(candidate["y"]),
            int(candidate["dx"]),
            int(candidate["dy"]),
            note,
        )
    else:
        tap_result = screen_journal.tap_probe(
            int(candidate["x"]),
            int(candidate["y"]),
            note,
        )
    return {
        "observation": observation,
        "candidates": candidates,
        "proposal": proposal,
        "tap_result": tap_result,
    }


DEMO_BATTLE_SHOTS = [
    {
        "id": "demo_left_block_lane",
        "x": 540,
        "y": 700,
        "dx": -360,
        "dy": 240,
        "note": "teacher_demo: left diagonal block lane",
    },
    {
        "id": "demo_right_block_lane",
        "x": 540,
        "y": 700,
        "dx": 360,
        "dy": 240,
        "note": "teacher_demo: right diagonal block lane",
    },
    {
        "id": "demo_vertical_lane",
        "x": 540,
        "y": 700,
        "dx": 0,
        "dy": 430,
        "note": "teacher_demo: vertical lane through center",
    },
]


def _observation_states(observation: dict[str, Any]) -> set[str]:
    current = observation.get("current_screen", {})
    states = {current.get("state")}
    states.update(item.get("state") for item in observation.get("similar_screens", []))
    return {state for state in states if state}


def collect_teacher_demo(max_steps: int = 18, dry_run: bool = False) -> dict[str, Any]:
    """Fast data collection: use known-safe teacher actions and record them for RAG."""
    steps: list[dict[str, Any]] = []
    shot_index = 0
    for i in range(max_steps):
        observation = screen_journal.observation_pack(dry_run=dry_run)
        current = observation["current_screen"]
        states = _observation_states(observation)
        detected = current.get("state")
        if detected == "home" or "home" in states or "home_after_welcome_clear" in states:
            steps.append({"step": i, "kind": "terminal", "state": detected, "states": sorted(states)})
            break

        if states & {"result", "kyouka", "koryaku_hint"}:
            if dry_run:
                result = {"dry_run": True, "action": "ok_sweep"}
            else:
                result = screen_journal.ok_sweep_probe("teacher_demo: advance post-clear screen")
            steps.append({"step": i, "kind": "post_clear_ok", "states": sorted(states), "result": result})
            continue

        if "rank_up" in states:
            if dry_run:
                result = {"dry_run": True, "action": "rank_up_continue"}
            else:
                result = screen_journal.tap_probe(540, 1200, "teacher_demo: advance rank up")
            steps.append({"step": i, "kind": "rank_up_continue", "states": sorted(states), "result": result})
            continue

        if states & {"battle_gimmick_dialog", "battle_gauge_dialog"}:
            if dry_run:
                result = {"dry_run": True, "action": "tap_ok"}
            else:
                result = screen_journal.tap_probe(
                    540,
                    1605,
                    "teacher_demo: dismiss battle tutorial dialog",
                )
                screen_journal.label_screen(
                    result["after"]["id"],
                    "battle_welcome_block",
                    "teacher demo returned from dialog to battle or battle transition",
                )
            steps.append({"step": i, "kind": "dialog_ok", "states": sorted(states), "result": result})
            continue

        if detected == "deck" or "welcome_deck_select" in states:
            if dry_run:
                result = {"dry_run": True, "action": "deck_sortie"}
            else:
                result = screen_journal.tap_probe(540, 1535, "teacher_demo: sortie from deck")
            steps.append({"step": i, "kind": "deck_sortie", "states": sorted(states), "result": result})
            continue

        if detected != "battle" and "battle_welcome_block" not in states:
            steps.append({"step": i, "kind": "unknown_wait", "state": detected, "states": sorted(states)})
            time.sleep(3.0)
            continue

        if not dry_run and detected == "battle":
            screen_journal.label_screen(
                int(current["id"]),
                "battle_welcome_block",
                "teacher demo confirmed active battle by battle HUD ROI",
            )
        shot = DEMO_BATTLE_SHOTS[shot_index % len(DEMO_BATTLE_SHOTS)]
        shot_index += 1
        if dry_run:
            result = {"dry_run": True, "action": shot}
        else:
            result = screen_journal.swipe_probe(
                int(shot["x"]),
                int(shot["y"]),
                int(shot["dx"]),
                int(shot["dy"]),
                shot["note"],
                wait_s=7.0,
            )
        steps.append({"step": i, "kind": "battle_shot", "states": sorted(states), "shot": shot, "result": result})
    return {"mode": "teacher_demo", "steps": steps}


def _candidate_by_id(candidates: list[dict[str, Any]], candidate_id: str) -> dict[str, Any] | None:
    return next((item for item in candidates if item.get("id") == candidate_id), None)


def select_reactor_action(observation: dict[str, Any]) -> dict[str, Any]:
    current = observation.get("current_screen", {})
    states = _observation_states(observation)
    candidates = candidate_taps_from_observation(observation)
    policy = load_runtime_policy()

    def use(candidate_id: str, reason: str) -> dict[str, Any]:
        candidate = _candidate_by_id(candidates, candidate_id)
        if candidate is None:
            raise RuntimeError(f"reactor candidate missing: {candidate_id}")
        return {
            "source": "policy_cache",
            "policy_owner": policy.get("policy_owner", "local_llm"),
            "executor": policy.get("executor", "cv_reactor"),
            "candidate": candidate,
            "reason": reason,
            "states": sorted(states),
        }

    if current.get("state") == "home" or "home" in states or "home_after_welcome_clear" in states:
        return {
            "source": "terminal",
            "policy_owner": policy.get("policy_owner", "local_llm"),
            "executor": policy.get("executor", "cv_reactor"),
            "candidate": {"id": "terminal_home", "action": "none"},
            "reason": "Home screen reached; stop fast reactor unless an outer strategy requests a quest.",
            "states": sorted(states),
        }
    if "rank_up" in states:
        return use("rank_up_continue", "Cached post-clear rule: rank-up screen advances with center tap.")
    if states & {"result", "kyouka", "koryaku_hint"}:
        return {
            "source": "policy_cache",
            "policy_owner": policy.get("policy_owner", "local_llm"),
            "executor": policy.get("executor", "cv_reactor"),
            "candidate": {"id": "post_clear_ok_sweep", "action": "ok_sweep"},
            "reason": "Cached post-clear rule: sweep safe OK positions through result/reward/hint screens.",
            "states": sorted(states),
        }
    if "battle_gauge_dialog" in states:
        return use("battle_gauge_ok", "Cached battle rule: dismiss gauge tutorial dialog with OK.")
    if "battle_gimmick_dialog" in states:
        return use("battle_gimmick_ok", "Cached battle rule: dismiss gimmick tutorial dialog with OK.")
    if current.get("state") == "deck" or "welcome_deck_select" in states:
        return use("deck_sortie", "Cached quest-start rule: deck screen progresses with sortie.")
    if "play_type_select" in states:
        return use("play_type_solo", "Cached welcome rule: choose solo for autonomous play.")
    if "welcome_stage_panel" in states:
        return use("welcome_stage_block_row", "Cached welcome rule: tap the NEW stage row.")
    if "welcome_quest_list" in states:
        return use("welcome_gimmick_1_new", "Cached welcome rule: first unlocked welcome quest is the NEW item.")
    if "welcome_info_dialog" in states:
        return use("welcome_info_ok_center", "Cached welcome rule: center of OK button succeeded; lower OK failed.")
    if "welcome_home" in states:
        return use("welcome_center_icon", "Cached welcome rule: center icon opens the welcome quest.")
    if current.get("state") == "battle" or "battle_welcome_block" in states:
        battle_candidates = [item for item in candidates if item.get("action") == "swipe"]
        if battle_candidates:
            idx = int(current.get("id", 0)) % len(battle_candidates)
            candidate = battle_candidates[idx]
            return {
                "source": "policy_cache",
                "policy_owner": policy.get("policy_owner", "local_llm"),
                "executor": policy.get("executor", "cv_reactor"),
                "candidate": candidate,
                "reason": "Cached battle rule: easy quest, fire a safe-zone shot without LLM latency.",
                "states": sorted(states),
            }
    return {
        "source": "unknown",
        "policy_owner": policy.get("policy_owner", "local_llm"),
        "executor": policy.get("executor", "cv_reactor"),
        "candidate": {"id": "unknown_wait", "action": "wait", "seconds": 2.5},
        "reason": "No cached safe rule matched; unknown screens should wait or ask local LLM.",
        "states": sorted(states),
    }


def execute_reactor_action(decision: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    candidate = decision["candidate"]
    action = candidate.get("action", "tap")
    note = (
        f"reactor source={decision.get('source')} "
        f"policy_owner={decision.get('policy_owner')} "
        f"id={candidate.get('id')} reason={decision.get('reason')}"
    )
    if dry_run or action == "none":
        return {"dry_run": dry_run, "action": action, "candidate": candidate}
    if action == "wait":
        time.sleep(float(candidate.get("seconds", 2.5)))
        after = screen_journal.record_screen(notes=note)
        return {"action": "wait", "after": asdict(after)}
    if action == "ok_sweep":
        return screen_journal.ok_sweep_probe(note)
    if action == "swipe":
        return screen_journal.swipe_probe(
            int(candidate["x"]),
            int(candidate["y"]),
            int(candidate["dx"]),
            int(candidate["dy"]),
            note,
            wait_s=5.5,
        )
    return screen_journal.tap_probe(
        int(candidate["x"]),
        int(candidate["y"]),
        note,
    )


def reactor_step(dry_run: bool = False, llm_on_unknown: bool = False) -> dict[str, Any]:
    observation = screen_journal.observation_pack(dry_run=dry_run)
    decision = select_reactor_action(observation)
    screen_journal.record_llm_decision(
        trigger="reactor_step",
        directive=decision,
        retrieved_context=observation,
        outcome="dry_run" if dry_run else None,
        note="fast reactor decision; known states execute cached local-LLM policy",
    )
    if decision["source"] == "unknown" and llm_on_unknown:
        llm_result = learn_step(dry_run=dry_run)
        return {
            "mode": "reactor_step",
            "observation": observation,
            "decision": decision,
            "llm_unknown_result": llm_result,
        }
    result = execute_reactor_action(decision, dry_run=dry_run)
    return {
        "mode": "reactor_step",
        "observation": observation,
        "decision": decision,
        "result": result,
    }


def reactor_loop(max_steps: int = 40, dry_run: bool = False, llm_on_unknown: bool = False) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for i in range(max_steps):
        step = reactor_step(dry_run=dry_run, llm_on_unknown=llm_on_unknown)
        step["step"] = i
        steps.append(step)
        candidate_id = step.get("decision", {}).get("candidate", {}).get("id")
        if candidate_id == "terminal_home":
            break
    return {"mode": "reactor_loop", "steps": steps}


def help_text() -> None:
    print("""
usage: python monst_autonomous_agent.py [once|loop|autopilot|observe|memory|daily-summary|learn-tap|learn-step|collect-demo|reactor-step|reactor-loop] [trigger] [--dry-run] [--no-llm] [--allow-fallback]

  once [trigger]  戦略ティックを1回実行し、指令を実行する
  loop            ローカルLLM戦略ティックと実行ティックを継続する
  autopilot       loop と同じ。LLM経営者モードの明示エイリアス
  observe         現在画面の facts を出力する
  memory          日誌4層メモリを出力する
  daily-summary   GPT-OSS 20Bで今日のまとめを logs/daily_summary_YYYY-MM-DD.md に書く
  learn-tap <x> <y> [note...]
                  現在画面、1タップ、遷移後画面を観測RAG DBに記録する
  learn-step      Qwenが観測RAGから候補タップを1つ選び、1タップだけ学習する
  collect-demo [max_steps]
                  Qwenを毎手呼ばず、教師デモ操作を高速収集してRAG DBへ蓄積する
  reactor-step    既知画面は方針キャッシュで即処理し、未知画面だけ停止/LLM相談する
  reactor-loop [max_steps]
                  reactor-step を繰り返す。--llm-on-unknown で未知画面だけQwenへ渡す
  --dry-run       ADB操作を実行せず、戦略と指令検証だけ行う
  --no-llm        Ollamaを呼ばず、内蔵フォールバック戦略を使う（テスト専用）
  --allow-fallback
                  Ollama失敗時だけ内蔵フォールバック戦略へ逃がす
  --llm-on-unknown
                  reactor 系で未知画面に当たった時だけ Qwen の learn-step を使う

デフォルトはローカルLLM必須。Ollamaに繋がらない場合は勝手に周回せず停止して
logs/autonomous_memory.json と logs/director_events.jsonl に理由を残す。

LLM設定:
  MONST_LLM_PROVIDER=ollama
  MONST_LLM_URL=http://localhost:11434/api/chat
  MONST_STRATEGY_LLM_MODEL=qwen3.5:9b
  MONST_SUMMARY_LLM_MODEL=gpt-oss:20b

OpenAI互換サーバーの場合:
  MONST_LLM_PROVIDER=openai
  MONST_LLM_URL=http://localhost:1234/v1/chat/completions
  MONST_STRATEGY_LLM_MODEL=<local-strategy-model-name>
  MONST_SUMMARY_LLM_MODEL=<local-summary-model-name>
""")


def main(argv: list[str]) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    mode = argv[1] if len(argv) > 1 else "once"
    dry_run = "--dry-run" in argv
    use_llm = "--no-llm" not in argv
    allow_fallback = "--allow-fallback" in argv
    llm_on_unknown = "--llm-on-unknown" in argv
    positional = [a for a in argv[2:] if not a.startswith("--")]

    if mode == "observe":
        print(json.dumps(observe(dry_run=dry_run), ensure_ascii=False, indent=2))
        return 0
    if mode == "memory":
        print_memory()
        return 0
    if mode == "daily-summary":
        path = write_daily_summary(dry_run=dry_run)
        print(path)
        return 0
    if mode == "learn-tap":
        if len(positional) < 2:
            help_text()
            return 2
        result = screen_journal.tap_probe(int(positional[0]), int(positional[1]), " ".join(positional[2:]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "learn-step":
        result = learn_step(dry_run=dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "collect-demo":
        max_steps = int(positional[0]) if positional else 18
        result = collect_teacher_demo(max_steps=max_steps, dry_run=dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "reactor-step":
        result = reactor_step(dry_run=dry_run, llm_on_unknown=llm_on_unknown)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "reactor-loop":
        max_steps = int(positional[0]) if positional else 40
        result = reactor_loop(max_steps=max_steps, dry_run=dry_run, llm_on_unknown=llm_on_unknown)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "once":
        trigger = positional[0] if positional else "manual"
        report = run_once(
            trigger=trigger,
            dry_run=dry_run,
            use_llm=use_llm,
            allow_fallback=allow_fallback,
        )
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0
    if mode in ("loop", "autopilot"):
        run_loop(dry_run=dry_run, use_llm=use_llm, allow_fallback=allow_fallback)
        return 0
    if mode in ("help", "-h", "--help"):
        help_text()
        return 0
    help_text()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except RuntimeError as e:
        append_event("fatal", {"error": str(e)})
        print(json.dumps({
            "result": "llm_error",
            "error": str(e),
            "llm_provider": LLM_PROVIDER,
            "llm_url": LLM_URL,
            "llm_model": STRATEGY_LLM_MODEL,
            "summary_llm_model": SUMMARY_LLM_MODEL,
            "memory": str(MEMORY_PATH),
            "events": str(EVENTS_PATH),
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)
