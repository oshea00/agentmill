"""Multiple-task research example: run two research questions sequentially."""

import asyncio

from orchestrator.task_runner import TaskRunner

TASK_DESCRIPTIONS = [
    "What are the main features of Python 3.13?",
    "What are the key differences between Rust and Go for backend development?",
]


async def main() -> None:
    results = await TaskRunner(
        template_name="researcher",
        template_vars={"task_description": "Answer research questions"},
        task_descriptions=TASK_DESCRIPTIONS,
    ).run()

    for idx, (description, output) in enumerate(
        zip(TASK_DESCRIPTIONS, results), start=1
    ):
        print(f"\n--- Result {idx}: {description} ---")
        print(output if output else "(no result)")


if __name__ == "__main__":
    asyncio.run(main())
