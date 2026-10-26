#!/usr/bin/env python3
"""Compatibility check: realistic student programs in every IDE language, run through sandbox.py.

Covers each language's usual input styles (prompted, line by line, read until end of input),
standard libraries, multiple files, compile errors, runtime errors and the sandbox limits.

Usage (from the repo root; runs code locally, the same way the runner container does):
  python scripts/check_languages.py              # all languages
  python scripts/check_languages.py java cpp     # some
Exit code 1 if any case fails.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sandbox  # noqa: E402

sandbox.LIMITS["runs_per_minute"] = 10_000  # the suite runs far more than a student would

OK, COMPILE_ERROR, RUNTIME_ERROR = "ok", "compile_error", "runtime_error"


def case(name: str, lang: str, files: Dict[str, str], *, entry: Optional[str] = None, stdin: str = "",
         expect: str = OK, out: str = "", err: str = "", reason: str = "") -> Dict[str, Any]:
    return {"name": name, "lang": lang, "files": [{"filename": k, "content": v} for k, v in files.items()],
            "entry": entry or next(iter(files)), "stdin": stdin, "expect": expect, "out": out, "err": err,
            "reason": reason}


CASES: List[Dict[str, Any]] = [
    # ── Python ──
    case("hello", "python", {"main.py": "print('hello')"}, out="hello"),
    case("input() with prompt", "python", {"main.py": "n = int(input('n: '))\nprint(n * 2)"}, stdin="21\n", out="42"),
    case("read until EOF", "python", {"main.py": "import sys\nprint(sum(map(int, sys.stdin.read().split())))"},
         stdin="1 2\n3 4\n", out="10"),
    case("for line in sys.stdin", "python", {"main.py": "import sys\nfor line in sys.stdin:\n    print(line.strip().upper())"},
         stdin="ab\ncd\n", out="AB\nCD"),
    case("stdlib", "python", {"main.py": "import math, collections, itertools, heapq, bisect, functools, re, json, random, datetime, fractions, decimal, string, statistics\n"
                                         "print(math.comb(5,2), collections.Counter('aab').most_common(1)[0][0], heapq.nsmallest(1,[3,1]), json.dumps({'a':1}))"},
         out="10 a [1] {\"a\": 1}"),
    case("dataclass + typing", "python", {"main.py": "from dataclasses import dataclass\nfrom typing import List\n@dataclass\nclass P:\n    x: int\nps: List[P] = [P(1), P(2)]\nprint(sum(p.x for p in ps))"}, out="3"),
    case("deep recursion", "python", {"main.py": "import sys\nsys.setrecursionlimit(10000)\ndef f(n): return 0 if n == 0 else 1 + f(n - 1)\nprint(f(5000))"}, out="5000"),
    case("local module import", "python", {"main.py": "from helper import add\nprint(add(2, 3))", "helper.py": "def add(a, b):\n    return a + b"}, out="5"),
    case("write + read file", "python", {"main.py": "open('out.txt','w').write('hi')\nprint(open('out.txt').read())"}, out="hi"),
    case("unicode", "python", {"main.py": "print('héllo – 世界 ✓')"}, out="héllo – 世界 ✓"),
    case("runtime error traceback", "python", {"main.py": "x = [1]\nprint(x[5])"}, expect=RUNTIME_ERROR, err="IndexError"),
    case("syntax error", "python", {"main.py": "print('a'"}, expect=RUNTIME_ERROR, err="SyntaxError"),
    case("infinite loop is stopped", "python", {"main.py": "while True:\n    pass"}, expect=RUNTIME_ERROR, reason="cpu|timeout"),
    case("subprocess blocked", "python", {"main.py": "import subprocess\nsubprocess.run(['ls'])"}, expect=RUNTIME_ERROR, err="not allowed"),

    # ── C ──
    case("hello", "c", {"main.c": '#include <stdio.h>\nint main(void){ printf("hello\\n"); return 0; }'}, out="hello"),
    case("scanf with prompt", "c", {"main.c": '#include <stdio.h>\nint main(void){ int n; printf("n: "); scanf("%d", &n); printf("%d\\n", n*2); }'}, stdin="21\n", out="42"),
    case("read until EOF", "c", {"main.c": '#include <stdio.h>\nint main(void){ int x; long s = 0; while (scanf("%d", &x) == 1) s += x; printf("%ld\\n", s); }'}, stdin="1 2\n3 4\n", out="10"),
    case("fgets + string.h", "c", {"main.c": '#include <stdio.h>\n#include <string.h>\nint main(void){ char b[64]; fgets(b, sizeof b, stdin); b[strcspn(b, "\\n")] = 0; printf("[%s] %zu\\n", b, strlen(b)); }'}, stdin="hey\n", out="[hey] 3"),
    case("math.h (-lm)", "c", {"main.c": '#include <stdio.h>\n#include <math.h>\nint main(void){ volatile double x = 2.0; printf("%.3f %.1f\\n", sqrt(x), pow(x, 10)); }'}, out="1.414 1024.0"),
    case("malloc + qsort + bool", "c", {"main.c": '#include <stdio.h>\n#include <stdlib.h>\n#include <stdbool.h>\nint cmp(const void*a,const void*b){return *(int*)a-*(int*)b;}\nint main(void){ int *a = malloc(3*sizeof *a); a[0]=3;a[1]=1;a[2]=2; qsort(a,3,sizeof *a,cmp); bool ok = a[0]==1; printf("%d %d %d %d\\n",a[0],a[1],a[2],ok); free(a); }'}, out="1 2 3 1"),
    case("multiple files + header", "c", {"main.c": '#include <stdio.h>\n#include "util.h"\nint main(void){ printf("%d\\n", add(2, 3)); }', "util.h": "int add(int a, int b);", "util.c": '#include "util.h"\nint add(int a, int b){ return a + b; }'}, out="5"),
    case("C99 VLA + for-decl", "c", {"main.c": '#include <stdio.h>\nint main(void){ int n = 4; int a[n]; for (int i = 0; i < n; i++) a[i] = i*i; printf("%d\\n", a[3]); }'}, out="9"),
    case("compile error", "c", {"main.c": '#include <stdio.h>\nint main(void){ printf("x") return 0; }'}, expect=COMPILE_ERROR, err="error"),
    case("segfault reported", "c", {"main.c": '#include <stdio.h>\nint main(void){ int *p = 0; *p = 1; printf("x\\n"); }'}, expect=RUNTIME_ERROR, reason="crash|signal"),
    case("non-zero exit code", "c", {"main.c": "int main(void){ return 3; }"}, expect=RUNTIME_ERROR),
    case("infinite loop is stopped", "c", {"main.c": "int main(void){ volatile int x = 0; for(;;) x++; }"}, expect=RUNTIME_ERROR, reason="cpu|timeout"),

    # ── C++ ──
    case("hello", "cpp", {"main.cpp": '#include <iostream>\nint main(){ std::cout << "hello" << std::endl; }'}, out="hello"),
    case("cin with prompt", "cpp", {"main.cpp": '#include <iostream>\nusing namespace std;\nint main(){ int n; cout << "n: "; cin >> n; cout << n*2 << "\\n"; }'}, stdin="21\n", out="42"),
    case("read until EOF + fast io", "cpp", {"main.cpp": '#include <iostream>\nusing namespace std;\nint main(){ ios::sync_with_stdio(false); cin.tie(nullptr); long long x, s = 0; while (cin >> x) s += x; cout << s << "\\n"; }'}, stdin="1 2\n3 4\n", out="10"),
    case("getline", "cpp", {"main.cpp": '#include <iostream>\n#include <string>\nint main(){ std::string s; std::getline(std::cin, s); std::cout << s.size() << " " << s << "\\n"; }'}, stdin="hello world\n", out="11 hello world"),
    case("STL", "cpp", {"main.cpp": '#include <bits/stdc++.h>\nusing namespace std;\nint main(){ vector<int> v{3,1,2}; sort(v.begin(), v.end()); map<string,int> m{{"a",1}}; unordered_map<int,int> u; u[1]=2; set<int> s(v.begin(), v.end()); priority_queue<int> pq(v.begin(), v.end()); cout << v[0] << m["a"] << u[1] << s.size() << pq.top() << accumulate(v.begin(), v.end(), 0) << "\\n"; }'}, out="112336"),
    case("C++17 features", "cpp", {"main.cpp": '#include <iostream>\n#include <map>\n#include <optional>\n#include <string_view>\nint main(){ std::map<int,int> m{{1,2}}; for (auto [k, v] : m) std::cout << k + v; std::optional<int> o = 5; std::string_view sv = "ab"; if constexpr (sizeof(int) >= 4) std::cout << *o << sv << "\\n"; }'}, out="35ab"),
    case("classes + templates", "cpp", {"main.cpp": '#include <iostream>\ntemplate <typename T> T mx(T a, T b){ return a > b ? a : b; }\nstruct Shape { virtual double area() const = 0; virtual ~Shape() = default; };\nstruct Sq : Shape { double s; Sq(double s): s(s) {} double area() const override { return s*s; } };\nint main(){ Sq q(3); const Shape& sh = q; std::cout << mx(2, 7) << " " << sh.area() << "\\n"; }'}, out="7 9"),
    case("multiple files + header", "cpp", {"main.cpp": '#include <iostream>\n#include "util.hpp"\nint main(){ std::cout << add(2, 3) << "\\n"; }', "util.hpp": "#pragma once\nint add(int a, int b);", "util.cpp": '#include "util.hpp"\nint add(int a, int b){ return a + b; }'}, out="5"),
    case("cmath", "cpp", {"main.cpp": '#include <cmath>\n#include <cstdio>\nint main(){ volatile double x = 2.0; std::printf("%.3f\\n", std::sqrt(x)); }'}, out="1.414"),
    case("exception message", "cpp", {"main.cpp": '#include <vector>\nint main(){ std::vector<int> v; return v.at(3); }'}, expect=RUNTIME_ERROR, err="out_of_range|range"),
    case("compile error", "cpp", {"main.cpp": "int main(){ undefined_fn(); }"}, expect=COMPILE_ERROR, err="error"),

    # ── Java ──
    case("hello", "java", {"Main.java": 'public class Main { public static void main(String[] a){ System.out.println("hello"); } }'}, out="hello"),
    case("Scanner with prompt", "java", {"Main.java": 'import java.util.Scanner;\npublic class Main { public static void main(String[] a){ Scanner sc = new Scanner(System.in); System.out.print("n: "); int n = sc.nextInt(); System.out.println(n * 2); } }'}, stdin="21\n", out="42"),
    case("Scanner until EOF", "java", {"Main.java": 'import java.util.*;\npublic class Main { public static void main(String[] a){ Scanner sc = new Scanner(System.in); long s = 0; while (sc.hasNextInt()) s += sc.nextInt(); System.out.println(s); } }'}, stdin="1 2\n3 4\n", out="10"),
    case("BufferedReader", "java", {"Main.java": 'import java.io.*;\nimport java.util.*;\npublic class Main { public static void main(String[] a) throws IOException { BufferedReader br = new BufferedReader(new InputStreamReader(System.in)); StringTokenizer st = new StringTokenizer(br.readLine()); int x = Integer.parseInt(st.nextToken()), y = Integer.parseInt(st.nextToken()); System.out.println(x + y); } }'}, stdin="2 3\n", out="5"),
    case("collections + streams + records", "java", {"Main.java": 'import java.util.*;\nimport java.util.stream.*;\npublic class Main { record P(int x) {}\n public static void main(String[] a){ List<Integer> l = new ArrayList<>(List.of(3,1,2)); Collections.sort(l); Map<String,Integer> m = new HashMap<>(); m.merge("a", 1, Integer::sum); var s = l.stream().map(i -> i * 2).collect(Collectors.toList()); System.out.println(l + " " + m + " " + s + " " + new P(4).x()); } }'}, out="[1, 2, 3] {a=1} [2, 4, 6] 4"),
    case("multiple classes, one file", "java", {"Main.java": 'class Helper { static int add(int a, int b){ return a + b; } }\npublic class Main { public static void main(String[] a){ System.out.println(Helper.add(2, 3)); } }'}, out="5"),
    case("multiple files", "java", {"Main.java": 'public class Main { public static void main(String[] a){ System.out.println(new Calc().twice(4)); } }', "Calc.java": "public class Calc { int twice(int x){ return 2 * x; } }"}, out="8"),
    case("main class not named Main", "java", {"Solution.java": 'public class Solution { public static void main(String[] args){ System.out.println("sol"); } }'}, out="sol"),
    case("entry file without main", "java", {"Calc.java": "public class Calc { static int f(){ return 7; } }", "App.java": 'public class App { public static void main(String[] a){ System.out.println(Calc.f()); } }'}, entry="Calc.java", out="7"),
    case("unicode", "java", {"Main.java": 'public class Main { public static void main(String[] a){ System.out.println("héllo 世界"); } }'}, out="héllo 世界"),
    case("exception stack trace", "java", {"Main.java": 'public class Main { public static void main(String[] a){ int[] x = new int[1]; System.out.println(x[3]); } }'}, expect=RUNTIME_ERROR, err="ArrayIndexOutOfBoundsException"),
    case("compile error", "java", {"Main.java": 'public class Main { public static void main(String[] a){ int x = "s"; } }'}, expect=COMPILE_ERROR, err="error"),
    case("deep recursion", "java", {"Main.java": 'public class Main { static int f(int n){ return n == 0 ? 0 : 1 + f(n - 1); } public static void main(String[] a){ System.out.println(f(20000)); } }'}, out="20000"),
    case("infinite loop is stopped", "java", {"Main.java": "public class Main { public static void main(String[] a){ while (true) {} } }"}, expect=RUNTIME_ERROR, reason="cpu|timeout"),

    # ── JavaScript ──
    case("hello", "javascript", {"main.js": "console.log('hello')"}, out="hello"),
    case("readline line by line", "javascript", {"main.js": "const rl = require('readline').createInterface({ input: process.stdin });\nconst lines = [];\nrl.on('line', l => lines.push(l));\nrl.on('close', () => console.log(lines.map(Number).reduce((a, b) => a + b, 0)));"}, stdin="1\n2\n3\n", out="6"),
    case("readline question", "javascript", {"main.js": "const rl = require('readline').createInterface({ input: process.stdin, output: process.stdout });\nrl.question('n: ', a => { console.log(Number(a) * 2); rl.close(); });"}, stdin="21\n", out="42"),
    case("readline/promises", "javascript", {"main.js": "const rl = require('readline/promises').createInterface({ input: process.stdin, output: process.stdout });\n(async () => { const a = await rl.question('a? '); console.log(Number(a) * 3); rl.close(); })();"}, stdin="5\n", out="15"),
    case("fs.readFileSync(0)", "javascript", {"main.js": "const data = require('fs').readFileSync(0, 'utf8').trim().split(/\\s+/).map(Number);\nconsole.log(data.reduce((a, b) => a + b, 0));"}, stdin="1 2\n3 4\n", out="10"),
    case("process.stdin events", "javascript", {"main.js": "let s = '';\nprocess.stdin.on('data', d => s += d);\nprocess.stdin.on('end', () => console.log(s.trim().toUpperCase()));"}, stdin="abc\n", out="ABC"),
    case("modern syntax", "javascript", {"main.js": "class A { #x = 1; get x() { return this.#x; } }\nconst m = new Map([[1, 'a']]);\nconst o = { a: { b: 2 } };\nconsole.log(new A().x, m.get(1), o?.a?.b ?? 0, [3, 1, 2].toSorted?.() ?? [1, 2, 3], 2n ** 10n);"}, out="1 a 2 [ 1, 2, 3 ] 1024n"),
    case("async/await + timers", "javascript", {"main.js": "const wait = ms => new Promise(r => setTimeout(r, ms));\n(async () => { await wait(50); console.log('done'); })();"}, out="done"),
    case("require local module", "javascript", {"main.js": "const { add } = require('./util');\nconsole.log(add(2, 3));", "util.js": "module.exports.add = (a, b) => a + b;"}, out="5"),
    case("unicode", "javascript", {"main.js": "console.log('héllo 世界 ✓')"}, out="héllo 世界 ✓"),
    case("runtime error", "javascript", {"main.js": "const o = undefined;\nconsole.log(o.x);"}, expect=RUNTIME_ERROR, err="TypeError"),
    case("syntax error", "javascript", {"main.js": "console.log('a'"}, expect=RUNTIME_ERROR, err="SyntaxError"),
    case("infinite loop is stopped", "javascript", {"main.js": "while (true) {}"}, expect=RUNTIME_ERROR, reason="cpu|timeout"),
    case("child_process blocked", "javascript", {"main.js": "require('child_process').execSync('ls')"}, expect=RUNTIME_ERROR, err="ERR_ACCESS_DENIED|permission|denied"),
]


async def run_case(c: Dict[str, Any]) -> Dict[str, Any]:
    out: List[str] = []
    err: List[str] = []

    async def emit(ev: Dict[str, Any]) -> None:
        if ev["type"] == "output":
            (out if ev["stream"] == "stdout" else err).append(ev["data"])

    queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
    if c["stdin"]:
        queue.put_nowait(c["stdin"])
    queue.put_nowait(None)
    t = time.monotonic()
    try:
        res = await sandbox.run_code(key="check-" + c["lang"] + c["name"], language=c["lang"], files=c["files"],
                                     entry=c["entry"], emit=emit, stdin=queue, batch=True)
    except sandbox.RunRejected as e:
        res = {"exit_code": None, "reason": "rejected", "message": str(e)}
    return {**res, "stdout": "".join(out), "stderr": "".join(err) + (res.get("message") or ""),
            "secs": time.monotonic() - t}


def verdict(c: Dict[str, Any], r: Dict[str, Any]) -> Optional[str]:
    import re
    if c["expect"] == OK and (r["exit_code"] != 0 or r.get("reason")):
        return f"expected success, got exit={r['exit_code']} reason={r.get('reason')!r}"
    if c["expect"] == COMPILE_ERROR and not r.get("compile_error"):
        return "expected a compile error"
    if c["expect"] == RUNTIME_ERROR and (r.get("compile_error") or (r["exit_code"] == 0 and not r.get("reason"))):
        return "expected a runtime failure"
    if c["out"] and r["stdout"].strip().replace("\r", "") != c["out"].strip() and c["out"] not in r["stdout"]:
        return f"stdout {r['stdout'][-120:]!r} != {c['out']!r}"
    if c["err"] and not re.search(c["err"], r["stderr"], re.I):
        return f"stderr lacks /{c['err']}/: {r['stderr'][-160:]!r}"
    if c["reason"] and not re.fullmatch(c["reason"], r.get("reason") or ""):
        return f"reason {r.get('reason')!r} !~ {c['reason']}"
    return None


async def main() -> int:
    langs = sys.argv[1:] or ["python", "c", "cpp", "java", "javascript"]
    failed = 0
    for lang in langs:
        cases = [c for c in CASES if c["lang"] == lang]
        results = await asyncio.gather(*[run_case(c) for c in cases])
        print(f"\n{lang} ({len(cases)} cases)")
        for c, r in zip(cases, results):
            problem = verdict(c, r)
            failed += bool(problem)
            print(f"  {'FAIL' if problem else 'ok  '} {c['name']:<32} {r['secs']:5.2f}s" + (f"  ← {problem}" if problem else ""))
            if problem and os.getenv("VERBOSE"):
                print("       stdout:", r["stdout"][-400:], "\n       stderr:", r["stderr"][-600:])
    print(f"\n{len(CASES) if len(langs) == 5 else '…'} cases, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
