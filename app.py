#!/usr/bin/env python3
"""
Cleaned and consolidated app.py for geofenced face-recognition attendance.

Notes:
- Put your background/template at templates/mark_attendance.html
- Static files at static/js, static/css, static/images
- Replace recognize_face(filepath) placeholder with your actual model call.
"""

import os
import time
import sqlite3
import logging
from math import radians, sin, cos, asin, sqrt
from flask import Flask, request, jsonify, send_from_directory, g, render_template
from werkzeug.utils import secure_filename
from collections import defaultdict

# ---------- Configuration ----------
COLLEGE_LAT = 12.80147378887274       # campus latitude
COLLEGE_LON = 80.22372835171538       # campus longitude
RADIUS_METERS = 1000                  # allowed radius
CONFIDENCE_THRESHOLD = 0.6            # min recognition confidence
TEMP_DIR = "temp"
DB_PATH = "attendance.db"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
# Rate limiting: allow one recognition request per (key) per RATE_WINDOW seconds
RATE_WINDOW = 2.0   # seconds between allowed requests from same key (IP or user)
# -----------------------------------

os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 MB

# ---------- Utilities ----------
def haversine_meters(lat1, lon1, lat2, lon2):
    """Distance between two lat/lon points in meters."""
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
    return db

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            timestamp INTEGER,
            latitude REAL,
            longitude REAL,
            distance REAL,
            confidence REAL,
            raw_filename TEXT
        )
    ''')
    conn.commit()
    conn.close()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

# ---------- Recognition placeholder ----------
def recognize_face(filepath):
    """
    Replace this placeholder with your model call.
    Must return (user_id, confidence) or (None, 0.0).
    Example:
        from model import recognize
        return recognize(filepath)
    """
    # TODO: integrate your model here
    return (None, 0.0)

# ---------- Small in-memory rate limiter ----------
_last_request_at = defaultdict(lambda: 0.0)
def rate_limited(key: str) -> bool:
    """Return True if request should be blocked (too soon), otherwise record and allow."""
    now = time.time()
    prev = _last_request_at[key]
    if now - prev < RATE_WINDOW:
        return True
    _last_request_at[key] = now
    return False

# ---------- Common messages ----------
OUTSIDE_MSG = "You are outside the radius of college, so go to college and mark your attendance."

def process_mark_request(lat_raw, lon_raw, file_storage, client_key=None):
    """Process a single attendance attempt. Returns (response_dict, status_code)."""
    # Basic validation
    if lat_raw is None or lon_raw is None:
        return ({'success': False, 'reason': 'missing_coords', 'message': 'Missing coordinates.'}, 400)
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except ValueError:
        return ({'success': False, 'reason': 'invalid_coords', 'message': 'Invalid coordinates.'}, 400)

    if file_storage is None or file_storage.filename == '':
        return ({'success': False, 'reason': 'missing_photo', 'message': 'No photo uploaded.'}, 400)
    if not allowed_file(file_storage.filename):
        return ({'success': False, 'reason': 'bad_file_type', 'message': 'Unsupported file type.'}, 400)

    # rate limit per client_key (IP or other identifier)
    if client_key is not None and rate_limited(client_key):
        return ({'success': False, 'reason': 'rate_limited', 'message': 'Too many requests. Slow down.'}, 429)

    # server-side location check
    dist = haversine_meters(COLLEGE_LAT, COLLEGE_LON, lat, lon)
    if dist > RADIUS_METERS:
        logging.info(f"Outside radius attempt: dist={dist:.1f}m from {client_key}")
        return ({'success': False, 'reason': 'outside_radius', 'distance': dist, 'message': OUTSIDE_MSG}, 403)

    # save file
    timestamp = int(time.time())
    safe_name = secure_filename(file_storage.filename)
    filename = f"{timestamp}_{safe_name}"
    filepath = os.path.join(TEMP_DIR, filename)
    try:
        file_storage.save(filepath)
    except Exception as e:
        logging.exception("Failed to save upload")
        return ({'success': False, 'reason': 'save_failed', 'message': 'Failed to save uploaded file.', 'error': str(e)}, 500)

    # run recognition
    try:
        user_id, confidence = recognize_face(filepath)
    except Exception as e:
        logging.exception("Recognition error")
        try:
            os.remove(filepath)
        except:
            pass
        return ({'success': False, 'reason': 'recognition_error', 'message': 'Recognition failed.', 'error': str(e)}, 500)

    # handle recognition result
    if user_id is None or confidence is None or confidence < CONFIDENCE_THRESHOLD:
        # log failed attempt
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('INSERT INTO attendance (user_id, timestamp, latitude, longitude, distance, confidence, raw_filename) VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (None, timestamp, lat, lon, dist, confidence if confidence else 0.0, filename))
            conn.commit()
        except Exception:
            logging.exception("Failed to log failed attempt")
        try:
            os.remove(filepath)
        except:
            pass
        return ({'success': False, 'reason': 'not_recognized', 'confidence': confidence, 'message': 'Face not recognized. Please try again.'}, 401)

    # mark attendance
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('INSERT INTO attendance (user_id, timestamp, latitude, longitude, distance, confidence, raw_filename) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (str(user_id), timestamp, lat, lon, dist, float(confidence), filename))
        conn.commit()
    except Exception as e:
        logging.exception("DB write failed")
        try:
            os.remove(filepath)
        except:
            pass
        return ({'success': False, 'reason': 'db_error', 'message': 'Failed to write attendance to DB.', 'error': str(e)}, 500)

    # cleanup file (keep if you want audits)
    try:
        os.remove(filepath)
    except:
        pass

    logging.info(f"Attendance marked: user={user_id} dist={dist:.1f} conf={confidence}")
    return ({'success': True, 'user_id': user_id, 'confidence': confidence, 'distance': dist, 'message': 'Attendance marked successfully.'}, 200)

# ---------- Routes ----------
@app.route('/')
def index():
    return jsonify({"status": "ok", "message": "Attendance server alive. Use /mark for UI."})

@app.route('/mark')
def mark_ui():
    try:
        return render_template('mark_attendance.html')
    except Exception as e:
        logging.exception("Template render failed")
        return jsonify({'status': 'error', 'message': f'template missing: {str(e)}'}), 500

@app.route('/verify_location', methods=['POST'])
def verify_location():
    data = request.get_json(silent=True) or {}
    lat = data.get('latitude'); lon = data.get('longitude')
    if lat is None or lon is None:
        return jsonify({'allowed': False, 'reason': 'missing_coords', 'message': 'Missing coordinates.'}), 400
    try:
        lat = float(lat); lon = float(lon)
    except ValueError:
        return jsonify({'allowed': False, 'reason': 'invalid_coords', 'message': 'Invalid coordinates.'}), 400
    dist = haversine_meters(COLLEGE_LAT, COLLEGE_LON, lat, lon)
    if dist > RADIUS_METERS:
        return jsonify({'allowed': False, 'distance': dist, 'message': OUTSIDE_MSG}), 403
    return jsonify({'allowed': True, 'distance': dist, 'message': 'You are inside the college radius. You may proceed.'})

@app.route('/mark_attendance', methods=['POST'])
def mark_attendance():
    lat = request.form.get('latitude'); lon = request.form.get('longitude')
    file = request.files.get('photo')
    client_key = request.remote_addr or 'unknown'
    resp, code = process_mark_request(lat, lon, file, client_key)
    return jsonify(resp), code

# Alias for older clients
@app.route('/recognize_face', methods=['POST'])
def recognize_face_route():
    lat = request.form.get('latitude'); lon = request.form.get('longitude')
    file = request.files.get('photo')
    client_key = request.remote_addr or 'unknown'
    resp, code = process_mark_request(lat, lon, file, client_key)
    return jsonify(resp), code

@app.route('/attendance_records', methods=['GET'])
def attendance_records():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, user_id, timestamp, latitude, longitude, distance, confidence, raw_filename FROM attendance ORDER BY timestamp DESC LIMIT 200')
    rows = cur.fetchall()
    conn.close()
    results = []
    for r in rows:
        results.append({
            'id': r[0], 'user_id': r[1], 'timestamp': r[2],
            'latitude': r[3], 'longitude': r[4], 'distance': r[5],
            'confidence': r[6], 'raw_filename': r[7]
        })
    return jsonify({'records': results})

@app.route('/temp/<path:filename>')
def temp_file(filename):
    return send_from_directory(TEMP_DIR, filename)

# ---------- Startup ----------
if __name__ == '__main__':
    init_db()
    logging.info(f"Starting attendance server on http://127.0.0.1:5000/mark (local only)")
    app.run(host='127.0.0.1', port=5000, debug=True)
