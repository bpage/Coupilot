import os
import re
import json
import time
import urllib.request
from urllib.parse import quote_plus
import google.generativeai as genai
from twilio.rest import Client
from dotenv import load_dotenv
from database import get_pending_tasks, mark_tasks_completed

load_dotenv()  # loads from env vars on Render, or .env locally

# --- Configuration ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]
MY_WHATSAPP_NUMBER = os.environ["MY_WHATSAPP_NUMBER"]
SERVER_URL = os.environ.get("SERVER_URL", "http://187.77.14.36:8080")

# --- Board Configuration ---
BOARD1_NAME = os.environ.get("BOARD1_NAME", "Brett")
BOARD1_WHATSAPP = os.environ.get("BOARD1_WHATSAPP", MY_WHATSAPP_NUMBER)
BOARD2_NAME = os.environ.get("BOARD2_NAME", "Ilona")
BOARD2_WHATSAPP = os.environ.get("BOARD2_WHATSAPP", MY_WHATSAPP_NUMBER)

BOARDS = {
    1: {"name": BOARD1_NAME, "whatsapp": BOARD1_WHATSAPP},
    2: {"name": BOARD2_NAME, "whatsapp": BOARD2_WHATSAPP},
}


def get_nudge_target(board_id: int) -> str:
    """Nudge goes to the board owner's WhatsApp."""
    return BOARDS[board_id]["whatsapp"]

# --- Conversation Memory ---
_conversations = {}
CONVERSATION_TIMEOUT = 30 * 60  # 30 minutes

TRAVEL_RE = re.compile(
    r'\b(flight|flights|fly|flying|trip|travel|vacation|vacay|cruise|holiday|'
    r'getaway|cabo|cancun|miami|nyc|new york|paris|london|tokyo|hawaii|vegas|'
    r'chicago|austin|nashville|seattle|denver|rome|bali|europe|mexico|'
    r'puerto rico|costa rica|bahamas|jamaica|tulum|scottsdale|san francisco|'
    r'portland|boston|atlanta|phoenix|san diego|barcelona|amsterdam|dubai)\b',
    re.IGNORECASE
)

MONTHS_RE = re.compile(
    r'\b(january|february|march|april|may|june|july|august|september|october|'
    r'november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b',
    re.IGNORECASE
)

# Noise words that trail a destination: "with Audrey", "w/ John", "for us", etc.
_DEST_NOISE_RE = re.compile(
    r'\s+(?:with|w/|w\b|for|via|by|featuring|and\s+\w+\s+(?:and|with)|feat\.?).*$',
    re.IGNORECASE
)

# Patterns that follow a travel preposition: "flights to X", "trip to X", etc.
_TO_RE = re.compile(
    r'(?:flights?\s+to|fly(?:ing)?\s+to|trip\s+to|travel\s+to|going\s+to|'
    r'vacation\s+in|visit(?:ing)?\s+|book(?:ing)?\s+(?:\w+\s+)?to)\s+'
    r'([A-Za-z][A-Za-z\s]{1,30}?)(?=\s+(?:in|for|on|next|this|the|with|w/|\d)|[,\.!?]|$)',
    re.IGNORECASE
)

# Patterns where destination comes BEFORE the travel noun: "Cabo trip", "Paris vacation"
_DEST_FIRST_RE = re.compile(
    r'(?:^|[\s,]+)([A-Za-z][A-Za-z\s]{0,25}?)\s+(?:trip|vacation|getaway|holiday|cruise)\b',
    re.IGNORECASE
)



def build_prompt(tasks: list[dict]) -> str:
    task_list = "\n".join(f"- {t['description']}" for t in tasks)
    return f"""You are a warm, thoughtful relationship assistant. Your partner has added
the following household tasks for today:

{task_list}

Generate a short "Daily Mission" message (max 3-4 sentences) that:
1. Acknowledges the tasks naturally (don't just list them back).
2. Weaves in one small, specific romantic gesture or act of service
   the recipient could do today (e.g., leave a note, make their
   partner's favorite drink, send a sweet text at lunch).
3. Ends with an encouraging or affectionate sign-off.

Keep the tone casual, warm, and genuine — not cheesy. Write it as a
text message, not a formal letter. Output ONLY the message text — no commentary, no explanations, no meta-notes."""


# Common city abbreviations to expand
_ABBREVS = {
    'ny': 'New York', 'nyc': 'New York City', 'la': 'Los Angeles',
    'sf': 'San Francisco', 'lv': 'Las Vegas', 'dc': 'Washington DC',
    'chi': 'Chicago', 'nola': 'New Orleans', 'phx': 'Phoenix',
}

def _normalize_dest(dest: str) -> str:
    """Expand abbreviations and title-case a destination."""
    lower = dest.strip().lower()
    if lower in _ABBREVS:
        return _ABBREVS[lower]
    # Expand abbreviations embedded as whole words
    words = dest.strip().split()
    expanded = [_ABBREVS.get(w.lower(), w) for w in words]
    return ' '.join(expanded).title()


