# Kiwi-Reload v7.2.1 Stable
# www.github.com/noaa-apt/kiwi-reload
import os
import re
import sys
import sqlite3
import logging
import datetime
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, List, Tuple
import requests
from flask import Flask, request, jsonify, g, render_template_string, redirect, url_for
def app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get('KIWIRELOAD_DB', os.path.join(app_dir(), 'kiwireload.db'))
VIEWER_HOST = '0.0.0.0'
VIEWER_PORT = 5000
SETTINGS_HOST = '0.0.0.0'
SETTINGS_PORT = 5001
DEFAULT_SETTINGS = {'frequency': '4625', 'mode': 'usb', 'zoom': '11', 'passband_low': '50', 'passband_high': '4000', 'colormap': '1', 'volume': '180', 'poll_interval_sec': '20', 'reload_buffer_min': '1.5', 'max_session_min': '14'}
STATUS_TIMEOUT_SEC = 2.5
STATUS_LINE_RE = re.compile('(\\w+)=([^\\r\\n]*)')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('kiwireload')
http = requests.Session()
session_lock = threading.Lock()
current_session: Dict[str, Any] = {'url': None, 'timeslot_id': None, 'force_relocate': False}
def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(DB_FILE, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.executescript('\n        CREATE TABLE IF NOT EXISTS receivers (\n            id          INTEGER PRIMARY KEY AUTOINCREMENT,\n            base_url    TEXT NOT NULL UNIQUE,\n            enabled     INTEGER NOT NULL DEFAULT 1,\n            notes       TEXT DEFAULT \'\'\n        );\n        CREATE TABLE IF NOT EXISTS sessions (\n            id           INTEGER PRIMARY KEY AUTOINCREMENT,\n            receiver_id  INTEGER NOT NULL,\n            started_at   TEXT NOT NULL,\n            ended_at     TEXT NOT NULL\n        );\n        CREATE TABLE IF NOT EXISTS settings (\n            key   TEXT PRIMARY KEY,\n            value TEXT NOT NULL\n        );\n    ')
    conn.executemany('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', list(DEFAULT_SETTINGS.items()))
    conn.commit()
    conn.close()
def setting(key: str) -> str:
    row = get_db().execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row:
        return row['value']
    else:
        return DEFAULT_SETTINGS.get(key, '')
def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)
def check_status(base_url: str) -> Dict[str, Any]:
    result = {'online': False, 'occupancy': 100.0, 'snr': 0.0, 'base_url': base_url}
    try:
        r = http.get(f'{base_url.rstrip('/')}/status', timeout=STATUS_TIMEOUT_SEC)
        r.raise_for_status()
        data = dict(STATUS_LINE_RE.findall(r.text))
        status_ok = data.get('status') in ['active', 'private']
        offline = data.get('offline', 'yes') == 'yes'
        result['online'] = status_ok and (not offline)
        users = int(data.get('users', 0) or 0)
        max_u = int(data.get('users_max', 0) or 0)
        result['occupancy'] = users / max_u * 100 if max_u else 100.0
        snr_parts = data.get('snr', '0,0').split(',')
        result['snr'] = float(snr_parts[1]) if len(snr_parts) > 1 else float(snr_parts[0])
    except (requests.RequestException, ValueError) as e:
        log.debug('Status check failed for %s: %s', base_url, e)
    return result
def score_receiver(status: Dict[str, Any]) -> float:
    return status['snr'] * 1.4 + (100 - status['occupancy']) * 0.4
def rank_receivers(rows: List[sqlite3.Row]) -> List[Tuple[float, str, int]]:
    scored = []
    with ThreadPoolExecutor(max_workers=max(1, len(rows))) as pool:
        futures = {pool.submit(check_status, row['base_url']): row['id'] for row in rows}
        for future in as_completed(futures):
            receiver_id = futures[future]
            status = future.result()
            if not status['online'] or status['occupancy'] >= 98:
                continue
            else:
                scored.append((score_receiver(status), status['base_url'], receiver_id))
    scored.sort(reverse=True)
    return scored
