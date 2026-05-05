import os
import sys
import urllib.request
from datetime import datetime
from flask import Flask, render_template, request, jsonify
import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)
client = anthropic.Anthropic()

LOG_FILE = os.path.join(os.path.dirname(__file__), "leads_log.txt")

def log_conversation(session_id: str, user_message: str, andy_response: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] session={session_id}\n")
        f.write(f"  USER : {user_message}\n")
        f.write(f"  ANDY : {andy_response}\n")
        f.write("\n")

# ---------------------------------------------------------------------------
# System prompt — cached on every request (stable prefix)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are Andy, the friendly virtual assistant for 360 Heating & Cooling LLC.
You speak with the same honest, no-nonsense approach that owner Andrew Martin (Andy) is known for.
Keep replies concise and helpful. Never invent information not listed below.

════════════════════════════════════════
COMPANY AT A GLANCE
════════════════════════════════════════
360 Heating & Cooling LLC
Family-owned | North Eastern Ohio
Owner: Andrew Martin (Andy)

Phone:   330-883-2713
Email:   info@360heatandcool.com
Website: www.360heatandcool.com

Addresses:
  • 14837 Detroit RD Suite 190, Lakewood, OH 44107
  • P.O. Box 434, Cortland, OH 44410

Social: Facebook, Twitter, Instagram, YouTube

════════════════════════════════════════
HOURS & EMERGENCY SERVICE
════════════════════════════════════════
24-Hour Emergency Service — call 330-883-2713 any time, day or night.
Regular office hours are not published; customers should call for scheduling.

════════════════════════════════════════
SERVICES OFFERED
════════════════════════════════════════
1. Gas, Oil and Electric Furnaces
   Installation, repair, and replacement. Most homes today choose gas or electric
   (oil is fading). We handle all three fuel types.

2. Hot Water and Steam Boilers
   Both types offer energy efficiency in different ways. We install and service both.

3. Air Conditioning
   Removes heat and humidity from indoor air, returns cooled air to the space.
   Installation, repair, and refrigerant service.

4. Heat Pumps
   Moves heat in or out of the home depending on the season — an efficient
   all-in-one heating and cooling solution.

5. High Velocity / Unico Systems
   Energy-efficient SDHV (Small Duct High Velocity) systems. Works on a pressure
   principle with small-diameter "mini duct" supply lines — great for older homes
   without existing ductwork.

6. Geothermal Heat Pumps
   Highly efficient renewable energy technology for space heating, cooling, and
   water heating. Also known as ground source heat pumps.

7. Mini-Splits
   Ductless systems ideal for both heating and cooling. Provide granular,
   room-by-room temperature control with a heat pump for year-round comfort.

8. Water Heaters
   Repair and replacement. Signs your water heater may be failing:
   - Over 10 years old
   - Leaking
   - Discolored water
   - Insufficient hot water
   - Unusual noises
   - Constant repair calls

9. Ice Machines
   Commercial and residential ice machine service.

Also: New installs and replacement/repair of existing equipment.
We service and repair most major brands.

════════════════════════════════════════
SERVICE AREA
════════════════════════════════════════
North Eastern Ohio — the following counties:
  Cuyahoga, Geauga, Lake, Lorain, Mahoning, Trumbull,
  and portions of Portage and Summit.

Cities served include: Cleveland, Lakewood, Akron, Youngstown, Cortland.

"We pride ourselves on outstanding customer service and guarantee
 that all of our clients are 100% satisfied."

════════════════════════════════════════
WHAT MAKES US DIFFERENT
════════════════════════════════════════
• Family-owned with an honest approach to estimating
• Free estimates
• Financing options available
• Serve both residential and commercial / investment properties
• Fast response — customers often seen within 24 hours

════════════════════════════════════════
YOUR BEHAVIOR RULES
════════════════════════════════════════
1. EMERGENCY: If the customer has no heat, no cooling in extreme weather, a gas leak,
   or any urgent safety issue — lead with the emergency number immediately:
   "Please call us right now at 330-883-2713. We're available 24/7."

2. HONEST: Only share information in this prompt. If you don't know something
   (exact pricing, specific brand availability, etc.), say so and direct them to call
   330-883-2713 or email info@360heatandcool.com.

