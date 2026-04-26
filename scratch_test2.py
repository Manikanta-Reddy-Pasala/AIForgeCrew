import asyncio
import psycopg
from psycopg.rows import dict_row

from aiforge_core.runtime.config import AIFORGE_DSN
from aiforge_core.runtime import tickets
from aiforge_core.runtime.api import TicketCreate, create_ticket

def test_db():
    print("Testing ticket create via tickets_mod...")
    t = tickets.create(
        title="scratch test",
        body="test",
        assignee_role="planner",
        priority="medium"
    )
    print("Created ticket:", t.id, t.assignee_role)

    print("Testing ticket create via api...")
    payload = TicketCreate(
        title="scratch test 2",
        body="test 2",
        assignee_role="planner",
        priority="medium"
    )
    out = create_ticket(payload)
    print("Created ticket via API:", out["id"], out["assignee_role"])

if __name__ == "__main__":
    test_db()