def _clean_and_split(raw: str) -> list[str]:
    """Strip noise, split compound destinations on 'and', normalize each."""
    # Strip trailing companion/context words
    cleaned = _DEST_NOISE_RE.sub('', raw).strip()
    # Remove trailing single letters (e.g. "Paris W" → "Paris")
    cleaned = re.sub(r'\s+[A-Za-z]$', '', cleaned).strip()
    # Split on " and " to handle "NY and Paris" → ["NY", "Paris"]
    parts = re.split(r'\s+and\s+', cleaned, flags=re.IGNORECASE)
    return [_normalize_dest(p) for p in parts if p.strip()]


def extract_travel(tasks: list[dict]) -> list[dict]:
    """Pure regex travel detection — no AI call, nothing to fail silently."""
    results = []
    for task in tasks:
        desc = task.get('description', '')
        if not TRAVEL_RE.search(desc):
            continue

        print(f"[TRAVEL] Detected travel in: {desc!r}")

        raw_dest = None

        # Try "flights to X" / "trip to X" pattern first
        m = _TO_RE.search(desc)
        if m:
            raw_dest = m.group(1)
            print(f"[TRAVEL] Raw dest (TO pattern): {raw_dest!r}")

        # Try "Cabo trip" / "Paris vacation" pattern
        if not raw_dest:
            m = _DEST_FIRST_RE.search(desc)
            if m:
                raw_dest = m.group(1)
                print(f"[TRAVEL] Raw dest (DEST-FIRST pattern): {raw_dest!r}")

        if not raw_dest:
            print(f"[TRAVEL] Could not extract destination, skipping.")
            continue

        destinations = _clean_and_split(raw_dest)
        dm = MONTHS_RE.search(desc)
        date = dm.group(1).title() if dm else None
        print(f"[TRAVEL] Destinations: {destinations}, date: {date!r}")

        for dest in destinations:
            results.append({"destination": dest, "date": date})

    return results


def build_flight_link(destination: str, date: str | None) -> str:
    """Build a Google Flights search URL in 'Origin to Destination' format."""
    query = f"Los Angeles to {destination}"
    if date:
        query += f" {date}"
    return "https://www.google.com/travel/flights?q=" + query.replace(" ", "+")


