#!/usr/bin/env python3
"""
TDCallInfo — Publisher-facing call lookup web interface + Teams bot.

Web UI: Publishers authenticate with their Traffic Source ID (Partner ID).
Teams:  Bot responds to @mentions in channels/chats with call lookups.

Env vars required:
    ANTHROPIC_API_KEY     — Anthropic API key
    MICROSOFT_APP_ID      — Azure Bot App ID      (Teams bot)
    MICROSOFT_APP_SECRET  — Azure Bot client secret (Teams bot)
    MICROSOFT_APP_TENANT  — Azure Bot tenant ID   (Teams bot)
"""

import os
import re
import json
import time
import logging
import requests
import anthropic
from collections import defaultdict
from aiohttp import web

try:
    from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
    from botbuilder.schema import Activity
    TEAMS_BOT_AVAILABLE = True
except ImportError:
    TEAMS_BOT_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────
ANT_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
TD_AUTH      = "Basic dGRwdWI1ZDk4N2Q1MTAxOGQyNDE5OGMzYmI1MzE1ZGQ0NDE2MTp0ZHBydmQ2YWQ1NjE0YjM1NTAzMzM2NmMxYzZkYzI0YzM2ZmQ2ZWUwYzhkYzM="
TD_BASE      = "https://elite-calls-com.trackdrive.com/api/v1"
KALIPER_KEY  = "yfkTafG4eMlpeOjJFibycWRWiY8em7Kqx2idyA84TlSKMLTe9o954zOWVd5sjCoW00KLyKELIMuxelWEFn6hFMFbR9eZEc7JfivteEeNkYa7PjxIIuSV2S6YY4OD3EGjf2o6jEfPPJSWH8p678zaCussdH348axRdmKXaiAa449GfaN0G3dHu0rHIAW9YYnZKsxgd5MnwVYWA0a4V43r4PUk6lszeMU6Mv3JaBI7E3KJwruUOQAnoOnwO89GYuMe"
KALIPER_BASE = "https://api.thekaliper.com/api/v1"
MODEL        = "claude-haiku-4-5"
RATE_LIMIT   = 5   # lookups allowed per window
RATE_WINDOW  = 60  # seconds
ADMIN_TS_ID  = "2382"  # bypass partner filter — shows any call

# ── Azure Bot config (Teams) ───────────────────────────────────────────────────
APP_ID     = os.environ.get("MICROSOFT_APP_ID", "")
APP_SECRET = os.environ.get("MICROSOFT_APP_SECRET", "")
APP_TENANT = os.environ.get("MICROSOFT_APP_TENANT", "")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ── Bot adapter (only if env vars + botbuilder are present) ───────────────────
bot_adapter = None
if TEAMS_BOT_AVAILABLE and APP_ID and APP_SECRET:
    _settings = BotFrameworkAdapterSettings(APP_ID, APP_SECRET)
    # Use the app's home tenant for token acquisition instead of the default
    # botframework.com tenant (required for apps registered in a specific tenant)
    if APP_TENANT:
        _settings.channel_auth_tenant = APP_TENANT
        log.info("Using channel_auth_tenant: %s", APP_TENANT)
    bot_adapter = BotFrameworkAdapter(_settings)

    async def _on_turn_error(turn_context, error):
        log.error("Teams bot turn error: %s", error, exc_info=True)
        try:
            await turn_context.send_activity(f"⚠️ Bot error: {error}")
        except Exception:
            pass

    bot_adapter.on_turn_error = _on_turn_error
    log.info("Teams bot adapter initialized (App ID: %s)", APP_ID)
else:
    log.info("Teams bot adapter NOT initialized (missing env vars or botbuilder package)")

# ── Rate limiting ──────────────────────────────────────────────────────────────
rate_tracker: dict[str, list[float]] = defaultdict(list)

def check_rate_limit(ts_id: str) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    now = time.time()
    rate_tracker[ts_id] = [t for t in rate_tracker[ts_id] if now - t < RATE_WINDOW]
    if len(rate_tracker[ts_id]) >= RATE_LIMIT:
        return False
    rate_tracker[ts_id].append(now)
    return True

