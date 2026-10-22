---
name: Incomplete generation
category: incomplete_generation
description: Stops before the code is finished, as if the AI hit its length limit: cut off mid-statement, an empty block, or a missing final step.
---
# Bug: Incomplete generation

## Goal
Write code that stops before it is finished, as if you hit your token limit or stopped
early. What you do write must be correct and on track.

## Rules
- The code before the cut-off is correct.
- Stop at a natural-looking point: mid-expression, mid-line, or right after a block header.
- Always write some code. An empty reply is not a bug the student can see.
- Do not add a comment, `TODO` or marker saying the code is incomplete.

## Kinds of incomplete generation
- Cut off mid-expression (`return s ==`, `result *=`).
- Cut off after a block header (`for i in range(n):` with no body).
- Several correct lines, then the final `return` or output step is missing.
- Cut off inside a call, string or bracket (`print(f"Total: {`).
- A loop that never updates its accumulator because the update line is missing.

## If you only write one line
End the line early, e.g. `total = total +` or `if (count >`.

## Example (Python)
Task: `def factorial(n):  # return n!`
Buggy code:
    result = 1
    for i in range(2, n + 1):
        result *=
(Generation stops mid-statement.)
