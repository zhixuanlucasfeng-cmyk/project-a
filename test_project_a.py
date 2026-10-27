"""Run: python3 test_project_a.py"""
import json
import os
import tempfile
import threading
import urllib.request
from http.cookiejar import CookieJar

os.environ["PROJECT_A_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import server  # noqa: E402

# every question is well-formed
for q in server.QUESTIONS.values():
    assert q["chapter"] in server.CHAPTERS, q["id"]
    assert q["points"] and all(p["match"] and all(p["match"]) for p in q["points"]), q["id"]

# marking: full answer gets every point, a vague one gets none, word starts must line up
q = server.QUESTIONS["1-1"]
assert server.grade(q, "The MSB is negative. Flip all the bits then add 1.") == [0, 1, 2]
assert server.grade(q, "It's just a negative number.") == []
assert server.grade(server.QUESTIONS["8-1"], "primary key is unique. foreign key links to another table") == [0, 1, 2]

# full flow over HTTP
with server.db() as con:
    con.executescript(server.SCHEMA)
httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_port}"


def client():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    return call


t, s = client(), client()
assert t("/api/register", {"email": "t@x.com", "name": "Teach", "password": "password1", "role": "teacher"})[0] == 200
code = t("/api/classes", {"name": "12B"})[1]["code"]
cid = t("/api/classes")[1]["classes"][0]["id"]
assert t("/api/class-exam-date", {"class_id": cid, "date": "2099-01-01"})[0] == 200

assert s("/api/register", {"email": "s@x.com", "name": "Stu", "password": "short"})[0] == 400
assert s("/api/register", {"email": "s@x.com", "name": "Stu", "password": "password1"})[0] == 200
assert s("/api/join", {"code": code})[1]["class"] == "12B"
assert s("/api/me")[1]["exam_date"] == "2099-01-01"  # inherited from the class
assert s("/api/classes")[0] == 403  # students can't see teacher views

# answering a chapter-1 question twice fully marks those points learned
for _ in range(2):
    s("/api/next?chapter=1")
    s("/api/answer", {"qid": "1-1", "answer": "MSB is negative; flip the bits and add 1"})
me = s("/api/me")[1]
assert me["chapters"]["1"]["mastered"] == 3 and me["answered_today"] == 2, me
assert s("/api/next?chapter=1")[1]["id"] == "1-2"  # learned question goes to the back

s("/api/answer", {"qid": "1-2", "answer": "no idea"})
cls = t("/api/classes")[1]["classes"][0]
assert cls["students"][0]["mastered"] == 3 and cls["weak_points"][0]["misses"] == 1

assert client()("/api/me")[0] == 401
assert s("/api/login", {"email": "s@x.com", "password": "wrongpass"})[0] == 401
httpd.shutdown()
print("all checks passed")