# ── Scrubbing ──────────────────────────────────────────────────────────────────
SCRUB_FIELDS = {
    # Buyer / advertiser identity
    "buyer", "buyer_id", "user_buyer_id", "buyer_converted", "buyer_repeat_caller",
    # Revenue / pricing
    "revenue", "buyer_revenue", "trackdrive_cost", "provider_cost",
    "payout", "traffic_source_payout",
    # Traffic source name (keep ID only)
    "traffic_source", "user_traffic_source_id",
    # Raw durations we don't want to expose (use answered_duration instead)
    "total_duration", "hold_duration", "ivr_duration", "attempted_duration", "agent_duration",
}

LOG_SCRUB_PATTERNS = [
    (r"\[Buyer![^\]]+\]", "[Advertiser]"),
    (r"\[BuyerConversion![^\]]+\]", "[AdConversion]"),
    (r'"revenue"\s*:\s*"?[\d.]+?"?', '"revenue":"[hidden]"'),
    (r'"payout"\s*:\s*"?[\d.]+?"?', '"payout":"[hidden]"'),
    (r"(PING|buyer conversion).{0,60}?'([^']{4,})'", r"\1 [Advertiser]"),
    (r"buyer conversion \(Revenue CPL\) converted \d+ '.+?'", "buyer conversion converted [hidden]"),
    (r"\$[\d.]+", "[hidden]"),
    (r'\b\d+\.\d{2,}\b', "[hidden]"),   # bare decimal amounts that may be dollar values
]

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")

def scrub_call(call: dict) -> dict:
    clean = {}
    for k, v in call.items():
        if k in SCRUB_FIELDS or k.startswith("token-"):
            continue
        clean[k] = v
    for field in ("offer", "name"):
        if field in clean and clean[field]:
            clean[field] = clean[field].replace("&amp;", "&")
    # Use the partner-visible ID (user_traffic_source_id) for display
    user_ts_id = call.get("user_traffic_source_id")
    if user_ts_id:
        clean["traffic_source"] = f"TS#{user_ts_id}"
    # Expose only the forwarded/connected duration (time actually with advertiser)
    answered = call.get("answered_duration")
    if answered is not None:
        clean["forwarded_duration_seconds"] = answered
    return clean

def scrub_log(messages: list[str]) -> list[str]:
    cleaned = []
    for msg in messages:
        for pattern, replacement in LOG_SCRUB_PATTERNS:
            msg = re.sub(pattern, replacement, msg, flags=re.I)
        cleaned.append(msg)
    return cleaned

# ── Tools ──────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "lookup_call_by_uuid",
        "description": "Look up a call in Trackdrive by UUID. Returns offer, duration, conversion status.",
        "input_schema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"]
        }
    },
    {
        "name": "lookup_call_by_phone",
        "description": "Look up calls in Trackdrive by caller phone number.",
        "input_schema": {
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"]
        }
    },
    {
        "name": "get_call_log",
        "description": "Get the call log for a Trackdrive call by numeric ID. Use to diagnose non-conversions.",
        "input_schema": {
            "type": "object",
            "properties": {"call_id": {"type": "integer"}},
            "required": ["call_id"]
        }
    },
    {
        "name": "get_kaliper_call",
        "description": "Get Kaliper data for a call by its Trackdrive UUID (sourceId). Returns buyer ping responses, blacklist flags, QC results, and call quality data. Always call this for every call found — whether you found it by UUID or by phone number. Use the 'uuid' field from the TD call record.",
        "input_schema": {
            "type": "object",
            "properties": {"uuid": {"type": "string"}},
            "required": ["uuid"]
        }
    }
]

def match_ts(call: dict, ts_id: str) -> bool:
    """Check if a call belongs to the given partner (user_traffic_source_id)."""
    return str(call.get("user_traffic_source_id", "")) == str(ts_id)

