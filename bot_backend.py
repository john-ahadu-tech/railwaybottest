"""
AHADU MARKET - bot_backend.py
Full production-ready backend (Flask + SQLAlchemy + python-telegram-bot v13.7)

Features:
- User registration (Telegram Login Widget + Telegram messages)
- Admin approval workflow (approve salespeople -> send promo code)
- Products management (admin can add/delete products)
- Orders (salespeople log sales -> admin approves/rejects)
- Payouts (admin view, mark as paid)
- Leaderboard (monthly)
- Notifications to admin/sales group via Telegram bot
- Works locally with SQLite and in production with PostgreSQL (via DATABASE_URL)
- Polling mode (default) or Webhook mode (if WEBHOOK_URL is set)

IMPORTANT:
- Replace placeholder TELEGRAM_BOT_USERNAME in index.html's Telegram widget with your bot username.
- For production, set these env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_ADMIN_CHAT_ID, DATABASE_URL, SECRET_KEY, WEBHOOK_URL (optional).
"""

import os
import hmac
import hashlib
import uuid
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

from dotenv import load_dotenv
load_dotenv()

# Telegram imports (v13.7)
from telegram import Bot, ParseMode
from telegram.error import TelegramError
from telegram.ext import Updater, Dispatcher, CommandHandler, MessageHandler, Filters

# -------------------------
# Configuration
# -------------------------
# WARNING: For local testing you provided a token earlier.
# For production move these into environment variables.
DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "6180505622:AAEW7vZO3IIXO91EPOaQFYt3gBO9GUcPoas")
DEFAULT_ADMIN_CHAT_ID = os.getenv("TELEGRAM_BOT_ADMIN_CHAT_ID", "1241311689")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://<your-domain>/telegram/webhook
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-in-prod")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ahadu_local.db")
COMMISSION_PERCENT = float(os.getenv("COMMISSION_PERCENT", "5.0"))

# Flask app + DB
app = Flask(__name__, static_folder=".", static_url_path="/")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = SECRET_KEY

db = SQLAlchemy(app)

# Telegram Bot client
bot_token = DEFAULT_BOT_TOKEN
ADMIN_CHAT_ID = DEFAULT_ADMIN_CHAT_ID

bot = Bot(token=bot_token)

# Dispatcher/Updater: we'll use Updater for polling when no webhook is set
# Create dispatcher without Updater first (Dispatcher is managed by Updater below)
# We'll create the Updater later in __main__ block for polling.
dispatcher = Dispatcher = None  # placeholder variable name for clarity

# -------------------------
# Database models
# -------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.String, unique=True, nullable=True)
    name = db.Column(db.String, nullable=False)
    phone = db.Column(db.String, nullable=True)
    role = db.Column(db.String, default="sales")  # 'admin' or 'sales'
    status = db.Column(db.String, default="PENDING")  # 'PENDING', 'APPROVED'
    promo_code = db.Column(db.String, nullable=True)
    session_token = db.Column(db.String, unique=True, nullable=True)
    theme = db.Column(db.String, default="bright")  # 'bright' or 'dark'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "telegram_id": self.telegram_id,
            "name": self.name,
            "phone": self.phone,
            "role": self.role,
            "status": self.status,
            "promo_code": self.promo_code,
            "theme": self.theme,
            "created_at": self.created_at.isoformat(),
        }

class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    specs = db.Column(db.String, nullable=True)
    price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "specs": self.specs,
            "price": self.price,
            "quantity": self.quantity,
            "created_at": self.created_at.isoformat(),
        }

class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    salesperson_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    other_name = db.Column(db.String, nullable=True)
    other_price = db.Column(db.Float, nullable=True)
    customer_name = db.Column(db.String, nullable=False)
    customer_phone = db.Column(db.String, nullable=False)
    status = db.Column(db.String, default="PENDING")  # PENDING, COMPLETED, CANCELLED
    commission_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "salesperson_id": self.salesperson_id,
            "product_id": self.product_id,
            "other_name": self.other_name,
            "other_price": self.other_price,
            "customer_name": self.customer_name,
            "customer_phone": self.customer_phone,
            "status": self.status,
            "commission_paid": self.commission_paid,
            "created_at": self.created_at.isoformat(),
        }

# Create DB tables if missing
with app.app_context():
    db.create_all()

