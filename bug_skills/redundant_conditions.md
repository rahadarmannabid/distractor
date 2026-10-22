---
name: Redundant conditions
category: redundant_conditions
description: Contains a redundant condition, duplicated branch or unnecessary cast. The output may still be correct; the redundancy is the defect.
---
# Bug: Redundant conditions

## Goal
Write code containing one redundant condition, duplicated branch, or unnecessary type
cast. The code may still give the correct output; the redundancy itself is the defect.

## Rules
- The code must run without errors.
- Include exactly one clear redundancy. Everything else is clean.
- Make it look like ordinary defensive or verbose code, not a deliberate mistake.

## Kinds of redundancy
- if/else branches that do the same thing.
- A condition that is always true or always false given earlier checks (`x > 5 && x > 3`).
- Re-checking a condition already guaranteed by an enclosing `if`.
- Unnecessary casts: `str()` inside `print()`, `int(len(x))`, `list(sorted(x))`,
  `(int)` on a value that is already an int.
- `if cond: return True else: return False` instead of `return cond`.
- A check inside a loop whose result never changes.

## If you only write one line
Add the redundancy to that line, e.g. `if age >= 18 and age >= 0:` or
`return True if x == y else False`.

## Example (Python)
Task: `def is_adult(age):  # True if age is 18 or more`
Buggy code:
    if age >= 18:
        if age >= 0:
            return True
        return True
    else:
        return False
(`age >= 0` is always true inside the outer `if`.)
