from flask import Flask, request, jsonify
from flask_cors import CORS
import os, datetime, json
import hashlib
import secrets
from contextlib import contextmanager
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv

load_dotenv()

ADMIN_PASSWORD_HASH = hashlib.sha256(b"9050").hexdigest()

ACTIVE_TOKENS = set()


app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)  # allow your Admin/Client HTML to call this API


@app.route("/")
def index():
    return app.send_static_file("client.html")

DATABASE_URL = os.environ["DATABASE_URL"]
pool = SimpleConnectionPool(1, 10, DATABASE_URL)


@contextmanager
def db():
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)


init_db()


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

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (id, data) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                (data["id"], json.dumps(data))
            )

    return jsonify({"message": "Order created", "order": data}), 201


# ---------- list (for Admin Recent Orders) ----------
@app.route("/api/orders", methods=["GET"])
def list_orders():
    limit = max(1, min(int(request.args.get("limit", 200)), 1000))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM orders ORDER BY data->>'createdAt' DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
    return jsonify([r[0] for r in rows])

# ---------- get / track ----------
@app.route("/api/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM orders WHERE id = %s", (order_id,))
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify(row[0])

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

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET data = jsonb_set(data, '{speed}', %s::jsonb) WHERE id = %s RETURNING id",
                (json.dumps(speed), order_id)
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify({"ok": True, "id": order_id, "speed": speed})

# ---------- update status (and optional note) ----------
@app.route("/api/orders/<order_id>/status", methods=["PATCH"])
def set_status(order_id):
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("pending", "in-transit", "delivered", "loading"):
        return jsonify({"error": "Invalid status"}), 400
    pending_note = body.get("pendingNote", None)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM orders WHERE id = %s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Order not found"}), 404

            order = row[0]
            order["status"] = status
            order["pendingNote"] = pending_note

            # manage timestamps like your UI does
            if status == "in-transit" and not order.get("startedAt"):
                order["startedAt"] = now_iso()
            if status == "pending":
                order["startedAt"] = None
            if status == "delivered":
                order["completedAt"] = now_iso()

            cur.execute("UPDATE orders SET data = %s WHERE id = %s", (json.dumps(order), order_id))

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

    current_position = {"lat": lat, "lng": lng}

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET data = jsonb_set(data, '{currentPosition}', %s::jsonb) WHERE id = %s RETURNING id",
                (json.dumps(current_position), order_id)
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify({"ok": True, "currentPosition": current_position})

# ---------- delete ----------
@app.route("/api/orders/<order_id>", methods=["DELETE"])
def delete_order(order_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE id = %s RETURNING id", (order_id,))
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify({"ok": True, "deleted": order_id})


@app.route("/api/orders/<order_id>", methods=["PATCH"])
def patch_order(order_id):
    payload = request.json or {}

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM orders WHERE id = %s FOR UPDATE", (order_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Order not found"}), 404

            order = row[0]
            # allow updating nested fields, including adding name
            for key in ["origin", "destination", "currentPosition", "status", "pendingNote", "speed", "startedAt", "completedAt"]:
                if key in payload:
                    order[key] = payload[key]

            cur.execute("UPDATE orders SET data = %s WHERE id = %s", (json.dumps(order), order_id))

    return jsonify(order)


# ---------- dev run ----------
if __name__ == "__main__":
    # Gunicorn will run this as app:app in production; this is for local dev only.
    app.run(host="0.0.0.0", port=3001, debug=True)
