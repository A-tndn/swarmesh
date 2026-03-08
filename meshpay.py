"""MeshPay — Card-to-Crypto Payment Gateway powered by CoinGate.

Accept card payments, settle in SOL, forward to SwarMesh treasury.
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import secrets
from typing import Optional

import aiohttp
from aiohttp import web

logging.basicConfig(level="INFO", format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("meshpay")

# --- Config ---
DB_PATH = os.path.expanduser("~/.meshpay/meshpay.db")
COINGATE_API_KEY = os.getenv("COINGATE_API_KEY", "")
COINGATE_API_URL = os.getenv("COINGATE_API_URL", "https://api.coingate.com/api/v2")
# Use sandbox for testing: https://api-sandbox.coingate.com/api/v2
COINGATE_SANDBOX = os.getenv("COINGATE_SANDBOX", "false").lower() == "true"

BASE_URL = os.getenv("MESHPAY_BASE_URL", "https://swarmesh.xyz")  # public URL for callbacks
SETTLE_CURRENCY = "SOL"  # settlement currency
ADMIN_TOKEN = os.getenv("MESHPAY_ADMIN_TOKEN", "meshpay_admin_2026")

# SwarMesh treasury for auto-forward
SWARMESH_TREASURY = "52Pzs3ahgiJvuHEYS3QwB82EXM8122QuvoZuL5gGNgfQ"

# Telegram notifications
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6522780299")


def _api_url():
    if COINGATE_SANDBOX:
        return "https://api-sandbox.coingate.com/api/v2"
    return COINGATE_API_URL


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            coingate_id INTEGER DEFAULT 0,
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'new',
            pay_currency TEXT DEFAULT '',
            pay_amount TEXT DEFAULT '',
            receive_amount TEXT DEFAULT '',
            receive_currency TEXT DEFAULT 'SOL',
            payment_url TEXT DEFAULT '',
            customer_email TEXT DEFAULT '',
            customer_name TEXT DEFAULT '',
            callback_token TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT DEFAULT '',
            forwarded_to_treasury INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id TEXT UNIQUE NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            email TEXT DEFAULT '',
            webhook_url TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS webhook_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            event TEXT,
            payload TEXT,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_admin(request) -> bool:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() == ADMIN_TOKEN
    return False


def auth_merchant(request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    key = auth[7:].strip()
    conn = get_db()
    row = conn.execute("SELECT * FROM merchants WHERE api_key=? AND active=1", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None


# --- CoinGate API ---

async def coingate_create_order(amount: float, currency: str, title: str, description: str,
                                 order_id: str, callback_token: str,
                                 success_url: str = "", cancel_url: str = "") -> dict:
    """Create a CoinGate payment order."""
    if not COINGATE_API_KEY:
        return {"error": "CoinGate API key not configured"}

    payload = {
        "price_amount": amount,
        "price_currency": currency,
        "receive_currency": SETTLE_CURRENCY,
        "title": title[:150],
        "description": description[:500] if description else title[:500],
        "order_id": order_id,
        "callback_url": f"{BASE_URL}/pay/webhook",
        "success_url": success_url or f"{BASE_URL}/pay/success?order={order_id}",
        "cancel_url": cancel_url or f"{BASE_URL}/pay/cancel?order={order_id}",
        "token": callback_token,
    }

    headers = {
        "Authorization": f"Token {COINGATE_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{_api_url()}/orders", json=payload, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if resp.status == 200:
                    logger.info("CoinGate order created: %s -> %s", order_id, data.get("id"))
                    return data
                else:
                    logger.error("CoinGate error %d: %s", resp.status, data)
                    return {"error": data.get("message", "Unknown error"), "status": resp.status}
    except Exception as e:
        logger.error("CoinGate API failed: %s", e)
        return {"error": str(e)}


async def coingate_get_order(coingate_id: int) -> dict:
    """Get order status from CoinGate."""
    headers = {"Authorization": f"Token {COINGATE_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_api_url()}/orders/{coingate_id}", headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.json()
    except Exception as e:
        return {"error": str(e)}


# --- Telegram ---

async def notify_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        logger.error("Telegram notify failed: %s", e)


# --- HTTP Handlers ---

async def handle_health(request):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    paid = conn.execute("SELECT COUNT(*) FROM payments WHERE status='paid'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM payments WHERE status IN ('new','pending')").fetchone()[0]
    conn.close()
    return web.json_response({
        "service": "meshpay",
        "status": "ok",
        "coingate_configured": bool(COINGATE_API_KEY),
        "settle_currency": SETTLE_CURRENCY,
        "payments_total": total,
        "payments_paid": paid,
        "payments_pending": pending,
    })


async def handle_create_payment(request):
    """POST /pay/create — Create a new payment.
    Body: {amount, currency, title, description?, customer_email?, customer_name?, metadata?}
    Auth: Bearer (merchant API key or admin token)
    """
    merchant = auth_merchant(request)
    if not merchant and not is_admin(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    amount = data.get("amount")
    if not amount or not isinstance(amount, (int, float)) or amount <= 0:
        return web.json_response({"error": "valid amount required"}, status=400)

    currency = (data.get("currency") or "USD").upper()
    title = (data.get("title") or "Payment")[:150]
    description = (data.get("description") or "")[:500]
    customer_email = (data.get("customer_email") or "")[:256]
    customer_name = (data.get("customer_name") or "")[:128]
    metadata = json.dumps(data.get("metadata", {}))
    success_url = data.get("success_url", "")
    cancel_url = data.get("cancel_url", "")

    order_id = f"mp_{secrets.token_hex(8)}"
    callback_token = secrets.token_hex(16)

    # Create in DB first
    conn = get_db()
    conn.execute(
        """INSERT INTO payments (order_id, title, description, amount, currency,
           customer_email, customer_name, callback_token, metadata, receive_currency)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (order_id, title, description, amount, currency, customer_email,
         customer_name, callback_token, metadata, SETTLE_CURRENCY)
    )
    conn.commit()
    conn.close()

    # Create on CoinGate
    cg_result = await coingate_create_order(
        amount=amount, currency=currency, title=title, description=description,
        order_id=order_id, callback_token=callback_token,
        success_url=success_url, cancel_url=cancel_url,
    )

    if "error" in cg_result:
        # Update status
        conn = get_db()
        conn.execute("UPDATE payments SET status='error' WHERE order_id=?", (order_id,))
        conn.commit()
        conn.close()
        return web.json_response({
            "error": "Payment creation failed",
            "detail": cg_result.get("error"),
            "order_id": order_id,
        }, status=502)

    # Update with CoinGate details
    payment_url = cg_result.get("payment_url", "")
    coingate_id = cg_result.get("id", 0)
    conn = get_db()
    conn.execute(
        """UPDATE payments SET coingate_id=?, payment_url=?, status='pending'
           WHERE order_id=?""",
        (coingate_id, payment_url, order_id)
    )
    conn.commit()
    conn.close()

    logger.info("Payment created: %s (%.2f %s) -> %s", order_id, amount, currency, payment_url)

    return web.json_response({
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "settle_currency": SETTLE_CURRENCY,
        "payment_url": payment_url,
        "status": "pending",
        "message": "Redirect customer to payment_url to complete payment.",
    })


