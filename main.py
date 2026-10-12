import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from langgraph.errors import GraphRecursionError

from agents import agent

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "runs.jsonl"

MAX_GRAPH_STEPS = 80


def save_run_log(record: dict) -> None:
    """Append one run record to the JSONL log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    task = input("Task: ").strip()

    if not task:
        print("Please enter a task.")
        return

    started = time.perf_counter()

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task": task,
        "status": "running",
        "tool_calls": [],
        "observations": [],
        "completed_steps": [],
        "error": None,
        "attempt_history": [],
    }

    try:
        result = agent.invoke(
            {
                "messages": [{"role": "user", "content": task}],
                "goal": task,
                "status": "running",
                "observations": [],
                "completed_steps": [],
                "retry_count": 0,
                "verification_reason": "",
                "verification_evidence": [],
                "attempt_history": [],
                "attempt_start_index": 0,
            },
            config={"recursion_limit": MAX_GRAPH_STEPS},
        )

        messages = result.get("messages", [])

        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                record["tool_calls"].append({
                    "name": call.get("name", "unknown"),
                    "args": call.get("args", {}),
                })

        record["observations"] = result.get("observations", [])
        record["completed_steps"] = result.get("completed_steps", [])
        record["attempt_history"] = result.get("attempt_history", [])
        # Output fromm the verifier llm
        record["status"] = result.get("status", "unknown")

        print("\n--- Attempt History ---")
        for attempt in record["attempt_history"]:
            print(
                f"Attempt {attempt['attempt']}: "
                f"{attempt['status']} - {attempt['reason']}"
            )

        print("\n--- Agent response ---")
        final = str(messages[-1].content).strip() if messages else ""
        print(final or "[The agent returned an empty final response.]")

        print("\n--- Task state ---")
        print("Goal:", result.get("goal", task))
        print("Status:", record["status"])
        print("Completed steps:", record["completed_steps"])
        print("Retry count:", result.get("retry_count", 0))

    except GraphRecursionError:
        record["status"] = "failed"
        record["error"] = {
            "type": "GraphRecursionError",
            "message": f"Exceeded {MAX_GRAPH_STEPS} graph steps. The agent "
                       "may be looping; try a stronger model.",
        }
        print(f"\nTask stopped: exceeded {MAX_GRAPH_STEPS} graph steps.")

    except Exception as error:
        record["status"] = "failed"
        record["error"] = {
            "type": type(error).__name__,
            "message": str(error)[:1000],
        }
        print(f"\nTask failed: {type(error).__name__}")
        print(str(error))

    finally:
        record["duration_seconds"] = round(time.perf_counter() - started, 2)

        try:
            save_run_log(record)
            print(f"\nRun log saved to: {LOG_FILE}")
        except OSError as error:
            print(f"\nWarning: could not save run log: {error}")


if __name__ == "__main__":
    main()
