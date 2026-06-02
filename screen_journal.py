#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Observation journal for the autonomous Monst agent.

Stores screenshots, template scores, action attempts, and simple image-hash
similarity data so the local LLM can receive grounded context instead of only
hand-written facts.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import monst_rank_bot as bot


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SCREEN_DIR = ROOT / "screens" / "journal"
DB_PATH = LOG_DIR / "observation_journal.sqlite3"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ScreenObservation:
    id: int
    ts: str
    state: str
    image_path: str | None
    perceptual_hash: str | None
    template_scores: dict[str, float]
    ocr_text: str | None = None
    label: str | None = None


@dataclass
class SimilarScreen:
    id: int
    ts: str
    state: str
    image_path: str | None
    distance: int
    successful_actions: list[dict[str, Any]]
    failed_actions: list[dict[str, Any]]


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            state TEXT NOT NULL,
            image_path TEXT,
            perceptual_hash TEXT,
            template_scores_json TEXT NOT NULL,
            ocr_text TEXT,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_screens_state ON screens(state);
        CREATE INDEX IF NOT EXISTS idx_screens_ts ON screens(ts);

        CREATE TABLE IF NOT EXISTS action_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            before_screen_id INTEGER,
            after_screen_id INTEGER,
            action_type TEXT NOT NULL,
            action_json TEXT NOT NULL,
            result TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY(before_screen_id) REFERENCES screens(id),
            FOREIGN KEY(after_screen_id) REFERENCES screens(id)
        );

        CREATE INDEX IF NOT EXISTS idx_actions_before ON action_attempts(before_screen_id);
        CREATE INDEX IF NOT EXISTS idx_actions_result ON action_attempts(result);

        CREATE TABLE IF NOT EXISTS llm_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            trigger TEXT NOT NULL,
            prompt_path TEXT,
            response_path TEXT,
            directive_json TEXT NOT NULL,
            retrieved_context_json TEXT NOT NULL,
            outcome TEXT,
            note TEXT
        );
        """
    )
    _ensure_column(conn, "screens", "label", "TEXT")
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def image_hash(img: np.ndarray) -> str:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
    mean = float(small.mean())
    bits = ["1" if px >= mean else "0" for px in small.flatten()]
    value = int("".join(bits), 2)
    return f"{value:064x}"


def hash_distance(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 10**9
    return (int(a, 16) ^ int(b, 16)).bit_count()


def template_scores(screen: np.ndarray | None) -> dict[str, float]:
    if screen is None:
        return {}
    return {name: round(bot.match_score(name, screen), 4) for name in bot.TEMPLATES}


def save_screen_image(img: np.ndarray, state: str, ts: str) -> Path:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "").replace("-", "").replace("T", "_")
    path = SCREEN_DIR / f"{safe_ts}_{state}.png"
    cv2.imwrite(str(path), img)
    return path


def record_screen(
    screen: np.ndarray | None = None,
    state: str | None = None,
    save_image: bool = True,
    notes: str | None = None,
) -> ScreenObservation:
    if screen is None:
        screen = bot.screencap()
    if state is None:
        state = bot.detect_state(screen) if screen is not None else "no_screen"
    ts = now_iso()
    img_path: Path | None = None
    phash: str | None = None
    scores = template_scores(screen)
    if screen is not None:
        phash = image_hash(screen)
        if save_image:
            img_path = save_screen_image(screen, state, ts)

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO screens
                (ts, state, image_path, perceptual_hash, template_scores_json, ocr_text, notes, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                state,
                str(img_path) if img_path else None,
                phash,
                json.dumps(scores, ensure_ascii=False),
                None,
                notes,
                None,
            ),
        )
        screen_id = int(cur.lastrowid)
        conn.commit()

    return ScreenObservation(
        id=screen_id,
        ts=ts,
        state=state,
        image_path=str(img_path) if img_path else None,
        perceptual_hash=phash,
        template_scores=scores,
        label=None,
    )


def _actions_for_screen(conn: sqlite3.Connection, screen_id: int, result: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, ts, action_type, action_json, result, note, after_screen_id
        FROM action_attempts
        WHERE before_screen_id = ? AND result = ?
        ORDER BY id DESC
        LIMIT 5
        """,
        (screen_id, result),
    ).fetchall()
    actions: list[dict[str, Any]] = []
    for row in rows:
        actions.append({
            "id": row["id"],
            "ts": row["ts"],
            "action_type": row["action_type"],
            "action": json.loads(row["action_json"]),
            "result": row["result"],
            "note": row["note"],
            "after_screen_id": row["after_screen_id"],
        })
    return actions


