---
name: Misinterpreting the problem
category: misinterpretation
description: Runs cleanly and looks reasonable, but solves a slightly different problem than the task, docstring or function name asks for.
---
# Bug: Misinterpreting the problem

## Goal
Write code that is valid, runs without errors and looks reasonable, but does something
slightly different from what the task, comment, docstring or function name asks for.

## Rules
- The code must run without exceptions on normal inputs.
- The mismatch should be easy to miss on a quick read, but a check against the task catches it.
- Plant exactly one misinterpretation. Everything else is correct.

## Ways to misinterpret
- Reverse the requested order or direction (ascending vs descending, left vs right).
- Return a different quantity: count instead of sum, index instead of value,
  first match instead of all matches, minimum instead of maximum.
- Swap inclusive and exclusive meanings ("at least" vs "more than").
- Solve a simpler or more general version of the task.
- Ignore one stated constraint (case-insensitive, unique values, in place).

## If you only write one line
Flip the quantity or direction in that line, e.g. `return min(nums)` where the largest
value is asked for, or `count += 1` where the values should be summed.

## Example (Python)
Task: `def count_vowels(s):  # return the number of vowels in s`
Buggy code: `return sum(1 for ch in s.lower() if ch not in "aeiou" and ch.isalpha())`
(Counts consonants instead of vowels.)
