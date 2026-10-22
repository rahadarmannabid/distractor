---
name: Syntax error
category: syntax_error
description: Logic is correct, but the code has exactly one grammar violation (unclosed bracket, missing colon or semicolon, unterminated string).
---
# Bug: Syntax error

## Goal
Write code whose logic is correct but which contains exactly one grammar violation, so
it does not compile or parse.

## Rules
- Apart from the syntax error, the logic must be correct.
- Plant exactly one syntax error, in the code you write (never in the existing code).
- Keep it small and easy to overlook. This bug shows up as soon as the code is run.

## Kinds of syntax errors
- Unclosed or mismatched parenthesis, bracket or brace.
- Unterminated string literal.
- Missing comma between list or argument items.
- Python: missing colon after `if`/`for`/`while`/`def`/`else`; `else if` instead of
  `elif`; inconsistent indentation; `=` instead of `==` in a condition.
- C / C++ / Java / JavaScript: missing semicolon; missing closing brace; `elif` instead
  of `else if`. (Note: `=` instead of `==` is NOT a syntax error in these languages.)

## If you only write one line
Drop one character from that line: a closing parenthesis, the trailing colon (Python)
or semicolon (C, C++, Java).

## Example (Python)
Task: `def max_of_two(a, b):  # return the larger of a and b`
Buggy code:
    if a > b
        return a
    return b
(Missing colon after `if a > b`.)
