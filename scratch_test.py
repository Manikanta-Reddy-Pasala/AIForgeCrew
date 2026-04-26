import asyncio
import httpx

async def main():
    async with httpx.AsyncClient() as client:
        resp = await client.post("http://127.0.0.1:8799/api/tickets", json={
            "title": "test assignee",
            "body": "this is a test",
            "assignee_role": "planner",
            "priority": "medium"
        })
        print(resp.status_code)
        print(resp.json())

if __name__ == "__main__":
    asyncio.run(main())