def retrieve_similar_screens(
    observation: ScreenObservation,
    limit: int = 5,
    max_distance: int = 64,
) -> list[SimilarScreen]:
    if not observation.perceptual_hash:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, ts, state, image_path, perceptual_hash, label
            FROM screens
            WHERE id != ? AND perceptual_hash IS NOT NULL
            ORDER BY id DESC
            LIMIT 300
            """,
            (observation.id,),
        ).fetchall()
        candidates: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            dist = hash_distance(observation.perceptual_hash, row["perceptual_hash"])
            if dist <= max_distance:
                candidates.append((dist, row))
        candidates.sort(key=lambda item: item[0])
        similar: list[SimilarScreen] = []
        for dist, row in candidates[:limit]:
            screen_id = int(row["id"])
            similar.append(SimilarScreen(
                id=screen_id,
                ts=row["ts"],
                state=row["label"] or row["state"],
                image_path=row["image_path"],
                distance=dist,
                successful_actions=_actions_for_screen(conn, screen_id, "success"),
                failed_actions=_actions_for_screen(conn, screen_id, "failure"),
            ))
        return similar


def record_action_attempt(
    *,
    before_screen_id: int | None,
    after_screen_id: int | None,
    action_type: str,
    action: dict[str, Any],
    result: str,
    note: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO action_attempts
                (ts, before_screen_id, after_screen_id, action_type, action_json, result, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                before_screen_id,
                after_screen_id,
                action_type,
                json.dumps(action, ensure_ascii=False),
                result,
                note,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def record_llm_decision(
    *,
    trigger: str,
    directive: dict[str, Any],
    retrieved_context: dict[str, Any],
    prompt_path: Path | None = None,
    response_path: Path | None = None,
    outcome: str | None = None,
    note: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO llm_decisions
                (ts, trigger, prompt_path, response_path, directive_json, retrieved_context_json, outcome, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                trigger,
                str(prompt_path) if prompt_path else None,
                str(response_path) if response_path else None,
                json.dumps(directive, ensure_ascii=False),
                json.dumps(retrieved_context, ensure_ascii=False),
                outcome,
                note,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def observation_pack(dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        obs = ScreenObservation(
            id=0,
            ts=now_iso(),
            state="dry_run",
            image_path=None,
            perceptual_hash=None,
            template_scores={},
        )
        similar: list[SimilarScreen] = []
    else:
        obs = record_screen()
        similar = retrieve_similar_screens(obs)
    return {
        "current_screen": asdict(obs),
        "similar_screens": [asdict(item) for item in similar],
    }


def label_screen(screen_id: int, label: str, note: str = "") -> None:
    with connect() as conn:
        row = conn.execute("SELECT notes FROM screens WHERE id = ?", (screen_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"screen not found: {screen_id}")
        notes = row["notes"] or ""
        if note:
            notes = (notes + "\n" + note).strip()
        conn.execute(
            "UPDATE screens SET label = ?, state = ?, notes = ? WHERE id = ?",
            (label, label, notes, screen_id),
        )
        conn.commit()


def update_action_result(action_id: int, result: str, note: str = "") -> None:
    with connect() as conn:
        row = conn.execute("SELECT note FROM action_attempts WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"action attempt not found: {action_id}")
        old_note = row["note"] or ""
        new_note = (old_note + "\n" + note).strip() if note else old_note
        conn.execute(
            "UPDATE action_attempts SET result = ?, note = ? WHERE id = ?",
            (result, new_note, action_id),
        )
        conn.commit()


def tap_probe(x: int, y: int, note: str = "", wait_s: float = 2.5, jitter: int = 0) -> dict[str, Any]:
    before = record_screen(notes=f"tap_probe before {x},{y} {note}".strip())
    bot.tap(x, y, jitter=jitter)
    time.sleep(wait_s)
    after = record_screen(notes=f"tap_probe after {x},{y} {note}".strip())
    dist = hash_distance(before.perceptual_hash, after.perceptual_hash)
    result = "changed" if dist > 8 or before.state != after.state else "no_change"
    action_id = record_action_attempt(
        before_screen_id=before.id,
        after_screen_id=after.id,
        action_type="tap_probe",
        action={"x": x, "y": y, "jitter": jitter},
        result=result,
        note=f"{note} hash_distance={dist}",
    )
    return {
        "action_id": action_id,
        "result": result,
        "hash_distance": dist,
        "before": asdict(before),
        "after": asdict(after),
    }


def swipe_probe(
    x: int,
    y: int,
    dx: int,
    dy: int,
    note: str = "",
    wait_s: float = 6.0,
    hold_ms: int = 160,
    duration_ms: int = 420,
) -> dict[str, Any]:
    before = record_screen(notes=f"swipe_probe before {x},{y} {dx},{dy} {note}".strip())
    bot.drag_and_release(x, y, dx, dy, hold_ms=hold_ms, duration_ms=duration_ms)
    time.sleep(wait_s)
    after = record_screen(notes=f"swipe_probe after {x},{y} {dx},{dy} {note}".strip())
    dist = hash_distance(before.perceptual_hash, after.perceptual_hash)
    result = "changed" if dist > 8 or before.state != after.state else "no_change"
    action_id = record_action_attempt(
        before_screen_id=before.id,
        after_screen_id=after.id,
        action_type="swipe_probe",
        action={
            "x": x,
            "y": y,
            "dx": dx,
            "dy": dy,
            "hold_ms": hold_ms,
            "duration_ms": duration_ms,
        },
        result=result,
        note=f"{note} hash_distance={dist}",
    )
    return {
        "action_id": action_id,
        "result": result,
        "hash_distance": dist,
        "before": asdict(before),
        "after": asdict(after),
    }


def ok_sweep_probe(note: str = "", wait_s: float = 4.0) -> dict[str, Any]:
    before = record_screen(notes=f"ok_sweep before {note}".strip())
    bot.tap_sweep_ok()
    time.sleep(wait_s)
    after = record_screen(notes=f"ok_sweep after {note}".strip())
    dist = hash_distance(before.perceptual_hash, after.perceptual_hash)
    result = "changed" if dist > 8 or before.state != after.state else "no_change"
    action_id = record_action_attempt(
        before_screen_id=before.id,
        after_screen_id=after.id,
        action_type="ok_sweep",
        action={"x": bot.OK_SWEEP_X, "ys": bot.OK_SWEEP_Y},
        result=result,
        note=f"{note} hash_distance={dist}",
    )
    return {
        "action_id": action_id,
        "result": result,
        "hash_distance": dist,
        "before": asdict(before),
        "after": asdict(after),
    }


def recent(limit: int = 10) -> dict[str, Any]:
    with connect() as conn:
        screens = conn.execute(
            """
            SELECT id, ts, state, label, image_path, notes
            FROM screens
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        actions = conn.execute(
            """
            SELECT id, ts, before_screen_id, after_screen_id, action_type, action_json, result, note
            FROM action_attempts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "screens": [dict(row) for row in screens],
        "actions": [
            {
                **{k: row[k] for k in row.keys() if k != "action_json"},
                "action": json.loads(row["action_json"]),
            }
            for row in actions
        ],
    }


def help_text() -> None:
    print("""
usage: python screen_journal.py [observe|recent|label|tap-probe|mark-action]

  observe
      現在画面を保存して observation pack を出力する
  recent [limit]
      最近の screen/action を表示する
  label <screen_id> <label> [note...]
      画面に人間/LLM用ラベルを付ける
  tap-probe <x> <y> [note...]
      現在画面を保存、1タップ、遷移後画面を保存して action_attempt に記録する
  mark-action <action_id> <success|failure|changed|no_change> [note...]
      tap-probe 結果に後からラベルを付ける
""")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "observe"
    if mode == "observe":
        pack = observation_pack(dry_run=False)
        print(json.dumps(pack, ensure_ascii=False, indent=2))
        return 0
    if mode == "recent":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        print(json.dumps(recent(limit), ensure_ascii=False, indent=2))
        return 0
    if mode == "label":
        if len(sys.argv) < 4:
            help_text()
            return 2
        label_screen(int(sys.argv[2]), sys.argv[3], " ".join(sys.argv[4:]))
        print(json.dumps({"ok": True, "screen_id": int(sys.argv[2]), "label": sys.argv[3]}, ensure_ascii=False))
        return 0
    if mode == "tap-probe":
        if len(sys.argv) < 4:
            help_text()
            return 2
        result = tap_probe(int(sys.argv[2]), int(sys.argv[3]), " ".join(sys.argv[4:]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if mode == "mark-action":
        if len(sys.argv) < 4:
            help_text()
            return 2
        update_action_result(int(sys.argv[2]), sys.argv[3], " ".join(sys.argv[4:]))
        print(json.dumps({"ok": True, "action_id": int(sys.argv[2]), "result": sys.argv[3]}, ensure_ascii=False))
        return 0
    if mode in ("help", "-h", "--help"):
        help_text()
        return 0
    help_text()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
