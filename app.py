from flask import Flask, request, jsonify
from flask_cors import CORS
import json, os, datetime, threading
import hashlib
import secrets


ADMIN_PASSWORD_HASH = hashlib.sha256(b"9050").hexdigest()

ACTIVE_TOKENS = set()


app = Flask(__name__)
CORS(app)  # allow your Admin/Client HTML to call this API

DB_FILE = "orders.json"
DB_LOCK = threading.Lock()

# ---------- tiny JSON "DB" helpers ----------
def load_orders():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_orders(data):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DB_FILE)

def now_iso():
    return datetime.datetime.utcnow().isoformat()

# ---------- health ----------
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/api/auth", methods=["POST"])
def admin_login():
    data = request.json or {}
    password = data.get("password", "")

    password_hash = hashlib.sha256(password.encode()).hexdigest()

    if password_hash != ADMIN_PASSWORD_HASH:
        return jsonify({"error": "Invalid credentials"}), 401

    token = secrets.token_hex(32)
    ACTIVE_TOKENS.add(token)

    return jsonify({"token": token})
def require_admin():
    token = request.headers.get("Authorization")
    if not token or token not in ACTIVE_TOKENS:
        return False
    return True


@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.json or {}
    if "id" not in data:
        return jsonify({"error": "Invalid data"}), 400

    def norm_place(obj, fallback_name_key):
        if isinstance(obj, dict) and "lat" in obj and "lng" in obj:
            if "name" not in obj:
                nm = data.get(fallback_name_key)
                if isinstance(nm, str) and nm.strip():
                    obj["name"] = nm.strip()
            return obj
        if isinstance(obj, list) and len(obj) == 2:
            return {
                "name": (data.get(fallback_name_key) or "").strip() or None,
                "lat": float(obj[0]),
                "lng": float(obj[1]),
            }
        return None

    data["origin"] = norm_place(data.get("origin"), "originName") or data.get("origin")
    data["destination"] = norm_place(data.get("destination"), "destinationName") or data.get("destination")
    data["currentPosition"] = data.get("currentPosition") or data["origin"]  # 👈 this line ensures map position

    for k in ("originName", "destinationName"):
        data.pop(k, None)

    data.setdefault("status", "pending")
    data.setdefault("pendingNote", None)
    data.setdefault("serviceType", "Fadex Logistics Courier Services")
    data.setdefault("weight", "N/A")
    data.setdefault("speed", 50)
    data.setdefault("createdAt", datetime.datetime.utcnow().isoformat() + "Z")

    orders = load_orders()
    orders[data["id"]] = data
    save_orders(orders)

    return jsonify({"message": "Order created", "order": data}), 201


# ---------- list (for Admin Recent Orders) ----------
@app.route("/api/orders", methods=["GET"])
def list_orders():
    limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    with DB_LOCK:
        orders = list(load_orders().values())
    # newest first by createdAt
    orders.sort(key=lambda o: o.get("createdAt", ""), reverse=True)
    return jsonify(orders[:limit])

# ---------- get / track ----------
@app.route("/api/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    with DB_LOCK:
        order = load_orders().get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(order)

# alias that your client example mentioned
@app.route("/api/track/<order_id>", methods=["GET"])
def track_order(order_id):
    return get_order(order_id)

# ---------- update speed ----------
@app.route("/api/orders/<order_id>/speed", methods=["PATCH"])
def set_speed(order_id):
    body = request.get_json(silent=True) or {}
    speed = body.get("speed")
    if speed is None:
        return jsonify({"error": "Missing 'speed'"}), 400
    try:
        speed = int(speed)
        if speed < 1 or speed > 1200:
            raise ValueError()
    except Exception:
        return jsonify({"error": "Speed must be integer 1..1200"}), 400

    with DB_LOCK:
        orders = load_orders()
        if order_id not in orders:
            return jsonify({"error": "Order not found"}), 404
        orders[order_id]["speed"] = speed
        save_orders(orders)
        return jsonify({"ok": True, "id": order_id, "speed": speed})

# ---------- update status (and optional note) ----------
@app.route("/api/orders/<order_id>/status", methods=["PATCH"])
def set_status(order_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("pending", "in-transit", "delivered", "loading"):
        return jsonify({"error": "Invalid status"}), 400
    pending_note = body.get("pendingNote", None)

    with DB_LOCK:
        orders = load_orders()
        order = orders.get(order_id)
        if not order:
            return jsonify({"error": "Order not found"}), 404

        order["status"] = status
        order["pendingNote"] = pending_note

        # manage timestamps like your UI does
        if status == "in-transit" and not order.get("startedAt"):
            order["startedAt"] = now_iso()
        if status == "pending":
            order["startedAt"] = None
        if status == "delivered":
            order["completedAt"] = now_iso()

        save_orders(orders)
        return jsonify({"ok": True, "order": order})

# ---------- update current position ----------
@app.route("/api/orders/<order_id>/position", methods=["PATCH"])
def set_position(order_id):
    body = request.get_json(silent=True) or {}
    try:
        lat = float(body["lat"])
        lng = float(body["lng"])
    except Exception:
        return jsonify({"error": "Provide numeric 'lat' and 'lng'"}), 400

    with DB_LOCK:
        orders = load_orders()
        order = orders.get(order_id)
        if not order:
            return jsonify({"error": "Order not found"}), 404
        order["currentPosition"] = {"lat": lat, "lng": lng}
        save_orders(orders)
        return jsonify({"ok": True, "currentPosition": order["currentPosition"]})

# ---------- delete ----------
@app.route("/api/orders/<order_id>", methods=["DELETE"])
def delete_order(order_id):
    with DB_LOCK:
        orders = load_orders()
        if order_id not in orders:
            return jsonify({"error": "Order not found"}), 404
        orders.pop(order_id)
        save_orders(orders)
        return jsonify({"ok": True, "deleted": order_id})
    

@app.route("/api/orders/<order_id>", methods=["PATCH"])
def patch_order(order_id):
    orders = load_orders()
    if order_id not in orders:
        return jsonify({"error": "Order not found"}), 404
    payload = request.json or {}
    # allow updating nested fields, including adding name
    for key in ["origin", "destination", "currentPosition", "status", "pendingNote", "speed", "startedAt", "completedAt"]:
        if key in payload:
            orders[order_id][key] = payload[key]
    save_orders(orders)
    return jsonify(orders[order_id])


# ---------- dev run ----------
if __name__ == "__main__":
    # Gunicorn will run this as app:app in production; this is for local dev only.
    app.run(host="127.0.0.1", port=3001, debug=True)
