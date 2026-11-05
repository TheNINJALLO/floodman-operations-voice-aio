"""Minimal synchronous AGI exchange. Never log channel environments or values."""
import sys


def consume_environment() -> None:
    for line in sys.stdin:
        if not line.strip():
            return


def set_variable(name: str, value: str) -> None:
    quoted = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")
    print(f'SET VARIABLE {name} "{quoted}"', flush=True)
    # AGI is request/response. Exiting without consuming responses can close
    # stdin before Asterisk finishes applying the remaining transfer variables.
    response = sys.stdin.readline().strip()
    if response != "200 result=1":
        raise RuntimeError("Asterisk did not acknowledge the AGI variable")