def run_tool(name: str, params: dict, ts_id_filter: str | None = None, is_admin: bool = False) -> dict:
    # ADMIN_TS_ID bypasses partner matching — treat as no filter
    effective_filter = None if is_admin else ts_id_filter

    try:
        if name == "lookup_call_by_uuid":
            r = requests.get(f"{TD_BASE}/calls/{params['uuid']}",
                             headers={"Authorization": TD_AUTH}, timeout=15)
            data = r.json()
            if "call" in data:
                call = data["call"]
                if effective_filter and not match_ts(call, effective_filter):
                    return {"error": "no_match",
                            "message": "This call is not associated with your Partner ID."}
                data["call"] = scrub_call(call)
            return data

        elif name == "lookup_call_by_phone":
            digits = re.sub(r"\D", "", params["phone"])[-10:]
            r = requests.get(f"{TD_BASE}/calls/1{digits}/by_caller_number",
                             headers={"Authorization": TD_AUTH}, timeout=15)
            data = r.json()
            if "calls" in data:
                calls = data["calls"]
                if effective_filter:
                    calls = [c for c in calls if match_ts(c, effective_filter)]
                    if not calls:
                        return {"error": "no_match",
                                "message": "No calls found for your Partner ID with this phone number."}
                data["calls"] = [scrub_call(c) for c in calls]
            return data

        elif name == "get_call_log":
            r = requests.get(f"{TD_BASE}/call_logs",
                             params={"call_id": params["call_id"]},
                             headers={"Authorization": TD_AUTH}, timeout=15)
            data = r.json()
            logs = data.get("call_logs", [])
            DIAGNOSTIC_KEYWORDS = (
                "converted", "duration", "not unique", "duplicate",
                "did not convert", "manually", "no buyer", "voicemail",
                "concurrent", "blacklist", "dnc", "geo did not match",
                "buyer conversion", "traffic source conv", "offerconversion",
                "caller on dnc", "is on the dnc", "rejecting the call",
                "call router", "callrouter", "on_dnc", "hangup", "hang up",
                "call ended", "no answer", "abandoned",
                "left the conference", "last member",
            )
            NOISE_PATTERNS = (
                "[Webhook::",
                "[OfferConversion!",
                "did not match since",
                "Skipping since",
                "Skipping URL",
                "retreaver_postback",
                "ringba_call_uuid",
                "traffic_source_lead_id",
            )
            filtered = []
            for entry in logs:
                msg = strip_html(entry.get("message", ""))
                level = entry.get("level", "")
                # Skip webhook/offerconversion noise lines
                if any(n in msg for n in NOISE_PATTERNS):
                    continue
                # Include danger always; others only if they match diagnostic keywords
                if level == "danger" or any(k in msg.lower() for k in DIAGNOSTIC_KEYWORDS):
                    msg = scrub_log([msg])[0]
                    filtered.append({"level": level, "message": msg})
            data["call_logs"] = filtered
            return data

        elif name == "get_kaliper_call":
            uuid = params["uuid"]
            r = requests.get(f"{KALIPER_BASE}/calls/source-id/{uuid}",
                             headers={"X-API-Key": KALIPER_KEY}, timeout=15)
            if r.status_code == 404:
                return {"found": False, "message": "Call not found in Kaliper."}
            if r.status_code != 200:
                return {"found": False, "message": f"Kaliper returned HTTP {r.status_code}."}
            raw = r.json()
            # The response may be the call directly or wrapped
            call = raw.get("call", raw)
            result = {"found": True}
            # QC pass/fail
            if "qcCritPass" in call:
                result["qcCritPass"] = call["qcCritPass"]
            # Detector answers (QC results, buyer ping responses, blacklist flags)
            answers = call.get("Answers", call.get("answers", []))
            if answers:
                result["answers"] = [
                    {k: v for k, v in a.items() if k in ("questionId", "question", "answer", "reasoning", "detector")}
                    for a in answers
                ]
            # Call summary from RawData
            raw_data = call.get("RawData", call.get("rawData", {})) or {}
            taxonomy = (raw_data.get("taxonomyData", {}) or {}).get("taxonomy", {}) or {}
            summary = taxonomy.get("call_summary") or taxonomy.get("callSummary")
            if summary:
                result["callSummary"] = summary
            # Publisher / campaign (useful for admin)
            for field in ("publisherName", "campaignName", "Publisher", "Campaign"):
                if field in call:
                    result[field] = call[field]
            # Inbound phone and duration
            for field in ("inboundPhone", "duration"):
                if field in call:
                    result[field] = call[field]
            return result

        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": str(e)}

