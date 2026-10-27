"""Project A — active-recall revision for Cambridge 9618 AS Computer Science.

Run: python3 server.py   (then open http://localhost:8790)
Stdlib only: http.server + sqlite3. Questions live in data/questions.json.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = os.environ.get("PROJECT_A_DB", str(ROOT / "project_a.db"))
PORT = int(os.environ.get("PORT", 8790))
MASTERED = 2  # a mark point counts as learned after this many hits in a row

DATA = json.loads((ROOT / "data" / "questions.json").read_text())
CHAPTERS = DATA["chapters"]
QUESTIONS = {q["id"]: q for q in DATA["questions"]}


# ---------- marking ----------

def _norm(s):
    return " " + re.sub(r"\s+", " ", s.lower().replace("’", "'")) + " "


def grade(question, answer):
    """Indexes of mark points earned: every 'match' group needs one phrase present."""
    text = _norm(answer)
    hit = []
    for i, p in enumerate(question["points"]):
        if all(any(re.search(r"(?<![a-z0-9])" + re.escape(alt.lower()), text) for alt in group)
               for group in p["match"]):
            hit.append(i)
    return hit


# ---------- storage ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash BLOB NOT NULL, salt BLOB NOT NULL, role TEXT NOT NULL CHECK(role IN ('student','teacher')), exam_date TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id));
CREATE TABLE IF NOT EXISTS classes(id INTEGER PRIMARY KEY, teacher_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL, code TEXT UNIQUE NOT NULL, exam_date TEXT);
CREATE TABLE IF NOT EXISTS members(class_id INTEGER NOT NULL REFERENCES classes(id),
  student_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(class_id, student_id));
CREATE TABLE IF NOT EXISTS progress(user_id INTEGER NOT NULL, point_id TEXT NOT NULL, streak INTEGER NOT NULL DEFAULT 0,
  misses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id, point_id));
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, qid TEXT NOT NULL,
  score INTEGER NOT NULL, max INTEGER NOT NULL, at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def hash_pw(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)


def streaks(con, uid):
    return {r["point_id"]: r["streak"] for r in con.execute(
        "SELECT point_id, streak FROM progress WHERE user_id=?", (uid,))}


def chapter_mastery(st):
    out = {}
    for ch in CHAPTERS:
        pts = [f"{q['id']}.{i}" for q in QUESTIONS.values() if q["chapter"] == ch for i in range(len(q["points"]))]
        done = sum(st.get(p, 0) >= MASTERED for p in pts)
        out[ch] = {"title": CHAPTERS[ch], "mastered": done, "total": len(pts)}
    return out


def next_question(con, uid, chapter=None):
    """Least-learned question first; never the one just answered if there's a choice."""
    st = streaks(con, uid)
    last = {r["qid"]: r["at"] for r in con.execute(
        "SELECT qid, MAX(at) at FROM attempts WHERE user_id=? GROUP BY qid", (uid,))}
    recent = con.execute("SELECT qid FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    pool = [q for q in QUESTIONS.values() if not chapter or q["chapter"] == chapter]
    if recent and len(pool) > 1:
        pool = [q for q in pool if q["id"] != recent["qid"]]

    def learned(q):
        return sum(min(st.get(f"{q['id']}.{i}", 0), MASTERED) for i in range(len(q["points"]))) / (
            MASTERED * len(q["points"]))

    # ponytail: O(questions) scan per request, fine for a few hundred questions
    q = min(pool, key=lambda q: (learned(q), last.get(q["id"], ""), q["id"]))
    return q, learned(q) >= 1


def days_left(exam_date):
    if not exam_date:
        return None
    return (dt.date.fromisoformat(exam_date) - dt.date.today()).days


def summary(con, user):
    st = streaks(con, user["id"])
    chapters = chapter_mastery(st)
    total = sum(c["total"] for c in chapters.values())
    mastered = sum(c["mastered"] for c in chapters.values())
    left = days_left(user["exam_date"])
    today = con.execute("SELECT COUNT(*) FROM attempts WHERE user_id=? AND date(at)=date('now')",
                        (user["id"],)).fetchone()[0]
    unlearned_qs = sum(any(st.get(f"{q['id']}.{i}", 0) < MASTERED for i in range(len(q["points"])))
                       for q in QUESTIONS.values())
    # each unlearned question needs about MASTERED correct runs; spread them over the days left
    target = -(-unlearned_qs * MASTERED // max(left, 1)) if left is not None and left >= 0 else None
    return {"chapters": chapters, "mastered": mastered, "total": total, "days_left": left,
            "answered_today": today, "daily_target": target}


# ---------- http ----------

class Bad(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def need(body, key, maxlen=200):
    v = body.get(key)
    if not isinstance(v, str) or not v.strip() or len(v) > maxlen:
        raise Bad(400, f"'{key}' is missing or too long")
    return v.strip()


def valid_date(s):
    if s in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise Bad(400, "date must be YYYY-MM-DD")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, status, payload, extra_headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def user(self, con):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "sid" not in c:
            return None
        return con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                           (c["sid"].value,)).fetchone()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.route("GET", {})

    def do_POST(self):
        # JSON-only POSTs: a cross-site form can't send this content type without a CORS preflight
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "JSON only"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000:
            return self.send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "bad JSON"})
        self.route("POST", body)

    def route(self, method, body):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        con = db()
        try:
            with con:
                fn = ROUTES.get((method, path))
                if not fn:
                    raise Bad(404, "not found")
                result = fn(self, con, body, params)
            if isinstance(result, tuple):
                self.send(200, result[0], result[1])
            else:
                self.send(200, result)
        except Bad as e:
            self.send(e.status, {"error": e.msg})
        finally:
            con.close()


