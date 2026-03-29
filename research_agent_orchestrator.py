"""Research agent example: wire up a researcher worker and run one task."""

import asyncio

from orchestrator.task_runner import TaskRunner

TASK_DESCRIPTION = "What are the main features of Python 3.13?"


async def main() -> None:
    results = await TaskRunner(
        template_name="researcher",
        template_vars={"task_description": TASK_DESCRIPTION},
        task_descriptions=[TASK_DESCRIPTION],
    ).run()
    if results[0]:
        print("\n--- Research Result ---")
        print(results[0])


if __name__ == "__main__":
    asyncio.run(main())