# -------------------------
# Utilities
# -------------------------
def send_admin_message(text):
    """Send a message to the admin group/chat if ADMIN_CHAT_ID is set."""
    if not ADMIN_CHAT_ID:
        app.logger.warning("ADMIN_CHAT_ID not set; skipping admin notification.")
        return
    try:
        bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        app.logger.error("Failed to send admin message: %s", str(e))

def send_user_message(telegram_id, text):
    """Send a message to a user by telegram_id (string or int)."""
    if not telegram_id:
        app.logger.warning("No telegram_id provided; cannot send user message.")
        return
    try:
        bot.send_message(chat_id=str(telegram_id), text=text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        app.logger.error("Failed to send message to user %s: %s", telegram_id, str(e))

def generate_promo_code(name):
    base = (name or "user").split()[0].upper()
    code = f"{base[:4]}-{uuid.uuid4().hex[:6].upper()}"
    return code

def verify_telegram_signature(data: dict):
    """
    Verify Telegram Login Widget data as per Telegram docs:
    https://core.telegram.org/widgets/login
    Returns True if valid.
    """
    # Build data_check_string
    check_keys = []
    for k, v in sorted(data.items()):
        if k == "hash":
            continue
        check_keys.append(f"{k}={v}")
    data_check_string = "\n".join(check_keys)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    hmac_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac_hash == data.get("hash")

def require_auth(fn):
    """Decorator to authenticate using session_token header."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization")
        if not token:
            return jsonify({"error": "missing authorization token"}), 401
        user = User.query.filter_by(session_token=token).first()
        if not user:
            return jsonify({"error": "invalid session token"}), 401
        g.user = user
        return fn(*args, **kwargs)
    return wrapper

def admin_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if getattr(g, "user", None) is None:
            return jsonify({"error": "unauthorized"}), 401
        if g.user.role != "admin":
            return jsonify({"error": "admin required"}), 403
        return fn(*args, **kwargs)
    return wrapper

# -------------------------
# Telegram command handlers (for incoming messages when polling/webhook)
# -------------------------
def cmd_start(update, context):
    """Handle /start: register the Telegram user if not present."""
    telegram_id = str(update.effective_user.id)
    name = update.effective_user.full_name or update.effective_user.username or "Unknown"
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, name=name, status="PENDING", role="sales")
        db.session.add(user)
        db.session.commit()
        # notify admin
        send_admin_message(f"🆕 New salesperson registered via /start:\nName: {name}\nTelegram ID: {telegram_id}\nPlease approve them in the admin panel.")
        update.message.reply_text("Thanks — you've been registered. Your account is pending admin approval.")
    else:
        if user.status == "PENDING":
            update.message.reply_text("Your account is still pending approval. Please wait.")
        else:
            update.message.reply_text("Welcome back! Your account is already approved. Open the Mini App to continue.")

def cmd_help(update, context):
    update.message.reply_text("AHADU MARKET bot: use the web app to manage sales. Admins can approve users and orders.")

# We'll wire handlers to the Dispatcher in __main__ section (when Updater exists)


# -------------------------
# Flask routes (API)
# -------------------------
@app.route("/")
def index():
    # Serve single-file frontend
    return send_from_directory(".", "index.html")

@app.route("/api/auth/telegram", methods=["POST"])
def auth_telegram():
    """
    Accepts the Telegram Login Widget payload from client.
    Body: JSON with the fields returned by Telegram Widget (including 'hash').
    Verifies signature and creates/updates a user. Returns session_token and user data.
    """
    payload = request.json or {}
    if not payload:
        return jsonify({"error": "missing payload"}), 400

    # Verify signature
    if not verify_telegram_signature(payload):
        return jsonify({"error": "invalid telegram signature"}), 400

    telegram_id = str(payload.get("id"))
    name = (payload.get("first_name") or "") + ((" " + (payload.get("last_name") or "")) if payload.get("last_name") else "")
    username = payload.get("username")
    phone = payload.get("phone") or request.json.get("phone")  # allow phone from client

    # find or create user
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, name=name or (username or "Unknown"), phone=phone or "", role="sales", status="PENDING")
        db.session.add(user)
        db.session.commit()
        # notify admin of a new registration
        text = f"🆕 <b>New salesperson registration</b>\nName: {user.name}\nTelegram ID: {user.telegram_id}\nStatus: {user.status}\nApprove them in the admin panel."
        send_admin_message(text)
    else:
        # update some fields
        if phone:
            user.phone = phone
        user.name = name or user.name
        db.session.commit()

    # create session token
    token = uuid.uuid4().hex
    user.session_token = token
    db.session.commit()

    return jsonify({"session_token": token, "user": user.to_dict()})


@app.route("/api/me", methods=["GET"])
@require_auth
def api_me():
    return jsonify({"user": g.user.to_dict()})


# Admin: list pending approvals
@app.route("/api/admin/pending", methods=["GET"])
@require_auth
@admin_only
def admin_pending():
    pending = User.query.filter_by(status="PENDING").all()
    return jsonify({"pending": [p.to_dict() for p in pending]})


# Admin: approve salesperson
@app.route("/api/admin/approve_user", methods=["POST"])
@require_auth
@admin_only
def admin_approve_user():
    payload = request.json or {}
    uid = payload.get("user_id")
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    user = User.query.filter_by(id=uid).first()
    if not user:
        return jsonify({"error": "user not found"}), 404
    user.status = "APPROVED"
    user.promo_code = generate_promo_code(user.name)
    db.session.commit()

    # send welcome message via bot
    welcome = f"🎉 Hello {user.name}!\nYour account on AHADU MARKET has been <b>APPROVED</b>.\nYour promo code: <code>{user.promo_code}</code>\nWelcome aboard — sell well!"
    send_user_message(user.telegram_id, welcome)

    send_admin_message(f"✅ Approved {user.name} (ID {user.id}). Promo: {user.promo_code}")

    return jsonify({"ok": True, "user": user.to_dict()})


# Products
@app.route("/api/products", methods=["GET", "POST", "DELETE"])
@require_auth
def api_products():
    if request.method == "GET":
        products = Product.query.order_by(Product.created_at.desc()).all()
        return jsonify({"products": [p.to_dict() for p in products]})

    if request.method == "POST":
        if g.user.role != "admin":
            return jsonify({"error": "admin only to add products"}), 403
        data = request.json or {}
        name = data.get("name")
        specs = data.get("specs", "")
        try:
            price = float(data.get("price", 0))
            quantity = int(data.get("quantity", 0))
        except Exception:
            return jsonify({"error": "invalid price or quantity"}), 400
        if not name or price <= 0:
            return jsonify({"error": "name and positive price required"}), 400
        prod = Product(name=name, specs=specs, price=price, quantity=quantity)
        db.session.add(prod)
        db.session.commit()
        return jsonify({"product": prod.to_dict()})

    if request.method == "DELETE":
        if g.user.role != "admin":
            return jsonify({"error": "admin only to delete products"}), 403
        data = request.json or {}
        pid = data.get("product_id")
        prod = Product.query.filter_by(id=pid).first()
        if not prod:
            return jsonify({"error": "product not found"}), 404
        db.session.delete(prod)
        db.session.commit()
        return jsonify({"ok": True})


# Create a new sale/order
@app.route("/api/orders", methods=["GET", "POST"])
@require_auth
def api_orders():
    # Salespeople must be approved to create orders
    if request.method == "POST":
        if g.user.status != "APPROVED":
            return jsonify({"error": "your account is not approved yet"}), 403
        data = request.json or {}
        product_id = data.get("product_id")  # optional
        other_name = data.get("other_name")
        other_price = data.get("other_price")
        customer_name = data.get("customer_name")
        customer_phone = data.get("customer_phone")

        if not customer_name or not customer_phone:
            return jsonify({"error": "customer name and phone required"}), 400

        if product_id:
            product = Product.query.filter_by(id=product_id).first()
            if not product:
                return jsonify({"error": "product not found"}), 404
            price = product.price
        else:
            if (not other_name) or (not other_price):
                return jsonify({"error": "other_name and other_price required when product_id is not provided"}), 400
            try:
                price = float(other_price)
            except Exception:
                return jsonify({"error": "invalid other_price"}), 400

        order = Order(
            salesperson_id=g.user.id,
            product_id=product_id,
            other_name=other_name,
            other_price=other_price if other_price else None,
            customer_name=customer_name,
            customer_phone=customer_phone,
            status="PENDING"
        )
        db.session.add(order)
        db.session.commit()

        # Notify admin about pending order
        product_desc = product.name if product_id else other_name
        text = f"🛒 <b>New Order (PENDING)</b>\nSalesperson: {g.user.name}\nProduct: {product_desc}\nCustomer: {customer_name}\nPhone: {customer_phone}\nOrder ID: {order.id}"
        send_admin_message(text)

        return jsonify({"order": order.to_dict()})

    # GET: For admin show all pending/all orders; for salespeople show their orders
    if request.method == "GET":
        if g.user.role == "admin":
            orders = Order.query.order_by(Order.created_at.desc()).all()
        else:
            orders = Order.query.filter_by(salesperson_id=g.user.id).order_by(Order.created_at.desc()).all()
        out = []
        for o in orders:
            odata = o.to_dict()
            if o.product_id:
                prod = Product.query.filter_by(id=o.product_id).first()
                odata["product"] = prod.to_dict() if prod else None
            out.append(odata)
        return jsonify({"orders": out})


# Admin: approve or reject order
@app.route("/api/admin/order_action", methods=["POST"])
@require_auth
@admin_only
def admin_order_action():
    payload = request.json or {}
    order_id = payload.get("order_id")
    action = payload.get("action")  # 'approve' or 'reject'
    if not order_id or action not in ("approve", "reject"):
        return jsonify({"error": "order_id and valid action required"}), 400

    order = Order.query.filter_by(id=order_id).first()
    if not order:
        return jsonify({"error": "order not found"}), 404

    if action == "approve":
        order.status = "COMPLETED"
        # reduce inventory if product
        if order.product_id:
            p = Product.query.filter_by(id=order.product_id).first()
            if p and p.quantity > 0:
                p.quantity = max(0, p.quantity - 1)
        db.session.commit()

        # Calculate commission and notify salesperson and group
        price = 0.0
        if order.product_id:
            p = Product.query.filter_by(id=order.product_id).first()
            price = p.price if p else 0.0
        else:
            price = float(order.other_price or 0.0)
        commission = (price * COMMISSION_PERCENT) / 100.0 if price else 0.0
        sp = User.query.filter_by(id=order.salesperson_id).first()
        celebratory = f"🎉 <b>Order Approved</b>\nSalesperson: {sp.name}\nOrder ID: {order.id}\nCustomer: {order.customer_name}\nCommission: {commission:.2f} ETB"
        # Post to sales group / admin chat
        send_admin_message(celebratory)
        # Notify salesperson privately
        send_user_message(sp.telegram_id, f"✅ Your order (ID {order.id}) has been approved!\nCommission: {commission:.2f} ETB\nGreat job!")
        return jsonify({"ok": True, "order": order.to_dict()})

    else:  # reject
        order.status = "CANCELLED"
        db.session.commit()
        sp = User.query.filter_by(id=order.salesperson_id).first()
        send_user_message(sp.telegram_id, f"❌ Your order (ID {order.id}) has been rejected by admin.")
        send_admin_message(f"Order {order.id} rejected by admin.")
        return jsonify({"ok": True, "order": order.to_dict()})


# Leaderboard: monthly top performers
@app.route("/api/leaderboard", methods=["GET"])
@require_auth
def api_leaderboard():
    # compute for current month
    start_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    salesummary = {}
    completed_orders = Order.query.filter(Order.status == "COMPLETED", Order.created_at >= start_month).all()
    for o in completed_orders:
        spid = o.salesperson_id
        price = 0.0
        if o.product_id:
            p = Product.query.filter_by(id=o.product_id).first()
            price = p.price if p else 0.0
        else:
            price = float(o.other_price or 0.0)
        if spid not in salesummary:
            salesummary[spid] = {"total_sales": 0.0, "orders": 0}
        salesummary[spid]["total_sales"] += price
        salesummary[spid]["orders"] += 1

    leaderboard = []
    for spid, data in salesummary.items():
        user = User.query.filter_by(id=spid).first()
        leaderboard.append({
            "salesperson_id": spid,
            "name": user.name if user else "Unknown",
            "orders": data["orders"],
            "total_sales": round(data["total_sales"], 2),
        })

    leaderboard.sort(key=lambda x: x["total_sales"], reverse=True)
    return jsonify({"leaderboard": leaderboard})


# Payouts: admin view of commissions and mark paid
@app.route("/api/admin/payouts", methods=["GET", "POST"])
@require_auth
@admin_only
def admin_payouts():
    if request.method == "GET":
        # For each salesperson compute total sales this month, commission and unpaid commission
        start_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        users = User.query.filter(User.role == "sales", User.status == "APPROVED").all()
        out = []
        for u in users:
            orders = Order.query.filter(Order.salesperson_id == u.id, Order.status == "COMPLETED", Order.created_at >= start_month).all()
            total_sales = 0.0
            unpaid_commission = 0.0
            for o in orders:
                price = 0.0
                if o.product_id:
                    p = Product.query.filter_by(id=o.product_id).first()
                    price = p.price if p else 0.0
                else:
                    price = float(o.other_price or 0.0)
                total_sales += price
                comm = (price * COMMISSION_PERCENT) / 100.0
                if not o.commission_paid:
                    unpaid_commission += comm
            out.append({
                "salesperson_id": u.id,
                "name": u.name,
                "total_sales": round(total_sales, 2),
                "unpaid_commission": round(unpaid_commission, 2)
            })
        return jsonify({"payouts": out})

    # POST: Mark salesperson commissions as paid (for their completed orders this month)
    payload = request.json or {}
    salesperson_id = payload.get("salesperson_id")
    if not salesperson_id:
        return jsonify({"error": "salesperson_id required"}), 400
    start_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    orders = Order.query.filter(Order.salesperson_id == salesperson_id, Order.status == "COMPLETED", Order.created_at >= start_month, Order.commission_paid == False).all()
    for o in orders:
        o.commission_paid = True
    db.session.commit()
    return jsonify({"ok": True, "marked": len(orders)})


# Simple route to allow checking health
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# Admin bootstrap route (one-time) to create an admin
@app.route("/bootstrap_admin", methods=["POST"])
def bootstrap_admin():
    """
    One-time route (protected by SECRET_KEY header) to create an admin user.
    Use only once during setup. Provide header X-SECRET-KEY matching app.config['SECRET_KEY'].
    Body: {"name": "...", "telegram_id": "..."}
    """
    key = request.headers.get("X-SECRET-KEY")
    if key != app.config["SECRET_KEY"]:
        return jsonify({"error": "forbidden"}), 403
    data = request.json or {}
    name = data.get("name")
    telegram_id = data.get("telegram_id")
    if not name or not telegram_id:
        return jsonify({"error": "name and telegram_id required"}), 400
    existing = User.query.filter_by(telegram_id=str(telegram_id)).first()
    if existing:
        existing.role = "admin"
        existing.status = "APPROVED"
        db.session.commit()
        return jsonify({"ok": True, "user": existing.to_dict()})
    admin = User(name=name, telegram_id=str(telegram_id), role="admin", status="APPROVED")
    db.session.add(admin)
    db.session.commit()
    return jsonify({"ok": True, "user": admin.to_dict()})


# Webhook endpoint for Telegram updates (if using webhook)
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """
    Accepts Telegram updates if you set webhook to this endpoint.
    We process /start and other simple commands via the dispatcher.
    """
    update = request.get_json(force=True)
    # Use python-telegram-bot to process update
    try:
        from telegram import Update
        u = Update.de_json(update, bot)
        # create dispatcher here if not exists (but for webhook mode we will create dedicated dispatcher)
        # We'll process manually using the Updater dispatcher created in main block if present.
        if app.config.get("DISPATCHER"):
            app.config["DISPATCHER"].process_update(u)
    except Exception as e:
        app.logger.error("Failed to process incoming webhook: %s", str(e))
    return jsonify({"ok": True})


# -------------------------
# Start/Stop helpers
# -------------------------
def set_webhook_if_needed(url):
    """Set webhook on Telegram if WEBHOOK_URL is provided."""
    if url:
        try:
            bot.set_webhook(url)
            app.logger.info("Webhook set to %s", url)
        except TelegramError as e:
            app.logger.error("Failed to set webhook: %s", str(e))


def notify_startup():
    try:
        send_admin_message("🚀 AHADU MARKET backend is now online.")
    except Exception as e:
        app.logger.error("Startup notify failed: %s", str(e))


# -------------------------
# Main entrypoint
# -------------------------
if __name__ == "__main__":
    # Create DB tables
    with app.app_context():
        db.create_all()

    # Initialize Updater & Dispatcher for polling (if not using webhook)
    updater = Updater(token=bot_token, use_context=True, workers=4)
    dp = updater.dispatcher
    # attach handlers
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    # fallback message handler
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, cmd_help))

    # store dispatcher in app config so webhook endpoint can use it if needed
    app.config["DISPATCHER"] = dp

    # If WEBHOOK_URL is set, attempt to set webhook; otherwise start polling.
    if WEBHOOK_URL:
        set_webhook_if_needed(WEBHOOK_URL)
        # Run Flask (webhook mode) - telegram will call /telegram/webhook
        notify_startup()
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
    else:
        # Start polling in a background thread
        updater.start_polling()
        app.logger.info("Started Telegram polling (development mode).")
        notify_startup()
        # Run Flask for frontend + API
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=(os.getenv("FLASK_ENV") != "production"))
