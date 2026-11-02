# Project A

Active-recall revision site for Cambridge 9618 AS Computer Science.
Type an answer, get marked against mark-scheme points, and the questions you know least come back first.
Set your exam date to get a daily target. Teachers can make a class, share a join code, and see each
student's progress and the class's most-missed points.

## Run

```
python3 server.py      # http://localhost:8790
python3 test_project_a.py
```

Python 3 standard library only (http.server + sqlite3). No install step.

## Questions

Questions live in `data/questions.json`. Each mark point is earned when the answer contains at least one
phrase from every group in its `match` list. The bundled questions are samples written for testing.

Marking is keyword-based, so it can't tell "the MSB is negative" from "the MSB is not negative".

## Deploy

`render.yaml` deploys to Render's free plan. The database lives in `/tmp` there, so it resets on restart.

## License

MIT