async def handle_webhook(request):
    """POST /pay/webhook — CoinGate callback on status change."""
    try:
        data = await request.post()  # CoinGate sends form-encoded
        if not data:
            data = await request.json()
    except Exception:
        try:
            data = dict(request.query)
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

    order_id = data.get("order_id", "")
    status = data.get("status", "")
    token = data.get("token", "")

    if not order_id or not status:
        logger.warning("Webhook missing order_id or status")
        return web.json_response({"error": "missing fields"}, status=400)

    # Log webhook
    conn = get_db()
    conn.execute(
        "INSERT INTO webhook_logs (order_id, event, payload) VALUES (?,?,?)",
        (order_id, status, json.dumps(dict(data)))
    )
    conn.commit()

    # Verify token
    payment = conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
    if not payment:
        conn.close()
        logger.warning("Webhook for unknown order: %s", order_id)
        return web.json_response({"error": "unknown order"}, status=404)

    if token and payment["callback_token"] and token != payment["callback_token"]:
        conn.close()
        logger.warning("Webhook token mismatch for %s", order_id)
        return web.json_response({"error": "invalid token"}, status=403)

    # Update payment
    updates = {"status": status}
    if status == "paid":
        updates["paid_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        updates["pay_currency"] = data.get("pay_currency", "")
        updates["pay_amount"] = data.get("pay_amount", "")
        updates["receive_amount"] = data.get("receive_amount", "")
        updates["receive_currency"] = data.get("receive_currency", SETTLE_CURRENCY)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [order_id]
    conn.execute(f"UPDATE payments SET {set_clause} WHERE order_id=?", vals)
    conn.commit()
    conn.close()

    logger.info("Payment %s -> %s", order_id, status)

    # Notify on payment
    if status == "paid":
        receive_amt = data.get("receive_amount", "?")
        receive_cur = data.get("receive_currency", SETTLE_CURRENCY)
        pay_amt = data.get("pay_amount", "?")
        pay_cur = data.get("pay_currency", "?")
        text = (
            f"<b>Payment Received</b>\n"
            f"Order: <code>{order_id}</code>\n"
            f"Paid: {pay_amt} {pay_cur}\n"
            f"Settled: {receive_amt} {receive_cur}\n"
            f"Customer: {payment['customer_email'] or 'anonymous'}\n"
            f"Title: {payment['title']}"
        )
        await notify_telegram(text)

    return web.json_response({"status": "ok"})


async def handle_payment_status(request):
    """GET /pay/status/{order_id} — Check payment status."""
    order_id = request.match_info.get("order_id", "")
    if not order_id:
        return web.json_response({"error": "order_id required"}, status=400)

    conn = get_db()
    payment = conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
    conn.close()

    if not payment:
        return web.json_response({"error": "order not found"}, status=404)

    resp = {
        "order_id": payment["order_id"],
        "title": payment["title"],
        "amount": payment["amount"],
        "currency": payment["currency"],
        "status": payment["status"],
        "payment_url": payment["payment_url"],
        "created_at": payment["created_at"],
    }
    if payment["status"] == "paid":
        resp["paid_at"] = payment["paid_at"]
        resp["receive_amount"] = payment["receive_amount"]
        resp["receive_currency"] = payment["receive_currency"]
        resp["pay_currency"] = payment["pay_currency"]
        resp["pay_amount"] = payment["pay_amount"]

    return web.json_response(resp)


async def handle_checkout_page(request):
    """GET /pay/checkout/{order_id} — Serve checkout UI."""
    order_id = request.match_info.get("order_id", "")
    conn = get_db()
    payment = conn.execute("SELECT * FROM payments WHERE order_id=?", (order_id,)).fetchone()
    conn.close()

    if not payment:
        return web.Response(text="Order not found", status=404, content_type="text/html")

    if payment["payment_url"]:
        # Redirect to CoinGate checkout
        raise web.HTTPFound(payment["payment_url"])

    return web.Response(text="Payment not ready yet. Try again.", status=400, content_type="text/html")


async def handle_success_page(request):
    """GET /pay/success — Post-payment success page."""
    order_id = request.query.get("order", "")
    html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Payment Success — MeshPay</title>
<style>
body {{ background:#0a0a0a; color:#e0e0e0; font-family:'Courier New',monospace;
  display:flex; align-items:center; justify-content:center; min-height:100vh; }}
.box {{ text-align:center; padding:48px; border:1px solid #1a1a1a; max-width:480px; }}
.check {{ font-size:3em; color:#00ff88; margin-bottom:16px; }}
h2 {{ color:#fff; font-weight:400; letter-spacing:2px; margin-bottom:12px; }}
p {{ color:#666; font-size:0.85em; line-height:1.6; }}
code {{ color:#00ff88; background:#111; padding:2px 6px; }}
</style></head><body>
<div class="box">
  <div class="check">&#10003;</div>
  <h2>PAYMENT COMPLETE</h2>
  <p>Your payment has been processed successfully.</p>
  <p>Order: <code>{order_id}</code></p>
  <p style="color:#333;font-size:0.7em;margin-top:24px;">Powered by MeshPay</p>
</div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_cancel_page(request):
    """GET /pay/cancel — Payment cancelled page."""
    order_id = request.query.get("order", "")
    html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Payment Cancelled — MeshPay</title>
<style>
body {{ background:#0a0a0a; color:#e0e0e0; font-family:'Courier New',monospace;
  display:flex; align-items:center; justify-content:center; min-height:100vh; }}
.box {{ text-align:center; padding:48px; border:1px solid #1a1a1a; max-width:480px; }}
.x {{ font-size:3em; color:#ff4444; margin-bottom:16px; }}
h2 {{ color:#fff; font-weight:400; letter-spacing:2px; margin-bottom:12px; }}
p {{ color:#666; font-size:0.85em; line-height:1.6; }}
</style></head><body>
<div class="box">
  <div class="x">&#10007;</div>
  <h2>PAYMENT CANCELLED</h2>
  <p>The payment was not completed.</p>
  <p>Order: <code>{order_id}</code></p>
  <p style="color:#333;font-size:0.7em;margin-top:24px;">Powered by MeshPay</p>
</div></body></html>"""
    return web.Response(text=html, content_type="text/html")


# --- Admin Endpoints ---

async def handle_admin_payments(request):
    """GET /pay/admin/payments — List all payments (admin only)."""
    if not is_admin(request):
        return web.json_response({"error": "admin required"}, status=403)

    status_filter = request.query.get("status", "")
    limit = min(int(request.query.get("limit", "50")), 200)

    conn = get_db()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM payments WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status_filter, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()

    payments = []
    for r in rows:
        payments.append({
            "order_id": r["order_id"], "title": r["title"],
            "amount": r["amount"], "currency": r["currency"],
            "status": r["status"], "payment_url": r["payment_url"],
            "receive_amount": r["receive_amount"], "receive_currency": r["receive_currency"],
            "pay_currency": r["pay_currency"], "pay_amount": r["pay_amount"],
            "customer_email": r["customer_email"], "customer_name": r["customer_name"],
            "created_at": r["created_at"], "paid_at": r["paid_at"],
        })

    # Stats
    conn = get_db()
    total_paid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='paid'"
    ).fetchone()
    conn.close()

    return web.json_response({
        "payments": payments,
        "total": len(payments),
        "stats": {
            "total_paid_count": total_paid[0],
            "total_paid_volume": total_paid[1],
        }
    })


async def handle_admin_create_merchant(request):
    """POST /pay/admin/merchants — Create a merchant API key."""
    if not is_admin(request):
        return web.json_response({"error": "admin required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    name = (data.get("name") or "").strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    email = (data.get("email") or "").strip()
    webhook_url = (data.get("webhook_url") or "").strip()

    merchant_id = f"merch_{secrets.token_hex(6)}"
    api_key = f"mpk_{secrets.token_hex(24)}"

    conn = get_db()
    conn.execute(
        "INSERT INTO merchants (merchant_id, api_key, name, email, webhook_url) VALUES (?,?,?,?,?)",
        (merchant_id, api_key, name, email, webhook_url)
    )
    conn.commit()
    conn.close()

    logger.info("Merchant created: %s (%s)", name, merchant_id)

    return web.json_response({
        "merchant_id": merchant_id,
        "api_key": api_key,
        "name": name,
        "message": "Use api_key in Authorization: Bearer header to create payments.",
    })


async def handle_admin_webhooks(request):
    """GET /pay/admin/webhooks — View webhook logs."""
    if not is_admin(request):
        return web.json_response({"error": "admin required"}, status=403)

    limit = min(int(request.query.get("limit", "50")), 200)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM webhook_logs ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()

    logs = [{"id": r["id"], "order_id": r["order_id"], "event": r["event"],
             "payload": r["payload"], "received_at": r["received_at"]} for r in rows]

    return web.json_response({"logs": logs, "total": len(logs)})


# --- Quick Pay Link ---

async def handle_quick_pay(request):
    """GET /pay/quick — Generate a payment link without API.
    Query params: amount, currency, title, email
    """
    amount = request.query.get("amount", "")
    currency = request.query.get("currency", "USD")
    title = request.query.get("title", "Payment")
    email = request.query.get("email", "")

    if not amount:
        # Show the quick pay form
        return web.Response(text=QUICK_PAY_HTML, content_type="text/html")

    try:
        amount = float(amount)
    except ValueError:
        return web.json_response({"error": "invalid amount"}, status=400)

    order_id = f"mp_{secrets.token_hex(8)}"
    callback_token = secrets.token_hex(16)

    conn = get_db()
    conn.execute(
        """INSERT INTO payments (order_id, title, amount, currency, customer_email,
           callback_token, receive_currency) VALUES (?,?,?,?,?,?,?)""",
        (order_id, title, amount, currency.upper(), email, callback_token, SETTLE_CURRENCY)
    )
    conn.commit()
    conn.close()

    cg_result = await coingate_create_order(
        amount=amount, currency=currency.upper(), title=title,
        description=f"Quick payment: {title}",
        order_id=order_id, callback_token=callback_token,
    )

    if "error" in cg_result:
        return web.json_response({"error": cg_result["error"]}, status=502)

    payment_url = cg_result.get("payment_url", "")
    conn = get_db()
    conn.execute("UPDATE payments SET coingate_id=?, payment_url=?, status='pending' WHERE order_id=?",
                 (cg_result.get("id", 0), payment_url, order_id))
    conn.commit()
    conn.close()

    raise web.HTTPFound(payment_url)


# --- Dashboard Page ---

DASHBOARD_HTML = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshPay Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;min-height:100vh}
.container{max-width:800px;margin:0 auto;padding:40px 24px}
h1{font-size:1.8em;font-weight:400;letter-spacing:3px;color:#fff;margin-bottom:4px}
h1 span{color:#00ff88}
.sub{color:#555;font-size:0.75em;letter-spacing:2px;margin-bottom:32px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#1a1a1a;margin:24px 0;border:1px solid #1a1a1a}
.stat{background:#0a0a0a;padding:20px;text-align:center}
.stat .n{font-size:1.4em;color:#00ff88}
.stat .l{font-size:0.6em;color:#555;letter-spacing:2px;margin-top:4px}
table{width:100%;border-collapse:collapse;margin-top:24px;font-size:0.8em}
th{text-align:left;padding:10px 8px;color:#555;border-bottom:1px solid #1a1a1a;font-weight:400;letter-spacing:1px}
td{padding:10px 8px;border-bottom:1px solid #111;color:#888}
tr:hover td{color:#ccc}
.s-paid{color:#00ff88} .s-pending{color:#ff8800} .s-new{color:#666} .s-expired{color:#ff4444}
.amt{color:#fff}
.login{max-width:360px;margin:120px auto;text-align:center}
.login input{background:#111;border:1px solid #222;color:#e0e0e0;padding:12px;width:100%;margin:8px 0;
  font-family:'Courier New',monospace;outline:none}
.login input:focus{border-color:#00ff88}
.login button{background:transparent;border:1px solid #00ff88;color:#00ff88;padding:12px 32px;cursor:pointer;
  font-family:'Courier New',monospace;letter-spacing:2px;margin-top:8px;width:100%}
.login button:hover{background:#00ff88;color:#0a0a0a}
.empty{color:#333;text-align:center;padding:40px}
#createForm{display:none;border:1px solid #1a1a1a;padding:20px;margin:24px 0}
#createForm input,#createForm select{background:#111;border:1px solid #222;color:#e0e0e0;padding:10px;
  width:100%;margin:4px 0 12px;font-family:'Courier New',monospace;outline:none;font-size:0.85em}
#createForm input:focus{border-color:#00ff88}
.btn{background:transparent;border:1px solid #00ff88;color:#00ff88;padding:10px 24px;cursor:pointer;
  font-family:'Courier New',monospace;letter-spacing:1px;font-size:0.8em}
.btn:hover{background:#00ff88;color:#0a0a0a}
.btn-sm{padding:6px 12px;font-size:0.7em}
</style></head><body>
<div class="container">
  <div id="loginView" class="login">
    <h1>MESH<span>PAY</span></h1>
    <p style="color:#555;font-size:0.75em;margin:16px 0;">Admin Dashboard</p>
    <input type="password" id="tokenInput" placeholder="Admin token">
    <button onclick="login()">ENTER</button>
  </div>
  <div id="dashView" style="display:none">
    <h1>MESH<span>PAY</span></h1>
    <p class="sub">CARD-TO-CRYPTO PAYMENT GATEWAY</p>
    <div class="stats">
      <div class="stat"><div class="n" id="st-total">—</div><div class="l">TOTAL</div></div>
      <div class="stat"><div class="n" id="st-paid">—</div><div class="l">PAID</div></div>
      <div class="stat"><div class="n" id="st-volume">—</div><div class="l">VOLUME</div></div>
    </div>
    <button class="btn" onclick="toggleCreate()" style="margin-bottom:16px">+ NEW PAYMENT</button>
    <div id="createForm">
      <input id="cf-amount" type="number" step="0.01" placeholder="Amount (e.g. 50.00)">
      <select id="cf-currency"><option>USD</option><option>EUR</option><option>GBP</option><option>INR</option></select>
      <input id="cf-title" placeholder="Title (e.g. Premium Plan)">
      <input id="cf-email" placeholder="Customer email (optional)">
      <button class="btn" onclick="createPayment()">CREATE PAYMENT LINK</button>
      <div id="cf-result" style="margin-top:12px;font-size:0.8em;color:#00ff88;display:none"></div>
    </div>
    <table>
      <thead><tr><th>ORDER</th><th>TITLE</th><th>AMOUNT</th><th>STATUS</th><th>DATE</th></tr></thead>
      <tbody id="payTable"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody>
    </table>
  </div>
</div>
<script>
let TOKEN='';
function login(){TOKEN=document.getElementById('tokenInput').value;loadDash()}
async function loadDash(){
  try{
    const r=await fetch('/pay/admin/payments',{headers:{'Authorization':'Bearer '+TOKEN}});
    if(r.status===403){alert('Invalid token');return}
    const d=await r.json();
    document.getElementById('loginView').style.display='none';
    document.getElementById('dashView').style.display='block';
    document.getElementById('st-total').textContent=d.stats.total_paid_count;
    document.getElementById('st-paid').textContent=d.payments.filter(p=>p.status==='paid').length;
    document.getElementById('st-volume').textContent='$'+d.stats.total_paid_volume.toFixed(2);
    const tb=document.getElementById('payTable');
    if(!d.payments.length){tb.innerHTML='<tr><td colspan="5" class="empty">No payments yet.</td></tr>';return}
    tb.innerHTML=d.payments.map(p=>'<tr>'+
      '<td><code>'+p.order_id.substring(0,12)+'</code></td>'+
      '<td>'+esc(p.title)+'</td>'+
      '<td class="amt">'+p.amount+' '+p.currency+'</td>'+
      '<td class="s-'+p.status+'">'+p.status.toUpperCase()+'</td>'+
      '<td style="color:#444">'+p.created_at.split(' ')[0]+'</td>'+
    '</tr>').join('');
  }catch(e){alert('Failed to load')}
}
function toggleCreate(){const f=document.getElementById('createForm');f.style.display=f.style.display==='none'?'block':'none'}
async function createPayment(){
  const amt=document.getElementById('cf-amount').value;
  const cur=document.getElementById('cf-currency').value;
  const title=document.getElementById('cf-title').value||'Payment';
  const email=document.getElementById('cf-email').value;
  if(!amt){alert('Amount required');return}
  try{
    const r=await fetch('/pay/create',{method:'POST',headers:{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'},
      body:JSON.stringify({amount:parseFloat(amt),currency:cur,title:title,customer_email:email})});
    const d=await r.json();
    const res=document.getElementById('cf-result');
    if(d.payment_url){
      res.innerHTML='Payment link: <a href="'+d.payment_url+'" target="_blank" style="color:#00ff88">'+d.payment_url+'</a>';
      res.style.display='block';
      loadDash();
    }else{res.textContent='Error: '+(d.error||'unknown');res.style.display='block';res.style.color='#ff4444'}
  }catch(e){alert('Failed')}
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
</script></body></html>"""

QUICK_PAY_HTML = """<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quick Pay — MeshPay</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#e0e0e0;font-family:'Courier New',monospace;
  display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{max-width:400px;width:100%;padding:40px;border:1px solid #1a1a1a}
h2{font-weight:400;letter-spacing:2px;color:#fff;margin-bottom:4px}
h2 span{color:#00ff88}
.sub{color:#555;font-size:0.7em;margin-bottom:24px}
input,select{background:#111;border:1px solid #222;color:#e0e0e0;padding:12px;width:100%;margin:6px 0;
  font-family:'Courier New',monospace;outline:none}
input:focus{border-color:#00ff88}
button{background:transparent;border:1px solid #00ff88;color:#00ff88;padding:12px;width:100%;
  font-family:'Courier New',monospace;letter-spacing:2px;cursor:pointer;margin-top:12px}
button:hover{background:#00ff88;color:#0a0a0a}
</style></head><body>
<div class="box">
  <h2>MESH<span>PAY</span></h2>
  <p class="sub">QUICK PAYMENT</p>
  <form action="/pay/quick" method="GET">
    <input name="amount" type="number" step="0.01" placeholder="Amount" required>
    <select name="currency"><option>USD</option><option>EUR</option><option>INR</option><option>GBP</option></select>
    <input name="title" placeholder="What is this payment for?">
    <input name="email" type="email" placeholder="Your email (optional)">
    <button type="submit">PAY NOW</button>
  </form>
</div></body></html>"""


async def handle_dashboard(request):
    """GET /pay/dashboard — Admin dashboard."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


# --- App Setup ---

init_db()
app = web.Application()

# Public routes
app.router.add_get('/pay/health', handle_health)
app.router.add_get('/pay/quick', handle_quick_pay)
app.router.add_get('/pay/success', handle_success_page)
app.router.add_get('/pay/cancel', handle_cancel_page)
app.router.add_get('/pay/checkout/{order_id}', handle_checkout_page)
app.router.add_get('/pay/status/{order_id}', handle_payment_status)

# Webhook (CoinGate calls this)
app.router.add_post('/pay/webhook', handle_webhook)

# Authenticated (merchant or admin)
app.router.add_post('/pay/create', handle_create_payment)

# Admin
app.router.add_get('/pay/dashboard', handle_dashboard)
app.router.add_get('/pay/admin/payments', handle_admin_payments)
app.router.add_post('/pay/admin/merchants', handle_admin_create_merchant)
app.router.add_get('/pay/admin/webhooks', handle_admin_webhooks)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=7772)
