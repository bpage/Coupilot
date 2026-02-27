import os
import psycopg2
from collections import OrderedDict
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def _row_to_dict(cur):
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            description TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed INTEGER DEFAULT 0,
            products TEXT DEFAULT '[]',
            board_id INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recurring_tasks (
            id SERIAL PRIMARY KEY,
            description TEXT NOT NULL,
            day_of_week INTEGER NOT NULL,
            board_id INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def add_task(description: str, products: list = None, board_id: int = 1):
    import json
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (description, products, board_id) VALUES (%s, %s, %s)",
        (description, json.dumps(products or []), board_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_pending_tasks(board_id: int = None):
    conn = get_connection()
    cur = conn.cursor()
    if board_id is not None:
        cur.execute(
            "SELECT id, description, created_at FROM tasks WHERE completed = 0 AND board_id = %s ORDER BY created_at",
            (board_id,)
        )
    else:
        cur.execute(
            "SELECT id, description, created_at FROM tasks WHERE completed = 0 ORDER BY created_at"
        )
    rows = _row_to_dict(cur)
    cur.close()
    conn.close()
    return rows


def get_all_tasks(board_id: int = None):
    conn = get_connection()
    cur = conn.cursor()
    if board_id is not None:
        cur.execute(
            "SELECT id, description, created_at, completed FROM tasks WHERE board_id = %s ORDER BY created_at DESC",
            (board_id,)
        )
    else:
        cur.execute(
            "SELECT id, description, created_at, completed FROM tasks ORDER BY created_at DESC"
        )
    rows = _row_to_dict(cur)
    cur.close()
    conn.close()
    return rows


def get_tasks_by_date(board_id: int = None):
    """Group tasks into sections: Outstanding (overdue incomplete), Today, Yesterday, older dates.
    Outstanding always comes first if there are any."""
    tasks = get_all_tasks(board_id=board_id)
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    outstanding = []
    today_tasks = []
    yesterday_tasks = []
    older = OrderedDict()

    for task in tasks:
        try:
            ca = task["created_at"]
            if isinstance(ca, str):
                task_date = datetime.strptime(ca, "%Y-%m-%d %H:%M:%S").date()
            else:
                task_date = ca.date()
        except (ValueError, TypeError):
            task_date = today

        if not task["completed"] and task_date < today:
            outstanding.append(task)
        elif task_date == today:
            today_tasks.append(task)
        elif task_date == yesterday:
            yesterday_tasks.append(task)
        else:
            label = task_date.strftime("%B %d, %Y")
            older.setdefault(label, []).append(task)

    grouped = OrderedDict()
    if outstanding:
        grouped["Outstanding"] = outstanding
    if today_tasks:
        grouped["Today"] = today_tasks
    if yesterday_tasks:
        grouped["Yesterday"] = yesterday_tasks
    for label, tasks_list in older.items():
        grouped[label] = tasks_list

    return grouped


def get_task_board(task_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT board_id FROM tasks WHERE id = %s", (task_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 1


def toggle_task(task_id: int) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT completed FROM tasks WHERE id = %s", (task_id,))
    row = cur.fetchone()
    if row is None:
        cur.close()
        conn.close()
        return -1
    new_status = 0 if row[0] else 1
    cur.execute("UPDATE tasks SET completed = %s WHERE id = %s", (new_status, task_id))
    conn.commit()
    cur.close()
    conn.close()
    return new_status


def mark_tasks_completed(task_ids: list[int]):
    if not task_ids:
        return
    conn = get_connection()
    cur = conn.cursor()
    placeholders = ",".join("%s" for _ in task_ids)
    cur.execute(f"UPDATE tasks SET completed = 1 WHERE id IN ({placeholders})", task_ids)
    conn.commit()
    cur.close()
    conn.close()


def get_task_description(task_id: int) -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT description FROM tasks WHERE id = %s", (task_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else ""


def delete_task(task_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()


def add_recurring_task(description: str, day_of_week: int, board_id: int = 1):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recurring_tasks (description, day_of_week, board_id) VALUES (%s, %s, %s)",
        (description, day_of_week, board_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_recurring_tasks(board_id: int = None):
    conn = get_connection()
    cur = conn.cursor()
    if board_id is not None:
        cur.execute(
            "SELECT id, description, day_of_week, board_id FROM recurring_tasks WHERE active = 1 AND board_id = %s",
            (board_id,)
        )
    else:
        cur.execute(
            "SELECT id, description, day_of_week, board_id FROM recurring_tasks WHERE active = 1"
        )
    rows = _row_to_dict(cur)
    cur.close()
    conn.close()
    return rows


def delete_recurring_task(recurring_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recurring_tasks WHERE id = %s", (recurring_id,))
    conn.commit()
    cur.close()
    conn.close()


def create_due_recurring_tasks():
    """Auto-create tasks for today's recurring rules (idempotent)."""
    today = datetime.now()
    today_dow = today.weekday()
    today_str = today.strftime("%Y-%m-%d")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, description, board_id FROM recurring_tasks WHERE active = 1 AND day_of_week = %s",
        (today_dow,)
    )
    rules = _row_to_dict(cur)

    for rule in rules:
        cur.execute(
            "SELECT id FROM tasks WHERE description = %s AND board_id = %s AND created_at::text LIKE %s",
            (rule["description"], rule["board_id"], today_str + "%")
        )
        existing = cur.fetchone()
        if not existing:
            import json
            cur.execute(
                "INSERT INTO tasks (description, products, board_id) VALUES (%s, %s, %s)",
                (rule["description"], json.dumps([]), rule["board_id"])
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized (Postgres)")