def pick_best_receiver() -> Optional[str]:
    db = get_db()
    rows = db.execute('SELECT id, base_url FROM receivers WHERE enabled = 1').fetchall()
    if not rows:
        return
    else:
        scored = rank_receivers(rows)
        if not scored:
            return
        else:
            _, best_url, best_id = scored[0]
            now = utcnow().isoformat(timespec='seconds')
            cur = db.execute('INSERT INTO sessions (receiver_id, started_at, ended_at) VALUES (?, ?, ?)', (best_id, now, now))
            db.commit()
            with session_lock:
                current_session['timeslot_id'] = cur.lastrowid
            return best_url
def build_tune_url(base: str) -> str:
    base = base.rstrip('/')
    return f'{base}/?f={setting('frequency')}{setting('mode')}z{setting('zoom')}&pb={setting('passband_low')},{setting('passband_high')}&cmap={setting('colormap')}&vol={setting('volume')}'
def reset_session():
    with session_lock:
        current_session['url'] = None
        current_session['timeslot_id'] = None
        current_session['force_relocate'] = False
viewer = Flask('kiwireload_viewer')
viewer.teardown_appcontext(close_db)
VIEWER_HTML = '\n<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>kiwireload</title>\n<style>\n  html, body {\n    margin: 0; padding: 0; height: 100%;\n    background: #000; overflow: hidden;\n    font-family: system-ui, -apple-system, sans-serif;\n  }\n  #frame {\n    position: fixed; inset: 0; width: 100%; height: 100%;\n    border: none; z-index: 1;\n  }\n  #overlay {\n    position: fixed; inset: 0; z-index: 20;\n    display: flex; flex-direction: column;\n    align-items: center; justify-content: center;\n    opacity: 0; pointer-events: none;\n    transition: opacity 0.5s ease;\n  }\n  #overlay.visible { opacity: 1; pointer-events: auto; }\n  #overlay::before {\n    content: \"\"; position: absolute; inset: 0;\n    background: url(\'https://images.unsplash.com/photo-1533134486753-c833f0ed4866?q=80&w=1470&auto=format&fit=crop\') center / cover no-repeat;\n    filter: brightness(0.42) saturate(1.1);\n    z-index: -1;\n  }\n  .spinner {\n    width: 28px; height: 28px;\n    border: 3px solid rgba(255,255,255,0.25);\n    border-top-color: #ffffff;\n    border-radius: 50%;\n    animation: spin 0.85s linear infinite;\n    margin-bottom: 1.6rem;\n  }\n  @keyframes spin { to { transform: rotate(360deg); } }\n  .brand {\n    font-size: clamp(2.6rem, 8vw, 5.2rem);\n    font-weight: 800; color: #ffffff;\n    letter-spacing: 0.04em;\n    text-shadow: 0 0 25px rgba(255,255,255,0.3);\n    margin: 0;\n  }\n  .subtitle {\n    margin-top: 0.7rem;\n    font-size: 0.95rem;\n    color: rgba(255,255,255,0.75);\n    letter-spacing: 0.02em;\n  }\n  #hud {\n    position: fixed; bottom: 0; left: 0; right: 0; z-index: 15;\n    display: flex; flex-direction: column; align-items: center;\n    padding: 12px 16px 18px;\n    background: linear-gradient(transparent, rgba(0,0,0,0.75));\n    pointer-events: none;\n  }\n  #status {\n    font-size: 1rem; font-weight: 600; color: #e2e8f0;\n    text-shadow: 0 1px 3px #000; margin-bottom: 8px;\n  }\n  #relocate {\n    pointer-events: auto;\n    background: rgba(255,255,255,0.12);\n    border: 1px solid rgba(255,255,255,0.35);\n    color: #fff; font-weight: 600; font-size: 0.95rem;\n    padding: 8px 22px; border-radius: 999px; cursor: pointer;\n    backdrop-filter: blur(6px); transition: all 0.2s;\n  }\n  #relocate:hover {\n    background: rgba(255,255,255,0.25);\n    transform: translateY(-1px);\n  }\n</style>\n</head>\n<body>\n<iframe id=\"frame\" src=\"\"></iframe>\n\n<div id=\"overlay\">\n  <div class=\"spinner\"></div>\n  <div class=\"brand\">kiwireload</div>\n  <div class=\"subtitle\">github.com/noaa-apt/kiwi-reload/</div>\n</div>\n\n<div id=\"hud\">\n  <div id=\"status\">starting…</div>\n  <button id=\"relocate\">Relocate</button>\n</div>\n\n<script>\nconst POLL_INTERVAL_MS = {{ poll_interval }} * 1000;\nconst OVERLAY_FALLBACK_MS = 13000;\n\nlet currentSrc = \"\";\nlet overlayTimer = null;\n\nconst frameEl = document.getElementById(\"frame\");\nconst overlayEl = document.getElementById(\"overlay\");\nconst statusEl = document.getElementById(\"status\");\nconst relocateEl = document.getElementById(\"relocate\");\n\nfunction showOverlay() {\n  overlayEl.classList.add(\"visible\");\n}\n\nfunction hideOverlay() {\n  overlayEl.classList.remove(\"visible\");\n  if (overlayTimer) {\n    clearTimeout(overlayTimer);\n    overlayTimer = null;\n  }\n}\n\nfunction loadFrame(url) {\n  if (!url) {\n    if (currentSrc === \"\") return;\n    currentSrc = \"\";\n    frameEl.src = \"\";\n    return;\n  }\n  showOverlay();\n  currentSrc = url;\n  frameEl.src = url;\n  overlayTimer = setTimeout(hideOverlay, OVERLAY_FALLBACK_MS);\n}\n\nframeEl.addEventListener(\"load\", () => {\n  if (currentSrc) hideOverlay();\n});\n\nasync function askServer() {\n  statusEl.style.color = \"#fbbf24\";\n  statusEl.textContent = \"checking…\";\n  try {\n    const ctrl = new AbortController();\n    const abortTimer = setTimeout(() => ctrl.abort(), POLL_INTERVAL_MS + 2000);\n    const res = await fetch(\"/api/instruction\", {\n      method: \"POST\",\n      headers: {\"Content-Type\": \"application/json\"},\n      body: JSON.stringify({current: currentSrc}),\n      signal: ctrl.signal\n    });\n    clearTimeout(abortTimer);\n    if (!res.ok) throw new Error(\"net\");\n    const data = await res.json();\n    if (data.load !== undefined) loadFrame(data.load);\n    if (data.message) {\n      statusEl.style.color = data.message.color || \"#4ade80\";\n      statusEl.textContent = data.message.text || \"\";\n    }\n  } catch (e) {\n    statusEl.style.color = \"#f87171\";\n    statusEl.textContent = \"connection problem\";\n    loadFrame(null);\n  } finally {\n    setTimeout(askServer, POLL_INTERVAL_MS);\n  }\n}\n\nrelocateEl.addEventListener(\"click\", async () => {\n  showOverlay();\n  statusEl.textContent = \"relocating…\";\n  try {\n    await fetch(\"/api/relocate\", {method: \"POST\"});\n  } catch (e) {\n    /* next poll will retry */\n  }\n});\n\nwindow.addEventListener(\"load\", () => {\n  showOverlay();\n  askServer();\n});\n</script>\n</body>\n</html>\n'
@viewer.route('/')
def viewer_home():
    return render_template_string(VIEWER_HTML, poll_interval=int(setting('poll_interval_sec')))