# ── System prompts ─────────────────────────────────────────────────────────────
_SYSTEM_BASE = """You are TDCallInfo, a call status assistant for traffic sources (publishers) sending calls to Rapid Response Marketing.

You help publishers understand what happened with their calls — did they convert, and if not, why.

## What you CAN share
- Whether the call converted (yes/no), or if it was rejected before reaching an advertiser
- The offer the call was on
- Forwarded duration: use the forwarded_duration_seconds field (time the caller was actually connected to the advertiser). Format as minutes and seconds, e.g. "2 minutes 14 seconds". If 0 or missing, the call was never forwarded.
- Why it didn't convert or was rejected — always fetch the call log (get_call_log) for any call that did not convert or was rejected. Look for these reasons:
  - DNC (Do Not Call): "This caller is on the Do Not Call list and was rejected"
  - Blacklisted: "This caller is blacklisted and was rejected"
  - Duration threshold not met: "The call was forwarded for X minutes Y seconds but needed Z seconds to qualify"
  - Duplicate/repeat caller: "This caller already called recently and is flagged as a duplicate"
  - No advertiser available: "No advertiser was available to take the call"
  - Geo filter: "The caller's location did not match advertiser requirements"
  - Caller hung up / abandoned before connecting
  - Manually removed
  - Who ended the call: look for "left the conference ... last member false/true" log entries
    - "last member false" = that party hung up FIRST (others were still on the call)
    - "last member true" = that party was the LAST to leave (they ended the conference)
    - Example: "consumer left the conference last member false" + "Buyer left the conference last member true" means the caller hung up on the advertiser first

When hangup-order info is available, present it as a separate line after the main reason, prefixed with a 📵 emoji, e.g.:
  Reason: Duration threshold not met — forwarded 7 seconds, needed 90 seconds to qualify.
  📵 The caller hung up on the advertiser first.

## Kaliper quality data
After ANY call lookup — whether by UUID or phone number — always call get_kaliper_call for every call found, using the call's uuid field from the Trackdrive response.
If Kaliper data is found:
- If the buyer ping response was "blacklist" or "rejected", include it as a reason: "This caller was flagged as blacklisted by the advertiser's system."
- Surface any other detector answers that explain why the call didn't convert (e.g. DNC, duplicate, geo).
- Present Kaliper findings on a separate line prefixed with 🔍, e.g.:
    🔍 Advertiser ping response: Blacklisted — this number is blocked by the advertiser.

## Important: always check call logs
For ANY call that did not fully convert (status = rejected, or traffic_source_converted is not true), always call get_call_log with the call's numeric id to find the specific rejection reason before responding.

## What you must NEVER reveal — NO EXCEPTIONS
- Advertiser or buyer names, IDs, or any identifying information — always say "advertiser" only
- Revenue amounts, RPM, CPL, or any dollar figures we receive
- Payout amounts or rates paid to the publisher
- Margins, costs, or any financial data
- Any token values or internal pricing fields

If any financial or buyer data appears in the raw data, ignore it completely and do not mention it.

Be concise and professional. If a call converted, confirm it. If not, give the plain-English reason.
Do not speculate beyond what the data shows."""