def generate_daily_mission(tasks: list[dict]) -> str:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(build_prompt(tasks))
            mission = response.text.strip()

            # Append flight links for any travel tasks
            trips = extract_travel(tasks)
            if trips:
                flight_lines = []
                for trip in trips:
                    dest = (trip.get("destination") or "").strip()
                    date = (trip.get("date") or "").strip()
                    if not dest:
                        continue
                    url = build_flight_link(dest, date or None)
                    label = f"{dest} ({date})" if date else dest
                    flight_lines.append(f"✈️ Flights: {label} → {url}")
                if flight_lines:
                    mission += "\n\n" + "\n".join(flight_lines)

            # Append Amazon links for any product tasks
            shop_lines = []
            for task in tasks:
                products = extract_products(task.get("description", ""))
                for product in products:
                    url = "https://www.amazon.com/s?k=" + product.replace(" ", "+")
                    shop_lines.append(f"🛒 Shop: {product.title()} → {url}")
            if shop_lines:
                mission += "\n\n" + "\n".join(shop_lines)

            # Append calendar links for any event tasks with a specific date+time
            today_str = time.strftime("%Y-%m-%d")
            cal_lines = []
            for task in tasks:
                event = extract_event(task.get("description", ""), today_str)
                if event:
                    acal = build_apple_cal_link(event)
                    gcal = build_calendar_link(event)
                    cal_lines.append(
                        f"📅 {event['name']}\n"
                        f"   Apple Cal → {acal}\n"
                        f"   Google Cal → {gcal}"
                    )
            if cal_lines:
                mission += "\n\n" + "\n".join(cal_lines)

            return mission
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = (attempt + 1) * 10
                print(f"[GEMINI] Rate limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


SYSTEM_PROMPT = """You are a direct, actionable relationship copilot. The user lives in
Los Angeles (90065 — Eagle Rock / Highland Park area).

Rules:
- Give specific names, places, and actions — never generic advice.
- When recommending restaurants, bars, or activities, use real places near their area.
- Keep responses short: bullet points or a quick list. No fluff, no preambles.
- Warm but efficient — like texting a friend who always has the best recs.
- When the user asks for more, a wider area, different cuisine, etc., build on what you already suggested — don't repeat yourself.
- Keep responses under 1500 characters so they fit in a single WhatsApp message."""


def chat_reply(user_message: str, sender: str = "default") -> str:
    genai.configure(api_key=GEMINI_API_KEY)
    now = time.time()

    # Clean up expired conversations
    expired = [k for k, v in _conversations.items() if now - v["last"] > CONVERSATION_TIMEOUT]
    for k in expired:
        del _conversations[k]

    # Get or create conversation for this sender
    if sender in _conversations and now - _conversations[sender]["last"] < CONVERSATION_TIMEOUT:
        chat = _conversations[sender]["chat"]
    else:
        model = genai.GenerativeModel("gemini-2.0-flash", system_instruction=SYSTEM_PROMPT)
        chat = model.start_chat()
        _conversations[sender] = {"chat": chat, "last": now}

    _conversations[sender]["last"] = now

    for attempt in range(3):
        try:
            response = chat.send_message(user_message)
            return response.text.strip()
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                print(f"[GEMINI] Rate limited, retrying in {(attempt + 1) * 5}s...")
                time.sleep((attempt + 1) * 5)
            else:
                raise


def extract_products(text: str) -> list:
    """Use Gemini to extract purchasable product names from a task description."""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""Extract any purchasable product names from this task description.
Return only a JSON array of product name strings (no explanation).
If there are no products, return [].

Examples:
"buy new air fryer for booba" → ["air fryer"]
"get a roomba for the living room" → ["Roomba"]
"call the dentist" → []
"need instant pot and new vacuum" → ["Instant Pot", "vacuum"]

Task: "{text}"
"""
    try:
        response = model.generate_content(prompt)
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', response.text.strip())
        raw = re.sub(r'\s*```$', '', raw).strip()
        return json.loads(raw)
    except Exception:
        return []


def extract_event(text: str, today: str) -> dict | None:
    """Use Gemini to extract a calendar event from a task description.
    Returns dict with 'name', 'start', 'end' in YYYYMMDDTHHMMSS format, or None.
    """
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""Today is {today}.
Extract calendar event details from this task description.
Return a JSON object or null.

If the task mentions a specific date AND a specific time, return:
{{"name": "event title", "date": "YYYYMMDD", "start_time": "HHMM", "end_time": "HHMM"}}

Rules:
- "name": clean event title (e.g., "Dinner with Joe and Bob")
- "date": absolute date YYYYMMDD (resolve "this Friday", "tomorrow", "next Tuesday", etc.)
- "start_time": 24h HHMM (7pm → "1900", noon → "1200", 8:30pm → "2030")
- "end_time": start + 1 hour by default, unless a duration is specified
- If NO specific time is mentioned, return null (a date alone is not enough)
- If NO specific date is mentioned (just a time), return null

Examples:
"Dinner with Joe and Bob this Friday at 7pm" → {{"name": "Dinner with Joe and Bob", "date": "20260220", "start_time": "1900", "end_time": "2000"}}
"buy groceries" → null
"dentist appointment" → null
"call mom on Saturday" → null
"meeting tomorrow at 2pm for 2 hours" → {{"name": "Meeting", "date": "20260219", "start_time": "1400", "end_time": "1600"}}

Task: "{text}"
Return ONLY the JSON object or the word null. No explanation."""
    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r'^```[a-zA-Z]*\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw).strip()
        if raw.lower() == 'null':
            return None
        data = json.loads(raw)
        if not data or not data.get('name') or not data.get('date'):
            return None
        d = data['date']
        st = str(data.get('start_time', '0000')).zfill(4)
        et = str(data.get('end_time', '0100')).zfill(4)
        return {
            'name': data['name'],
            'start': f"{d}T{st}00",
            'end': f"{d}T{et}00",
        }
    except Exception:
        return None


def build_calendar_link(event: dict) -> str:
    """Build a Google Calendar quick-add URL."""
    text = quote_plus(event['name'])
    dates = f"{event['start']}/{event['end']}"
    return f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={text}&dates={dates}"


def shorten_url(long_url: str) -> str:
    """Shorten a URL via TinyURL so WhatsApp makes it tappable."""
    try:
        api = "https://tinyurl.com/api-create.php?url=" + quote_plus(long_url)
        resp = urllib.request.urlopen(api, timeout=5)
        return resp.read().decode().strip()
    except Exception:
        return long_url  # fallback to original if shortener fails


def build_apple_cal_link(event: dict) -> str:
    """Build an Apple Calendar link, shortened so WhatsApp makes it tappable."""
    params = f"name={quote_plus(event['name'])}&start={event['start']}&end={event['end']}"
    long_url = f"{SERVER_URL}/ical?{params}"
    return shorten_url(long_url)


def send_sms(body: str, to_number: str = None):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    target = to_number or MY_WHATSAPP_NUMBER
    try:
        message = client.messages.create(
            body=body,
            from_=TWILIO_WHATSAPP_FROM,
            to=target,
        )
        print(f"[TWILIO] Sent OK to {target}: {message.sid}")
        return message.sid
    except Exception as e:
        print(f"[TWILIO] FAILED sending to {target}: {e}")
        raise


def main():
    tasks = get_pending_tasks()

    if not tasks:
        print("No pending tasks. Skipping.")
        return

    print(f"Found {len(tasks)} pending task(s). Generating mission...")
    mission = generate_daily_mission(tasks)
    print(f"Daily Mission:\n{mission}\n")

    sid = send_sms(mission)
    print(f"WhatsApp message sent! SID: {sid}")


if __name__ == "__main__":
    main()