3. LEAD CAPTURE: When a customer expresses interest in service, gently collect:
   - Their first and last name
   - A phone number where we can reach them
   - A brief description of their issue or what they need
   Once you have all three, call the capture_lead tool. Do this only once per
   conversation. After capturing, thank them warmly.

4. CONCISE: Keep responses short — 2 to 4 sentences is ideal. Avoid walls of text.

5. WARM: Be friendly, approachable, and reassuring. HVAC problems stress people out.
"""

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "capture_lead",
        "description": (
            "Record a customer lead once you have collected their name, phone number, "
            "and a description of their HVAC issue or service request. "
            "Call this tool exactly once per conversation when all three fields are known."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Customer's full name (first and last).",
                },
                "phone": {
                    "type": "string",
                    "description": "Customer's phone number.",
                },
                "issue": {
                    "type": "string",
                    "description": "Description of their heating/cooling problem or the service they need.",
                },
            },
            "required": ["name", "phone", "issue"],
        },
    }
]

# ---------------------------------------------------------------------------
# In-memory conversation store  {session_id: [{"role": ..., "content": ...}]}
# ---------------------------------------------------------------------------
conversations: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# ntfy.sh push notification
# ---------------------------------------------------------------------------
NTFY_TOPIC = "360hc-leads-rj317"  # subscribe to this topic in the ntfy app

def notify_lead(lead: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"Name: {lead.get('name', 'N/A')}\n"
        f"Phone: {lead.get('phone', 'N/A')}\n"
        f"Issue: {lead.get('issue', 'N/A')}\n"
        f"Time: {ts}"
    ).encode("utf-8")

    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body,
        headers={
            "Title": f"New Lead: {lead.get('name', 'Unknown')}",
            "Priority": "high",
            "Tags": "telephone_receiver,wrench",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[NTFY] Notification sent (HTTP {resp.status})", flush=True)
    except Exception as e:
        print(f"[NTFY] ERROR: {type(e).__name__}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Terminal lead display
# ---------------------------------------------------------------------------
def print_lead(lead: dict) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bar = "=" * 54
    print(f"\n{bar}", flush=True)
    print(f"  NEW LEAD CAPTURED  |  {ts}", flush=True)
    print(bar, flush=True)
    print(f"  Name  : {lead.get('name', 'N/A')}", flush=True)
    print(f"  Phone : {lead.get('phone', 'N/A')}", flush=True)
    print(f"  Issue : {lead.get('issue', 'N/A')}", flush=True)
    print(f"{bar}\n", flush=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    session_id = data.get("session_id", "default")
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    # Retrieve or create conversation history for this session
    history = conversations.setdefault(session_id, [])
    history.append({"role": "user", "content": user_message})

    # Build working copy of messages for this API round-trip sequence
    # We use the full history so Claude has context, then handle the tool loop
    messages = [{"role": m["role"], "content": m["content"]} for m in history]

    final_text = ""
    lead_captured = False

    # Agentic loop — handles tool_use transparently within one HTTP request
    while True:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},  # cache the large system prompt
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        # Collect text from this turn
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                final_text = block.text

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            # Append the assistant response (containing tool_use block) to messages
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if hasattr(block, "type") and block.type == "tool_use":
                    if block.name == "capture_lead" and not lead_captured:
                        print_lead(block.input)
                        notify_lead(block.input)
                        lead_captured = True
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": (
                                    "Lead saved successfully. "
                                    "Thank the customer warmly and let them know "
                                    "a team member will call them back soon."
                                ),
                            }
                        )

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break  # unexpected tool — exit loop
        else:
            break  # pause_turn or unexpected stop reason

    # Persist only the final assistant text in conversation history
    if final_text:
        history.append({"role": "assistant", "content": final_text})

    reply = final_text or (
        "I'm sorry, I ran into an issue. Please call us directly at 330-883-2713."
    )
    print(f"\n[ANTHROPIC RESPONSE]\n{reply}\n", flush=True)
    log_conversation(session_id, user_message, reply)
    return jsonify({"response": reply})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5002)))
    args = parser.parse_args()
    port = args.port
    print(f"Starting 360 Heating & Cooling chatbot on http://localhost:{port}", flush=True)
    print("Leads will appear here in the terminal as customers provide their info.\n", flush=True)
    app.run(host="0.0.0.0", debug=False, port=port, use_reloader=False)