_SYSTEM_ADMIN_SUFFIX = """

## ADMIN MODE — no partner filter applied
You are running in admin mode. You can look up any call regardless of which traffic source it belongs to.

For Kaliper data in admin mode, show ALL available information:
- QC pass/fail status (qcCritPass field)
- All detector answers with their reasoning (answers array)
- Publisher and campaign name
- Call summary if available (callSummary field)
- Full ping response details

Format admin Kaliper results clearly:
  🔍 Kaliper QC: PASS / FAIL
  🔍 Publisher: [name] | Campaign: [name]
  🔍 Detectors: [list each answer with reasoning]
  🔍 Call summary: [summary text]

Show buyer/advertiser names freely in admin mode — the partner filter and name scrubbing do not apply."""

def build_system(is_admin: bool) -> str:
    return _SYSTEM_BASE + (_SYSTEM_ADMIN_SUFFIX if is_admin else "")

# ── Lookup logic ───────────────────────────────────────────────────────────────
async def do_lookup(query: str, ts_id: str | None, is_admin: bool = False) -> str:
    client = anthropic.Anthropic(api_key=ANT_KEY)
    messages = [{"role": "user", "content": query}]
    system = build_system(is_admin)

    for _ in range(10):
        response = client.messages.create(
            model=MODEL,
            max_tokens=768,
            system=system,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(
                (b.text for b in response.content if hasattr(b, "text")),
                "No response generated."
            )

        elif response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    log.info(f"  Tool: {block.name}({block.input}) ts_filter={ts_id} admin={is_admin}")
                    result = run_tool(block.name, block.input, ts_id_filter=ts_id, is_admin=is_admin)
                    if isinstance(result, dict) and result.get("error") == "no_match":
                        return result["message"]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            return "Unexpected response from assistant."

    return "Query too complex to resolve."

# ── HTML ───────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Elite-Calls Call Lookup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #0a1628, #1a2d4a);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 24px 20px;
  }

  /* ── Header ── */
  .site-header {
    margin-bottom: 32px;
    text-align: center;
  }
  .site-header img {
    height: 60px;
    width: auto;
  }

  /* ── Intro block above card ── */
  .intro {
    text-align: center;
    max-width: 480px;
    margin-bottom: 24px;
  }
  .intro h2 {
    font-size: 22px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 8px;
    letter-spacing: -0.3px;
  }
  .intro p {
    font-size: 14px;
    color: rgba(255,255,255,0.55);
    line-height: 1.6;
  }
  .intro .steps {
    display: flex;
    justify-content: center;
    gap: 20px;
    margin-top: 14px;
    flex-wrap: wrap;
  }
  .intro .step {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13px;
    color: rgba(255,255,255,0.65);
  }
  .intro .step-num {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: linear-gradient(135deg, #4ade80, #86efac);
    color: #0a1628;
    font-size: 11px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  /* ── Card ── */
  .card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px;
    padding: 36px;
    width: 100%;
    max-width: 480px;
    backdrop-filter: blur(10px);
  }
  .field { margin-bottom: 18px; }
  label {
    display: block;
    font-size: 12px;
    color: rgba(255,255,255,0.5);
    margin-bottom: 7px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  input {
    width: 100%;
    background: rgba(10, 22, 40, 0.7);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px;
    padding: 12px 14px;
    color: #fff;
    font-size: 15px;
    font-family: inherit;
    outline: none;
    transition: border-color 0.15s;
  }
  input::placeholder { color: rgba(255,255,255,0.25); }
  input:focus { border-color: #4ade80; }
  button {
    width: 100%;
    background: linear-gradient(135deg, #22c55e, #4ade80);
    color: #0a1628;
    border: none;
    border-radius: 8px;
    padding: 13px;
    font-size: 15px;
    font-weight: 700;
    font-family: inherit;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.1s;
    margin-top: 6px;
    letter-spacing: 0.2px;
  }
  button:hover:not(:disabled) { opacity: 0.9; transform: translateY(-1px); }
  button:active:not(:disabled) { transform: translateY(0); }
  button:disabled { background: rgba(255,255,255,0.1); color: rgba(255,255,255,0.3); cursor: not-allowed; transform: none; }
  .spinner {
    display: none;
    text-align: center;
    margin-top: 20px;
    color: rgba(255,255,255,0.4);
    font-size: 13px;
  }
  .spinner.visible { display: block; }
  .result {
    display: none;
    margin-top: 20px;
    padding: 16px;
    background: rgba(10, 22, 40, 0.5);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
    font-size: 14px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    color: rgba(255,255,255,0.85);
  }
  .result.visible { display: block; }
  .result.error { border-color: rgba(248,81,73,0.5); color: #f85149; background: rgba(248,81,73,0.07); }
  .result.success { border-color: rgba(74,222,128,0.3); background: rgba(15,43,26,0.5); }
  .rate-note {
    text-align: center;
    margin-top: 16px;
    color: rgba(255,255,255,0.25);
    font-size: 12px;
  }
</style>
</head>
<body>

  <div class="site-header">
    <img src="https://elite-calls.com/Elite_Calls_2x1%20light.png" alt="Elite-Calls">
  </div>

  <div class="intro">
    <h2>Partner Call Lookup</h2>
    <p>Instantly check the status of any call you've sent us — whether it converted and exactly why if it didn't.</p>
    <div class="steps">
      <div class="step"><div class="step-num">1</div> Enter your Partner ID</div>
      <div class="step"><div class="step-num">2</div> Enter a UUID or phone number</div>
      <div class="step"><div class="step-num">3</div> Get instant results</div>
    </div>
  </div>

  <div class="card">
    <div class="field">
      <label for="ts_id">Partner ID</label>
      <input type="text" id="ts_id" placeholder="Your Traffic Source ID (e.g. 10200)" autocomplete="off" inputmode="numeric">
    </div>
    <div class="field">
      <label for="query">Call UUID or Caller Phone Number</label>
      <input type="text" id="query" placeholder="UUID or 10-digit phone number" autocomplete="off">
    </div>
    <button id="btn" onclick="lookup()">Look Up Call</button>
    <div class="spinner" id="spinner">⏳ Looking up call...</div>
    <div class="result" id="result"></div>
    <p class="rate-note">Limited to 5 lookups per minute per partner.</p>
  </div>

<script>
async function lookup() {
  const ts_id = document.getElementById('ts_id').value.trim();
  const query = document.getElementById('query').value.trim();
  const btn = document.getElementById('btn');
  const spinner = document.getElementById('spinner');
  const result = document.getElementById('result');

  result.className = 'result';
  result.textContent = '';

  if (!ts_id || !query) {
    result.className = 'result visible error';
    result.textContent = 'Please enter both your Partner ID and a call UUID or phone number.';
    return;
  }

  if (!/^\\d+$/.test(ts_id)) {
    result.className = 'result visible error';
    result.textContent = 'Partner ID must be numeric.';
    return;
  }

  btn.disabled = true;
  spinner.className = 'spinner visible';

  try {
    const resp = await fetch('/lookup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ts_id, query })
    });
    const data = await resp.json();

    if (resp.status === 429) {
      result.className = 'result visible error';
      result.textContent = '⚠️ Rate limit reached. You can perform up to 5 lookups per minute. Please wait a moment and try again.';
    } else if (data.error) {
      result.className = 'result visible error';
      result.textContent = '❌ ' + data.error;
    } else {
      result.className = 'result visible success';
      result.textContent = data.result;
    }
  } catch (e) {
    result.className = 'result visible error';
    result.textContent = '❌ Connection error. Please try again.';
  }

  btn.disabled = false;
  spinner.className = 'spinner';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Enter') lookup();
});
</script>
</body>
</html>"""

# ── Teams bot message handler ─────────────────────────────────────────────────
async def process_teams_message(turn_context: "TurnContext"):
    """Handle an incoming Teams message — strip @mention, run lookup, reply."""
    text   = (turn_context.activity.text or "").strip()
    sender = (turn_context.activity.from_property.name or "user") if turn_context.activity.from_property else "user"

    log.info("Teams message received | type=%s sender=%s text=%r",
             turn_context.activity.type, sender, text[:80])

    # Strip @mention HTML tags (e.g. <at>TDCallInfo</at>)
    text = re.sub(r"<at>[^<]*</at>", "", text).strip()

    # Also strip via entities list
    if turn_context.activity.entities:
        for entity in turn_context.activity.entities:
            if entity.type == "mention":
                mention_text = entity.additional_properties.get("text", "")
                if mention_text:
                    text = text.replace(mention_text, "").strip()

    # Normalize phone numbers (strip +, spaces, dashes, parens, etc.)
    digits_only = re.sub(r"\D", "", text)
    if len(digits_only) in (10, 11):
        text = digits_only[-10:]

    log.info("Teams query after stripping: %r", text)

    if not text:
        await turn_context.send_activity(
            "Hi! Send me a call UUID or phone number and I'll look it up for you."
        )
        return

    try:
        # Typing indicator
        await turn_context.send_activity(Activity(type="typing"))
        # Teams = internal/admin — no partner filter, full Kaliper data
        result = await do_lookup(text, ts_id=None, is_admin=True)
        log.info("Teams lookup complete, sending reply (%d chars)", len(result))
        await turn_context.send_activity(result)
    except Exception as e:
        log.error("Teams lookup error: %s", e, exc_info=True)
        try:
            await turn_context.send_activity(f"⚠️ Error processing your request: {e}")
        except Exception:
            pass


async def teams_messages_handler(req: web.Request) -> web.Response:
    """POST /api/messages — Azure Bot Service webhook."""
    if bot_adapter is None:
        return web.Response(status=503, text="Teams bot not configured")

    if req.content_type != "application/json":
        return web.Response(status=415)

    try:
        body        = await req.json()
        activity    = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")

        log.info("Teams webhook | activity type=%s", body.get("type", "unknown"))

        async def call_fun(turn_context):
            await process_teams_message(turn_context)

        await bot_adapter.process_activity(activity, auth_header, call_fun)
        return web.Response(status=200)
    except Exception as e:
        log.error("teams_messages_handler error: %s", e, exc_info=True)
        return web.Response(status=500, text=str(e))


# ── Routes ─────────────────────────────────────────────────────────────────────
async def index_handler(req: web.Request) -> web.Response:
    return web.Response(text=HTML, content_type="text/html")

async def lookup_handler(req: web.Request) -> web.Response:
    try:
        body = await req.json()
        ts_id = str(body.get("ts_id", "")).strip()
        query = str(body.get("query", "")).strip()

        if not ts_id or not query:
            return web.json_response({"error": "Missing Partner ID or query."}, status=400)

        if not re.match(r"^\d+$", ts_id):
            return web.json_response({"error": "Invalid Partner ID — must be numeric."}, status=400)

        if not check_rate_limit(ts_id):
            return web.json_response({"error": "Rate limit exceeded."}, status=429)

        is_admin = ts_id == ADMIN_TS_ID

        # Normalize phone numbers — strip formatting chars (+, spaces, dashes, parens)
        # before passing to Claude so it always sees a clean 10-digit number.
        # UUIDs contain letters so won't be touched.
        digits_only = re.sub(r"\D", "", query)
        if len(digits_only) in (10, 11):
            query = digits_only[-10:]   # always pass bare 10-digit number
            log.info(f"Normalized phone to: {query}")

        log.info(f"Lookup: TS#{ts_id} admin={is_admin} query={query[:60]}")
        result = await do_lookup(query, ts_id, is_admin=is_admin)
        return web.json_response({"result": result})

    except Exception as e:
        log.error(f"Lookup error: {e}", exc_info=True)
        return web.json_response({"error": "Internal server error."}, status=500)

async def health_handler(req: web.Request) -> web.Response:
    teams_status = "enabled" if bot_adapter else "disabled (missing env vars or botbuilder)"
    return web.Response(text=f"TDCallInfo is running | Teams bot: {teams_status}")

app = web.Application()
app.router.add_get("/", index_handler)
app.router.add_post("/lookup", lookup_handler)
app.router.add_post("/api/messages", teams_messages_handler)
app.router.add_get("/health", health_handler)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"TDCallInfo Web starting on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)
