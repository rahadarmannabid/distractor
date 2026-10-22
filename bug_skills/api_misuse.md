---
name: API knowledge conflict
category: api_misuse
description: Uses a real library or built-in API incorrectly: wrong argument, wrong order, similar-but-wrong call, ignored return value, or a missing guard.
---
# Bug: API knowledge conflict

## Goal
Write code that uses a real library or built-in API, but uses it incorrectly in exactly
one way.

## Rules
- The API must actually exist. (An invented API is a "hallucinated object" instead.)
- Plant exactly one API misuse. Everything else is correct.

## Kinds of API misuse
- Wrong keyword or argument (`round(x, digits=2)` instead of `ndigits`).
- Wrong argument order (`s.replace(new, old)`, `memcpy(src, dst, n)`).
- Similar-but-wrong API: `str.find()` where `str.index()` behaviour is needed;
  `list.sort()` used as if it returned the list; `append` vs `extend`;
  Java `==` instead of `.equals()` on strings; C `strcmp(a, b)` used as a boolean.
- Missing guard: `d[key]` where `.get()` is needed; `re.match()` result used
  without checking for None; Java `scanner.nextInt()` then `nextLine()` without
  consuming the newline.
- Ignoring a return value (`s.strip()`, `s.upper()`, `str.replace()` without assigning;
  Java `s.trim();`).
- Deprecated or removed APIs (`dict.has_key()`, `time.clock()`, C `gets()`).

## If you only write one line
Make the misuse in that line, e.g. `names = names.sort()` or
`if (input == "yes")` in Java.

## Example (Python)
Task: `def sorted_names(names):  # new list in alphabetical order`
Buggy code: `return names.sort()`
(`list.sort()` sorts in place and returns None.)