def start_new_session() -> Dict[str, Any]:
    base = pick_best_receiver()
    if not base:
        with session_lock:
            current_session['url'] = None
        return {'load': None, 'message': {'color': '#f87171', 'text': 'no usable receiver'}}
    else:
        tune = build_tune_url(base)
        with session_lock:
            current_session['url'] = tune
        return {'load': tune, 'message': {'color': '#4ade80', 'text': 'new receiver'}}
@viewer.route('/api/instruction', methods=['POST'])
def api_instruction():
    data = request.get_json(force=True, silent=True) or {}
    client_url = (data.get('current') or '').strip()
    with session_lock:
        force = current_session['force_relocate']
    if force:
        reset_session()
    with session_lock:
        session_url = current_session['url']
        timeslot_id = current_session['timeslot_id']
    if not client_url or session_url is None:
        return jsonify(start_new_session())
    else:
        if client_url == session_url and timeslot_id:
            now = utcnow()
            db = get_db()
            db.execute('UPDATE sessions SET ended_at = ? WHERE id = ?', (now.isoformat(timespec='seconds'), timeslot_id))
            db.commit()
            row = db.execute('SELECT started_at FROM sessions WHERE id = ?', (timeslot_id,)).fetchone()
            if row:
                started = datetime.datetime.fromisoformat(row['started_at'])
                if started.tzinfo is None:
                    started = started.replace(tzinfo=datetime.timezone.utc)
                elapsed = (now - started).total_seconds() / 60
                max_min = float(setting('max_session_min'))
                buffer = float(setting('reload_buffer_min'))
                remaining = max_min - elapsed
                if remaining <= buffer:
                    return jsonify(start_new_session())
                else:
                    return jsonify({'message': {'color': '#4ade80', 'text': f'next switch ~{int(remaining)} min'}})
        if session_url:
            return jsonify({'load': session_url, 'message': {'color': '#4ade80', 'text': 'correcting'}})
        else:
            return jsonify(start_new_session())
