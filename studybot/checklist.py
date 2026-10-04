"""学習メニューの「今日のチェックリスト」。

学習メニューを送るときに今日やること（復習・弱い分野・科目B・目標時間）を
保存しておき、記録が増えると自動でチェックが付く。
"""

import json
import sqlite3
from contextlib import closing


# 弱い分野に割り当てる、今日の問題数（ふだんは一番弱い分野だけ、直前モードは3つ）
WEAK_QUOTA = 10
FINAL_STRETCH_QUOTAS = (20, 10, 10)
# 直前モードで弱い分野がないときの総合演習の問題数
TOTAL_QUOTA = 20


def init_checklist_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_checklists (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            items TEXT NOT NULL,
            PRIMARY KEY (user_id, day)
        )
    """)


def build_checklist_items(qualification, due_count, weak, final_stretch=False,
                          goal_minutes=None):
    """今日やることの一覧。weak は [(分野, 正答率), ...]。"""
    items = []
    if due_count:
        items.append({
            "kind": "review", "label": f"今日の復習 {due_count}問",
            "target": due_count,
        })
    weakest = sorted(weak, key=lambda pair: pair[1])
    quotas = FINAL_STRETCH_QUOTAS if final_stretch else (WEAK_QUOTA,)
    for (category, _), quota in zip(weakest, quotas):
        items.append({
            "kind": "category", "label": f"{category} {quota}問",
            "target": quota, "category": category,
            "qualification": qualification.code,
        })
    if final_stretch and not weakest:
        items.append({
            "kind": "questions", "label": f"総合演習 {TOTAL_QUOTA}問",
            "target": TOTAL_QUOTA, "qualification": qualification.code,
        })
    if final_stretch and qualification.has_part_b:
        items.append({
            "kind": "b", "label": "科目B 1セット", "target": 1,
            "qualification": qualification.code,
        })
    if goal_minutes:
        items.append({
            "kind": "time", "label": f"勉強 {goal_minutes}分",
            "target": goal_minutes,
        })
    return items


def save_checklist(db_path, user_id, day, items):
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_checklists (user_id, day, items)
                VALUES (?, ?, ?)
            """, (user_id, day.isoformat(), json.dumps(items, ensure_ascii=False)))


def get_checklist(db_path, user_id, day):
    """その日のチェックリスト。学習メニューを送っていない日は空。"""
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("""
                SELECT items FROM daily_checklists WHERE user_id = ? AND day = ?
            """, (user_id, day.isoformat())).fetchone()
    except sqlite3.Error:
        return []
    return json.loads(row[0]) if row else []


def item_progress(conn, user_id, day, item):
    """その項目の今日の実績（問題数・セット数・分）。"""
    day_text = day.isoformat()
    kind = item["kind"]
    if kind == "review":
        query = """
            SELECT COUNT(DISTINCT t.mistake_id)
            FROM sg_mistake_attempts AS t
            JOIN sg_mistakes AS m ON m.id = t.mistake_id
            WHERE m.user_id = ? AND t.attempted_on = ?
        """
        params = (user_id, day_text)
    elif kind == "category":
        query = """
            SELECT COALESCE(SUM(c.questions), 0)
            FROM study_log_category_results AS c
            JOIN study_log_analysis AS a ON a.message_id = c.message_id
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND l.study_date = ?
              AND a.qualification = ? AND c.category = ?
        """
        params = (user_id, day_text, item["qualification"], item["category"])
    elif kind == "questions":
        query = """
            SELECT COALESCE(SUM(a.questions), 0)
            FROM study_log_analysis AS a
            JOIN study_logs AS l ON l.message_id = a.message_id
            WHERE a.user_id = ? AND l.study_date = ? AND a.qualification = ?
              AND COALESCE(a.exam_section, 'A') = 'A'
        """
        params = (user_id, day_text, item["qualification"])
    elif kind == "b":
        query = """
            SELECT COUNT(*) FROM sg_b_practice
            WHERE user_id = ? AND practiced_on = ? AND qualification = ?
        """
        params = (user_id, day_text, item["qualification"])
    elif kind == "time":
        query = """
            SELECT COALESCE(SUM(duration_seconds), 0) / 60 FROM study_sessions
            WHERE user_id = ? AND study_date = ?
        """
        params = (user_id, day_text)
    else:
        return 0
    return conn.execute(query, params).fetchone()[0] or 0


def checklist_status(db_path, user_id, day):
    """保存したチェックリストの [(項目, 実績, 済んだか), ...]。なければ空。"""
    return checklist_progress(
        db_path, user_id, day, get_checklist(db_path, user_id, day)
    )


def checklist_progress(db_path, user_id, day, items):
    """items それぞれの [(項目, 実績, 済んだか), ...]。"""
    if not items:
        return []
    with closing(sqlite3.connect(db_path)) as conn:
        status = []
        for item in items:
            try:
                progress = item_progress(conn, user_id, day, item)
            except sqlite3.Error:
                progress = 0
            status.append((item, progress, progress >= item["target"]))
    return status


def format_checklist(status):
    """例：✅ 今日の復習 5問（5/5）。チェックリストがなければ None。"""
    if not status:
        return None
    lines = []
    for item, progress, done in status:
        unit = "分" if item["kind"] == "time" else ""
        mark = "✅" if done else "⬜"
        lines.append(
            f"{mark} {item['label']}（{min(progress, item['target'])}"
            f"/{item['target']}{unit}）"
        )
    return "\n".join(lines)


def checklist_summary(status):
    """例：3/4 完了。"""
    done = sum(1 for _, _, finished in status if finished)
    text = f"{done}/{len(status)} 完了"
    if status and done == len(status):
        text += " 🎉"
    return text
