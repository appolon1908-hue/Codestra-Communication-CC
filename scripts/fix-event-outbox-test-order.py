#!/usr/bin/env python3
"""Make the event outbox attempt-fencing test independent of prior test rows."""
from pathlib import Path

path = Path("tests/test_stage5_postgres.py")
source = path.read_text(encoding="utf-8")
old = '''        assert recipient not in row.payload_json
        assert "event.test" not in row.payload_json
        assert row.state == "pending"
    claimed = await claim(1, 30, session_factory=sessions)
'''
new = '''        assert recipient not in row.payload_json
        assert "event.test" not in row.payload_json
        assert row.state == "pending"
        # This database is shared by the module's PostgreSQL tests. Make the
        # event under test the oldest eligible row so claim(1) proves its
        # fencing behavior without depending on unrelated pending events.
        row.created_at = datetime(1970, 1, 1, tzinfo=timezone.utc)
        await session.commit()
    claimed = await claim(1, 30, session_factory=sessions)
'''
count = source.count(old)
if count != 1:
    raise SystemExit(f"EVENT_TEST_PATCH_DRIFT=count:{count}")
path.write_text(source.replace(old, new, 1), encoding="utf-8")
print("EVENT_OUTBOX_TEST_ORDER=PASS")