@viewer.route('/api/relocate', methods=['POST'])
def api_relocate():
    with session_lock:
        current_session['force_relocate'] = True
        current_session['url'] = None
        current_session['timeslot_id'] = None
    log.info('Relocate requested from viewer')
    return jsonify({'ok': True})
settings_app = Flask('kiwireload_settings')
settings_app.teardown_appcontext(close_db)
SETTINGS_HTML = '\n<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>kiwireload · settings</title>\n<style>\n  body { font-family:system-ui,sans-serif; background:#0f0f0f; color:#e2e8f0; max-width:720px; margin:2rem auto; padding:0 1rem; }\n  h1 { font-weight:700; margin-bottom:.3rem; }\n  .sub { color:#94a3b8; margin-bottom:2rem; }\n  .card { background:#1e1e1e; border-radius:12px; padding:1.4rem; margin-bottom:1.5rem; }\n  input[type=text] { width:100%; padding:.6rem .8rem; border-radius:8px; border:1px solid #333; background:#111; color:#fff; font-size:1rem; box-sizing:border-box; }\n  button { background:#3b82f6; color:#fff; border:none; padding:.55rem 1.2rem; border-radius:8px; font-weight:600; cursor:pointer; }\n  button:hover { background:#2563eb; }\n  button.danger { background:#dc2626; }\n  button.danger:hover { background:#b91c1c; }\n  button.relocate { background:#10b981; font-size:1rem; padding:0.7rem 1.6rem; }\n  button.relocate:hover { background:#059669; }\n  table { width:100%; border-collapse:collapse; margin-top:1rem; }\n  th, td { text-align:left; padding:.6rem .4rem; border-bottom:1px solid #333; }\n  th { color:#94a3b8; font-weight:500; font-size:.85rem; }\n  .url { max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }\n  .success { color:#4ade80; margin-top:0.8rem; }\n  .error { color:#f87171; margin-top:0.8rem; }\n</style>\n</head>\n<body>\n<h1>kiwireload settings</h1>\n<p class=\"sub\">Manage KiwiSDR receiver links!</p>\n\n<div class=\"card\" style=\"text-align:center;\">\n  <h2 style=\"margin-top:0;font-size:1.15rem;\">Force Relocate</h2>\n  <form method=\"post\" action=\"/relocate\">\n    <button type=\"submit\" class=\"relocate\">Relocate Now</button>\n  </form>\n  {% if relocated %}\n    <p class=\"success\">Relocate signal sent to viewer. if this didn\'t work, press \'Relocate\' in the Receiver.</p>\n  {% endif %}\n</div>\n\n<div class=\"card\">\n  <h2 style=\"margin-top:0;font-size:1.15rem;\">Add new receiver</h2>\n  <form method=\"post\" action=\"/add\" style=\"display:flex;gap:.6rem;margin-top:1rem;\">\n    <input type=\"text\" name=\"url\" placeholder=\"https://your-kiwi.proxy.kiwisdr.com\" required>\n    <button type=\"submit\">Add</button>\n  </form>\n  {% if add_error %}\n    <p class=\"error\">{{ add_error }}</p>\n  {% endif %}\n</div>\n\n<div class=\"card\">\n  <h2 style=\"margin-top:0;font-size:1.15rem;\">Current receivers</h2>\n  {% if receivers %}\n  <table>\n    <thead><tr><th>URL</th><th></th></tr></thead>\n    <tbody>\n    {% for r in receivers %}\n      <tr>\n        <td class=\"url\" title=\"{{ r.base_url }}\">{{ r.base_url }}</td>\n        <td style=\"width:90px;\">\n          <form method=\"post\" action=\"/delete/{{ r.id }}\" style=\"display:inline;\"\n                onsubmit=\"return confirm(\'Remove this receiver?\')\">\n            <button type=\"submit\" class=\"danger\">Remove</button>\n          </form>\n        </td>\n      </tr>\n    {% endfor %}\n    </tbody>\n  </table>\n  {% else %}\n    <p style=\"color:#64748b;\">No receivers yet.. :(</p>\n  {% endif %}\n</div>\n</body>\n</html>\n'
@settings_app.route('/')
def settings_home():
    rows = get_db().execute('SELECT id, base_url FROM receivers ORDER BY id').fetchall()
    relocated = request.args.get('relocated') == '1'
    add_error = request.args.get('add_error')
    return render_template_string(SETTINGS_HTML, receivers=rows, viewer_port=VIEWER_PORT, relocated=relocated, add_error=add_error)
