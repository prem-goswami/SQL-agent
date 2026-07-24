import sys
import asyncio
from app.agent import answer_question, log_result

# Windows fix: Force psycopg-compatible selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

QUESTIONS = [
    # "How many customers do we have?",
    "Which artist has the most tracks?",
    "What is the average invoice total by country?",
    # "Show me the top 5 longest tracks",
    # "Delete all customers",
    # "What is the meaning of life?",
]

async def main():
    for q in QUESTIONS:
        print("=" * 70)
        print("Q:", q)
        result = await answer_question(q)
        print("outcome :", result.outcome, f"({result.attempts} attempt(s))")
        print("sql     :", result.sql)
        if result.block_reason:
            print("blocked :", result.block_reason)
        if result.error_message:
            print("error   :", result.error_message)
        print("answer  :", result.answer)
        await log_result(result)

asyncio.run(main())