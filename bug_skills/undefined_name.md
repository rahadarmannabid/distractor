---
name: Undefined name
category: undefined_name
description: Logic is right, but one variable is used without ever being defined, is misspelled, or is out of scope.
---
# Bug: Undefined name

## Goal
Write code whose logic is right, but where one variable is used without being defined
in an accessible scope.

## Rules
- The code must otherwise be syntactically valid.
- Plant exactly one undefined name. Everything else is correct.

## Difference from "hallucinated object"
Undefined names are plain variables or constants (data). Invented functions, methods
or modules are "hallucinated objects" instead.

## Kinds of undefined names
- A constant that is never declared (`tax_rate`, `MAX_SIZE`).
- A typo of an existing variable (`totl` instead of `total`, `lenght` instead of `length`).
- A variable used before it is assigned.
- A loop variable or local used outside its scope (C/C++/Java: `i` after a `for (int i ...)` loop).
- A variable from another function assumed to be shared.
- A variable assigned only inside one branch, then used after it.

## If you only write one line
Use a close misspelling of a variable already in the file, or a constant that was never
declared, e.g. `return totl` or `price = cost * TAX_RATE`.

## Example (Python)
Task: `def sum_list(nums):  # sum of all numbers`
Buggy code:
    total = 0
    for n in nums:
        total += n
    return totl
(`totl` is a misspelling of `total`.)
