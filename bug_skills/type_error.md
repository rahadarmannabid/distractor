---
name: Type error
category: type_error
description: Logic is otherwise right, but one operation gets a value of the wrong type (string vs number, float index, list where a number is expected).
---
# Bug: Type error

## Goal
Write code that parses and whose logic is otherwise right, but where one operation
receives a value of the wrong type.

## Rules
- The code must be syntactically valid.
- Plant exactly one type mismatch. Everything else is correct.

## Kinds of type errors
- Concatenating a string and a number (`"Total: " + count` in Python).
- Indexing with a float (`items[len(items) / 2]` in Python 3).
- Calling a method on the wrong type (`.append()` on a str or tuple).
- Passing a list where a number is expected (`range(nums)`).
- Iterating over a number.
- Arithmetic between None/null and a number.
- Treating input read as text as if it were a number (or the reverse).
- C / C++ / Java: integer division where a decimal result is needed
  (`int avg = sum / n;`, `double r = a / b;` with int operands); a narrowing
  conversion that loses data.
- JavaScript: `+` on a string from input, giving concatenation instead of addition.

## If you only write one line
Put the mismatch in that line, e.g. `print("Total: " + total)` with a numeric total,
or `mid = items[len(items) / 2]`.

## Example (Python)
Task: `def middle_item(items):  # return the middle element`
Buggy code: `return items[len(items) / 2]`
(`/` returns a float, and list indices must be integers.)
