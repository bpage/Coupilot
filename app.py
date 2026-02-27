import re
import uuid
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
from markupsafe import Markup, escape
from database import (
    init_db, add_task, get_pending_tasks, get_tasks_by_date,
    delete_task, toggle_task, mark_tasks_completed, get_task_description,
    get_task_board, add_recurring_task, get_recurring_tasks,
    delete_recurring_task, create_due_recurring_tasks,
)
from agent import generate_daily_mission, send_sms, chat_reply, BOARDS, get_nudge_target

app = Flask(__name__)


def linkify_products(task):
    """Replace detected product names in task description with Amazon search links."""
    import json
    description = task.get('description', '') if isinstance(task, dict) else task
    products = task.get('products', '[]') if isinstance(task, dict) else '[]'
    try:
        product_list = json.loads(products) if isinstance(products, str) else products
    except Exception:
        product_list = []

    result = str(escape(description))
    for product in product_list:
        if not product:
            continue
        query = product.replace(' ', '+')
        link = (
            f'<a href="https://www.amazon.com/s?k={query}" target="_blank" '
            f'class="amazon-link">{escape(product)}</a>'
        )
        result = re.sub(re.escape(product), link, result, flags=re.IGNORECASE, count=1)
    return Markup(result)

app.jinja_env.filters['linkify'] = linkify_products

# Run migrations on startup (works with gunicorn too)
init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    board_id = request.args.get("board", 1, type=int)
    if board_id not in (1, 2):
        board_id = 1

    if request.method == "POST":
        task_text = request.form.get("task", "").strip()
        if task_text:
            add_task(task_text, board_id=board_id)
        return redirect(url_for("index", board=board_id))

    create_due_recurring_tasks()
    tasks_by_date = get_tasks_by_date(board_id=board_id)
    has_pending = any(
        not t["completed"] for group in tasks_by_date.values() for t in group
    )
    # Nudge target is the current board's person
    nudge_target_name = BOARDS[board_id]["name"]
    recurring = get_recurring_tasks(board_id=board_id)

    return render_template("index.html", tasks_by_date=tasks_by_date,
                           has_pending=has_pending, status=None, status_type=None,
                           board_id=board_id, boards=BOARDS,
                           nudge_target_name=nudge_target_name,
                           recurring_tasks=recurring)


@app.route("/toggle/<int:task_id>", methods=["POST"])
def toggle(task_id):
    new_status = toggle_task(task_id)
    if new_status == -1:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"completed": new_status})


@app.route("/send", methods=["POST"])
def send_mission():
    board_id = request.args.get("board", 1, type=int)
    if board_id not in (1, 2):
        board_id = 1
    tasks = get_pending_tasks(board_id=board_id)
    if not tasks:
        return jsonify({"error": "No pending tasks to send"}), 400
    try:
        mission = generate_daily_mission(tasks)
        target = get_nudge_target(board_id)
        send_sms(mission, to_number=target)
        return jsonify({"success": True, "message": "Daily Mission sent!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/send_task/<int:task_id>", methods=["POST"])
def send_single_task(task_id):
    try:
        task_board = get_task_board(task_id)
        mission = generate_daily_mission([{"description": get_task_description(task_id)}])
        target = get_nudge_target(task_board)
        send_sms(mission, to_number=target)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "default")
    print(f"[WHATSAPP] From: {sender} | Message: {incoming_msg}")
    if not incoming_msg:
        return Response(status=200)
    try:
        reply = chat_reply(incoming_msg, sender=sender)
        print(f"[WHATSAPP] Reply: {reply[:100]}...")
        send_sms(reply, to_number=sender)
        print("[WHATSAPP] Sent successfully")
    except Exception as e:
        print(f"[WHATSAPP] Error: {e}")
    return Response(status=200)


@app.route("/delete/<int:task_id>", methods=["POST"])
def remove_task(task_id):
    delete_task(task_id)
    return jsonify({"success": True})


@app.route("/recurring", methods=["POST"])
def add_recurring():
    data = request.get_json()
    desc = (data.get("description") or "").strip()
    day = data.get("day_of_week")
    board_id = data.get("board_id", 1)
    if not desc or day is None:
        return jsonify({"error": "Missing description or day"}), 400
    add_recurring_task(desc, int(day), int(board_id))
    return jsonify({"success": True})


@app.route("/recurring/<int:recurring_id>", methods=["DELETE"])
def remove_recurring(recurring_id):
    delete_recurring_task(recurring_id)
    return jsonify({"success": True})


@app.route("/ical")
def ical_event():
    import datetime
    name = request.args.get("name", "Event")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return "Missing parameters", 400
    now = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uid = f"{uuid.uuid4()}@coupilot"
    # Build the raw ICS content for JavaScript blob
    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Coupilot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        f"SUMMARY:{name}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    ics_escaped = "\\r\\n".join(ics_lines)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Add to Calendar</title>
  <style>
    body {{
      font-family: -apple-system, sans-serif;
      background: #f2f2f7;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      min-height: 100vh; margin: 0; padding: 24px; box-sizing: border-box;
    }}
    .card {{
      background: white; border-radius: 20px; padding: 32px 24px;
      text-align: center; max-width: 360px; width: 100%;
      box-shadow: 0 2px 20px rgba(0,0,0,0.08);
    }}
    .icon {{ font-size: 48px; margin-bottom: 12px; }}
    h2 {{ margin: 0 0 6px; font-size: 20px; font-weight: 700; color: #111; }}
    .sub {{ margin: 0 0 24px; font-size: 15px; color: #888; }}
    .btn {{
      display: block; background: #007AFF; color: white;
      padding: 16px; border-radius: 14px; text-decoration: none;
      font-size: 17px; font-weight: 600; border: none; cursor: pointer;
      width: 100%; box-sizing: border-box;
    }}
    .btn:active {{ background: #0062cc; }}
    .status {{ margin-top: 16px; font-size: 14px; color: #888; display: none; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">📅</div>
    <h2>{name}</h2>
    <p class="sub">Tap to add this event to your calendar</p>
    <button class="btn" onclick="addToCal()">Add to Calendar</button>
    <p class="status" id="status"></p>
  </div>
  <script>
    function addToCal() {{
      var ics = "{ics_escaped}";
      var blob = new Blob([ics], {{type: "text/calendar;charset=utf-8"}});
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = "event.ics";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }}
    // Auto-trigger on load
    setTimeout(addToCal, 500);
  </script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