def login_cookie(con, uid):
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sessions VALUES(?,?)", (token, uid))
    secure = "; Secure" if os.environ.get("HTTPS") else ""
    return [("Set-Cookie", f"sid={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000{secure}")]


def signed_in(h, con, role=None):
    u = h.user(con)
    if not u:
        raise Bad(401, "please log in")
    if role and u["role"] != role:
        raise Bad(403, f"{role}s only")
    return u


def api_register(h, con, b, _):
    email, name = need(b, "email").lower(), need(b, "name", 80)
    pw = need(b, "password")
    if "@" not in email or len(pw) < 8:
        raise Bad(400, "need a real email and a password of 8+ characters")
    role = b.get("role") if b.get("role") in ("student", "teacher") else "student"
    salt = secrets.token_bytes(16)
    try:
        uid = con.execute("INSERT INTO users(email,name,pw_hash,salt,role) VALUES(?,?,?,?,?)",
                          (email, name, hash_pw(pw, salt), salt, role)).lastrowid
    except sqlite3.IntegrityError:
        raise Bad(409, "that email already has an account")
    return {"ok": True}, login_cookie(con, uid)


def api_login(h, con, b, _):
    u = con.execute("SELECT * FROM users WHERE email=?", (need(b, "email").lower(),)).fetchone()
    if not u or not hmac.compare_digest(hash_pw(need(b, "password"), u["salt"]), u["pw_hash"]):
        raise Bad(401, "wrong email or password")
    return {"ok": True}, login_cookie(con, u["id"])


def api_logout(h, con, b, _):
    c = cookies.SimpleCookie(h.headers.get("Cookie", ""))
    if "sid" in c:
        con.execute("DELETE FROM sessions WHERE token=?", (c["sid"].value,))
    return {"ok": True}, [("Set-Cookie", "sid=; Max-Age=0; Path=/")]


def api_me(h, con, b, _):
    u = signed_in(h, con)
    me = {"name": u["name"], "email": u["email"], "role": u["role"], "exam_date": u["exam_date"]}
    if u["role"] == "student":
        me.update(summary(con, u))
    return me


def api_exam_date(h, con, b, _):
    u = signed_in(h, con, "student")
    con.execute("UPDATE users SET exam_date=? WHERE id=?", (valid_date(b.get("date")), u["id"]))
    return {"ok": True}


def api_next(h, con, b, params):
    u = signed_in(h, con, "student")
    chapter = params.get("chapter")
    if chapter and chapter not in CHAPTERS:
        raise Bad(400, "unknown chapter")
    q, all_learned = next_question(con, u["id"], chapter)
    return {"id": q["id"], "chapter": q["chapter"], "chapter_title": CHAPTERS[q["chapter"]],
            "question": q["question"], "marks": len(q["points"]), "review": all_learned}