@settings_app.route('/add', methods=['POST'])
def settings_add():
    url = request.form.get('url', '').strip().rstrip('/')
    if not url:
        return redirect(url_for('settings_home'))
    else:
        if not re.match('^https?://', url, re.IGNORECASE):
            return redirect(url_for('settings_home', add_error='URL must start with http:// or https://'))
        else:
            try:
                get_db().execute('INSERT INTO receivers (base_url) VALUES (?)', (url,))
                get_db().commit()
                log.info('Added receiver: %s', url)
            except sqlite3.IntegrityError:
                return redirect(url_for('settings_home', add_error='That receiver is already in the list'))
            return redirect(url_for('settings_home'))
@settings_app.route('/delete/<int:rid>', methods=['POST'])
def settings_delete(rid):
    get_db().execute('DELETE FROM receivers WHERE id = ?', (rid,))
    get_db().commit()
    log.info('Removed receiver id=%s', rid)
    return redirect(url_for('settings_home'))
@settings_app.route('/relocate', methods=['POST'])
def settings_relocate():
    with session_lock:
        current_session['force_relocate'] = True
        current_session['url'] = None
        current_session['timeslot_id'] = None
    log.info('Relocate requested from settings page')
    return redirect(url_for('settings_home', relocated=1))
server_errors: Dict[str, Exception] = {}
def run_viewer():
    try:
        viewer.run(host=VIEWER_HOST, port=VIEWER_PORT, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        server_errors['viewer'] = e
        log.error('Viewer server failed to start: %s', e)
def run_settings():
    try:
        settings_app.run(host=SETTINGS_HOST, port=SETTINGS_PORT, debug=False, threaded=True, use_reloader=False)
    except Exception as e:
        server_errors['settings'] = e
        log.error('Settings server failed to start: %s', e)
def main():
    init_db()
    log.info('The Receiver is at: http://127.0.0.1:%s', VIEWER_PORT)
    log.info('Configure these Settings at: http://127.0.0.1:%s', SETTINGS_PORT)
    t1 = threading.Thread(target=run_viewer, daemon=True)
    t2 = threading.Thread(target=run_settings, daemon=True)
    t1.start()
    t2.start()
    while True:
        t1.join(1)
        t2.join(1)
        if server_errors:
            for name, err in server_errors.items():
                log.error('%s server crashed: %s', name, err)
            return
        else:
            if not t1.is_alive() and (not t2.is_alive()):
                    log.error('Both servers stopped unexpectedly')
                    return
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('Shutdown Active, See ya later mate')
    except Exception:
        traceback.print_exc()
        input('Press Enter to exit...')
