---
name: Missing corner case
category: missing_corner_case
description: Correct for typical inputs but crashes or gives a wrong result on one edge case such as empty input, zero, None/null or a boundary value.
---
# Bug: Missing corner case

## Goal
Write code that is correct for ordinary inputs but breaks (crashes or returns a wrong
result) on one specific edge case.

## Rules
- The main logic must be correct for typical inputs, so a first ordinary run looks fine.
- Leave out exactly one needed guard or edge-case branch.
- Plant exactly one bug. Everything else is correct.

## Corner cases to omit
- Empty input: empty list, array, string or map (division by zero, index out of range).
- Zero or negative numbers (division by zero, negative index, log of 0).
- None / null values.
- Single-element input.
- Boundary values: off-by-one at the start or end of a range or loop.
- Duplicates, ties, or all-equal elements.
- Very large input (recursion too deep, integer overflow in C/C++/Java).

## If you only write one line
Write the line without its guard, e.g. `return sum(nums) / len(nums)` (fails on an empty
list) or `for (int i = 1; i < n; i++)` (skips the first element).

## Example (Python)
Task: `def first_word(sentence):  # return the first word`
Buggy code: `return sentence.split()[0]`
(Raises IndexError for an empty or whitespace-only string.)
