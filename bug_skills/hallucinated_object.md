---
name: Hallucinated object
category: hallucinated_object
description: Calls a plausible-sounding function, method, class or module that does not exist anywhere.
---
# Bug: Hallucinated object

## Goal
Write code that relies on one plausible-sounding object that does not exist: not in the
standard library, not in the existing code, and not in any import.

## Rules
- The made-up object must sound believable for the task and the language.
- Do not define it anywhere. Use it naturally, as if it were a real API.
- Plant exactly one hallucinated object. Everything else is correct.

## Difference from "undefined name"
Hallucinated objects are callable things (functions, methods, classes, modules,
attributes) invented as if they were a real API or helper. A plain variable that was
never assigned is an "undefined name" instead.

## Kinds of hallucinated objects
- A helper function that was never written (`find_max_value(nums)`).
- A method that does not exist on a built-in type: Python `list.push()`,
  `str.reverse()`, `dict.sort_by_value()`; Java `String.reverse()`, `list.length()`;
  JavaScript `array.sum()`, `Math.clamp()`.
- A non-existent standard library function: `math.average()`, `os.path.filesize()`,
  C `strrev()` (not standard), C++ `std::sum()`.
- A non-existent import (`from collections import SortedList`).

## If you only write one line
Use the invented method in that line, e.g. `total = nums.sum()` (Python) or
`int big = Collections.maxValue(list);` (Java).

## Example (Python)
Task: `def reverse_words(sentence):  # word order reversed`
Buggy code:
    words = sentence.split()
    return " ".join(words.reversed_copy())
(Lists have no `reversed_copy()` method.)
