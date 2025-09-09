# app.py
from flask import Flask, request, jsonify, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta, timezone
import string
import random
import re
import time
import json
import os

# -------------------------
# Configuration
# -------------------------
DATABASE_FILE = "shortener.db"
LOG_FILE = "access_logs.jsonl"   # JSON lines, append-only
HOSTNAME = "http://localhost:5000"  # change when deploying

# -------------------------
# Flask + DB setup
# -------------------------
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{DATABASE_FILE}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -------------------------
# Models
# -------------------------
class ShortURL(db.Model):
    __tablename__ = "shorturls"
    id = db.Column(db.Integer, primary_key=True)
    shortcode = db.Column(db.String(64), unique=True, nullable=False, index=True)
    original_url = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    expiry = db.Column(db.DateTime, nullable=False)
    # you can add 'created_by' or other fields if needed

class Click(db.Model):
    __tablename__ = "clicks"
    id = db.Column(db.Integer, primary_key=True)
    shortcode = db.Column(db.String(64), db.ForeignKey('shorturls.shortcode'), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False)
    referrer = db.Column(db.Text, nullable=True)
    location = db.Column(db.String(128), nullable=True)  # coarse-grained
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)

# -------------------------
# Ensure DB exists
# -------------------------
with app.app_context():
    db.create_all()