def api_answer(h, con, b, _):
    u = signed_in(h, con, "student")
    q = QUESTIONS.get(b.get("qid"))
    if not q:
        raise Bad(400, "unknown question")
    hit = set(grade(q, need(b, "answer", 5000)))
    for i in range(len(q["points"])):
        pid = f"{q['id']}.{i}"
        con.execute("INSERT OR IGNORE INTO progress(user_id, point_id) VALUES(?,?)", (u["id"], pid))
        if i in hit:
            con.execute("UPDATE progress SET streak=streak+1 WHERE user_id=? AND point_id=?", (u["id"], pid))
        else:
            con.execute("UPDATE progress SET streak=0, misses=misses+1 WHERE user_id=? AND point_id=?",
                        (u["id"], pid))
    con.execute("INSERT INTO attempts(user_id,qid,score,max) VALUES(?,?,?,?)",
                (u["id"], q["id"], len(hit), len(q["points"])))
    return {"score": len(hit), "max": len(q["points"]),
            "points": [{"text": p["text"], "hit": i in hit} for i, p in enumerate(q["points"])]}


def api_join(h, con, b, _):
    u = signed_in(h, con, "student")
    c = con.execute("SELECT * FROM classes WHERE code=?", (need(b, "code").upper(),)).fetchone()
    if not c:
        raise Bad(404, "no class with that code")
    con.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (c["id"], u["id"]))
    if c["exam_date"] and not u["exam_date"]:
        con.execute("UPDATE users SET exam_date=? WHERE id=?", (c["exam_date"], u["id"]))
    return {"ok": True, "class": c["name"]}


def api_create_class(h, con, b, _):
    u = signed_in(h, con, "teacher")
    code = secrets.token_hex(3).upper()
    con.execute("INSERT INTO classes(teacher_id,name,code) VALUES(?,?,?)", (u["id"], need(b, "name", 80), code))
    return {"ok": True, "code": code}


def api_class_exam_date(h, con, b, _):
    """Teacher sets the date for the whole class; students can still change their own afterwards."""
    u = signed_in(h, con, "teacher")
    date = valid_date(b.get("date"))
    c = con.execute("SELECT id FROM classes WHERE id=? AND teacher_id=?", (b.get("class_id"), u["id"])).fetchone()
    if not c:
        raise Bad(404, "not your class")
    con.execute("UPDATE classes SET exam_date=? WHERE id=?", (date, c["id"]))
    con.execute("UPDATE users SET exam_date=? WHERE id IN (SELECT student_id FROM members WHERE class_id=?)",
                (date, c["id"]))
    return {"ok": True}


def api_classes(h, con, b, _):
    u = signed_in(h, con, "teacher")
    out = []
    for c in con.execute("SELECT * FROM classes WHERE teacher_id=? ORDER BY id", (u["id"],)).fetchall():
        students, misses = [], {}
        for s in con.execute("SELECT u.* FROM members m JOIN users u ON u.id=m.student_id WHERE m.class_id=? "
                             "ORDER BY u.name", (c["id"],)).fetchall():
            sm = summary(con, s)
            students.append({"name": s["name"], "email": s["email"], "exam_date": s["exam_date"],
                             "mastered": sm["mastered"], "total": sm["total"],
                             "answered_today": sm["answered_today"],
                             "chapters": {k: v["mastered"] * 100 // max(v["total"], 1)
                                          for k, v in sm["chapters"].items()}})
            for r in con.execute("SELECT point_id, misses FROM progress WHERE user_id=? AND misses>0", (s["id"],)):
                misses[r["point_id"]] = misses.get(r["point_id"], 0) + r["misses"]
        weak = []
        for pid, n in sorted(misses.items(), key=lambda kv: -kv[1])[:8]:
            qid, i = pid.rsplit(".", 1)
            if qid in QUESTIONS:
                q = QUESTIONS[qid]
                weak.append({"question": q["question"], "point": q["points"][int(i)]["text"], "misses": n})
        out.append({"id": c["id"], "name": c["name"], "code": c["code"], "exam_date": c["exam_date"],
                    "students": students, "weak_points": weak})
    return {"classes": out, "chapters": CHAPTERS}


ROUTES = {
    ("POST", "/api/register"): api_register,
    ("POST", "/api/login"): api_login,
    ("POST", "/api/logout"): api_logout,
    ("GET", "/api/me"): api_me,
    ("POST", "/api/exam-date"): api_exam_date,
    ("GET", "/api/next"): api_next,
    ("POST", "/api/answer"): api_answer,
    ("POST", "/api/join"): api_join,
    ("POST", "/api/classes"): api_create_class,
    ("POST", "/api/class-exam-date"): api_class_exam_date,
    ("GET", "/api/classes"): api_classes,
}


def main():
    with db() as con:
        con.executescript(SCHEMA)
    print(f"Project A on http://localhost:{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