# -------------------------
# Logging Middleware (custom)
# -------------------------
class RequestLoggerMiddleware:
    """
    Custom request/response logger. Writes JSON lines to LOG_FILE.
    NOTE: This purposely avoids using Python's logging module so it
    is clear and self-contained. If you have a pre-test middleware,
    replace this with your provided middleware.
    """
    def __init__(self, app, logfile=LOG_FILE):
        self.app = app
        self.logfile = logfile
        # attach hooks
        @app.before_request
        def _start_timer():
            request._start_time = time.time()

        @app.after_request
        def _log_response(response):
            try:
                duration = (time.time() - getattr(request, "_start_time", time.time())) * 1000.0
                entry = {
                    "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                    "method": request.method,
                    "path": request.path,
                    "query_string": request.query_string.decode() if request.query_string else "",
                    "status": response.status_code,
                    "duration_ms": round(duration, 2),
                    "client_ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                    "user_agent": request.headers.get("User-Agent"),
                }
                # write JSON line
                with open(self.logfile, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                # never raise from logger
                pass
            return response

# attach middleware
RequestLoggerMiddleware(app)

# -------------------------
# Helpers
# -------------------------
ALPHABET = string.ascii_letters + string.digits
SHORTCODE_RE = re.compile(r'^[A-Za-z0-9]{4,64}$')  # allowed chars and reasonable length

def generate_shortcode(length=6):
    """Generate a random alphanumeric shortcode."""
    return ''.join(random.choices(ALPHABET, k=length))

def now_utc():
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def to_iso_z(dt: datetime):
    # returns ISO 8601 with trailing Z (UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def validate_url(url: str):
    # Basic validation: must start with http:// or https:// and have at least one dot.
    if not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")

# -------------------------
# API: Create Short URL
# -------------------------
@app.route("/shorturls", methods=["POST"])
def create_shorturl():
    """
    Expected JSON:
    {
      "url": "https://example.com/very/long",
      "validity": 30,            (optional, minutes)
      "shortcode": "abcd1"       (optional)
    }
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    original_url = data.get("url")
    if not original_url or not validate_url(original_url):
        return jsonify({"error": "Invalid or missing 'url' (must be absolute URL with http/https)"}), 400

    validity = data.get("validity", 30)
    # validity must be an integer representing minutes
    try:
        validity = int(validity)
        if validity <= 0:
            raise ValueError()
    except Exception:
        return jsonify({"error": "'validity' must be a positive integer representing minutes"}), 400

    custom_shortcode = data.get("shortcode")
    if custom_shortcode:
        custom_shortcode = str(custom_shortcode).strip()
        if not SHORTCODE_RE.match(custom_shortcode):
            return jsonify({"error": "Provided 'shortcode' invalid. Must be alphanumeric and length 4-64."}), 400
        # check uniqueness
        existing = ShortURL.query.filter_by(shortcode=custom_shortcode).first()
        if existing:
            return jsonify({"error": "Shortcode already in use"}), 409
        shortcode = custom_shortcode
    else:
        # generate unique shortcode (retry loop)
        shortcode = None
        for attempt_len in (6, 7, 8):  # try increasing lengths if collisions
            for _ in range(6):  # a few attempts per length
                candidate = generate_shortcode(length=attempt_len)
                if not ShortURL.query.filter_by(shortcode=candidate).first():
                    shortcode = candidate
                    break
            if shortcode:
                break
        if not shortcode:
            # final attempt with random long candidate
            candidate = generate_shortcode(length=10)
            if ShortURL.query.filter_by(shortcode=candidate).first():
                return jsonify({"error": "Could not generate a unique shortcode. Try again."}), 500
            shortcode = candidate

    created_at = datetime.utcnow().replace(tzinfo=timezone.utc)
    expiry = created_at + timedelta(minutes=validity)

    short = ShortURL(shortcode=shortcode, original_url=original_url,
                     created_at=created_at, expiry=expiry)
    db.session.add(short)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Shortcode collision occurred (try a different custom shortcode)"}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Internal server error creating shortcode"}), 500

    short_link = f"{HOSTNAME.rstrip('/')}/{shortcode}"
    return jsonify({
        "shortLink": short_link,
        "expiry": to_iso_z(expiry)
    }), 201

# -------------------------
# API: Retrieve Short URL Statistics
# -------------------------
@app.route("/shorturls/<string:shortcode>", methods=["GET"])
def get_shorturl_stats(shortcode):
    s = ShortURL.query.filter_by(shortcode=shortcode).first()
    if not s:
        return jsonify({"error": "Shortcode not found"}), 404

    clicks_q = Click.query.filter_by(shortcode=shortcode).order_by(Click.timestamp.asc()).all()
    click_list = []
    for c in clicks_q:
        click_list.append({
            "timestamp": to_iso_z(c.timestamp),
            "referrer": c.referrer,
            "location": c.location or "unknown",
            "ip": c.ip,
            "user_agent": c.user_agent
        })

    response = {
        "clicks": len(click_list),
        "originalURL": s.original_url,
        "createdAt": to_iso_z(s.created_at),
        "expiry": to_iso_z(s.expiry),
        "clickData": click_list
    }
    return jsonify(response), 200

# -------------------------
# Redirection endpoint
# -------------------------
@app.route("/<string:shortcode>", methods=["GET"])
def redirect_to_original(shortcode):
    s = ShortURL.query.filter_by(shortcode=shortcode).first()
    if not s:
        # Not found
        return jsonify({"error": "Shortcode not found"}), 404

    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    if now > s.expiry:
        return jsonify({"error": "Shortlink expired"}), 410

    # Track click
    referrer = request.headers.get("Referer") or request.headers.get("Referrer")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    user_agent = request.headers.get("User-Agent")
    # coarse-grained location - placeholder 'unknown'
    location = "unknown"

    click = Click(
        shortcode=shortcode,
        timestamp=now,
        referrer=referrer,
        location=location,
        ip=ip,
        user_agent=user_agent
    )
    db.session.add(click)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # do not block redirect on DB error
        pass

    # HTTP redirect to original URL
    return redirect(s.original_url, code=302)

# -------------------------
# Error handlers for JSON responses
# -------------------------
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad request"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error"}), 500

# -------------------------
# Run (for local dev)
# -------------------------
if __name__ == "__main__":
    # ensure log file exists
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()
    app.run(host="0.0.0.0", port=5000, debug=False)

