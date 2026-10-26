// Interactive terminal (all conditions), included by index.html.
// A small shell: students type commands (python main.py, gcc main.c -o main && ./main, ls, cat…).
// Python/C run on the server over /ws/run with live output and stdin; JavaScript runs in a Web
// Worker. Server-side guardrails are in sandbox.py.
// Relies on globals from index.html: currentSession, logActivity, addTerminalOutput,
// queueActivityBatchUpload, runButton; and window.WS (workspace.js).
(function () {
    'use strict';

    var JS_TIMEOUT_MS = 10000;
    var JS_OUTPUT_LIMIT = 100000;
    var CWD = '~/project';
    // A program that reads all of its input (sys.stdin.read(), while (cin >> x), hasNext()) only
    // finishes at end of input, which a real terminal needs Ctrl+D for. For such programs the input
    // is ended for the student once they have typed at least one line and then stopped typing this long.
    var AUTO_EOF_MS = 2000;
    var READS_TO_EOF = {
        python: /\bstdin(?:\.buffer)?\.read(?:lines)?\s*\(|\bin\s+(?:sys\.)?stdin\b|\(\s*(?:sys\.)?stdin\s*\)|open\(\s*0\s*[,)]|\bfileinput\b|except\s*\(?\s*EOFError/,
        c: /\bEOF\b|while\s*\(\s*(?:scanf|fgets|getline)\s*\(/,
        cpp: /\bEOF\b|while\s*\(\s*(?:std::)?(?:cin\s*>>|getline\s*\(|scanf\s*\(|fgets\s*\()|istreambuf_iterator|\.eof\s*\(/,
        java: /\.hasNext\w*\s*\(|readLine\s*\(\s*\)\s*\)\s*!=\s*null|\.lines\s*\(|readAllBytes|transferTo/
    };
    function readsToEof(language) {
        var re = READS_TO_EOF[language];
        if (!re) return false;
        return WS.list().some(function (f) { return re.test(f.content || ''); });
    }

    function lang() { return currentSession.programmingLanguage || 'python'; }

    // Study log (study_log.js, table "compilation"): one row per run or compile by the student, linked to
    // the suggestion or agent version accepted last (which also gets this run as its next_compilation_id).
    var COMPILED = { c: 1, cpp: 1, java: 1 };
    function codeVersionId(code) {
        var h = 5381;
        for (var i = 0; i < code.length; i++) h = ((h << 5) + h + code.charCodeAt(i)) | 0;
        return 'v' + (h >>> 0).toString(36) + '_' + code.length;
    }
    function logCompilation(language, entry, compileOnly, trigger, res, started, code) {
        if (!window.StudyLog) return;
        var id = StudyLog.id('cr'), last = StudyLog.lastAccepted();
        var compileStatus = res.compile_error ? 'failure' : (COMPILED[language] ? (res.status === 'rejected' ? 'not_attempted' : 'success') : 'no_compile_step');
        var runStatus = compileOnly || res.compile_error ? 'not_run'
            : res.status === 'success' ? 'success'
            : res.status === 'rejected' ? 'rejected'
            : res.reason ? (res.reason === 'stopped' ? 'stopped' : 'stopped_' + res.reason)
            : 'runtime_error';
        StudyLog.upsert('compilation', id, {
            timestamp: new Date(started).toISOString(), compilation_attempt_id: id, code_version_id: codeVersionId(code),
            run_by: 'student', run_trigger: trigger || '', language: language, entry_file: entry, compile_only: compileOnly ? 'Yes' : 'No',
            compilation_status: compileStatus, compilation_error: res.compile_error ? (res.error || res.output || '').slice(0, 4000) : '',
            compilation_time_ms: res.compile_ms != null ? res.compile_ms : (res.compile_error ? res.duration_ms : ''),
            run_status: runStatus, exit_code: res.exit_code, runtime_error: runStatus === 'runtime_error' || runStatus.indexOf('stopped_') === 0 ? (res.error || '').slice(0, 4000) : '',
            output_excerpt: (res.output || '').slice(0, 2000), run_time_ms: res.compile_error ? '' : res.duration_ms,
            total_time_ms: Date.now() - started, stdin_lines: res.stdin_lines, input_wait_count: term.waitCount || 0,
            suggestion_id: last ? last.suggestion_id || '' : ''
        });
        if (last && !last.next_compilation_id) StudyLog.upsert(last.table, last.row_id, { next_compilation_id: id });
    }
    function log(type, d) { try { logActivity(type, d || {}); } catch (e) { /* ignore */ } }

    // ── Output + the single input line ───────────────────────────────────
    var term = {
        open: null,        // current unterminated output line
        input: null,       // the one <input>, used for commands and for program stdin
        promptEl: null,
        mode: 'shell',     // 'shell' | 'program'
        history: [],
        hIdx: 0,
        onProgramLine: null,
        onProgramKey: null,
        onProgramEof: null,
        onProgramStop: null,
        waiting: false,    // the running program is waiting for input (the server's "input_wait")
        out: function () { return document.getElementById('terminalOutput'); },
        newLine: function () {
            var div = document.createElement('div');
            div.className = 'output-line term-line';
            this.out().appendChild(div);
            this.open = div;
            return div;
        },
        // Append text exactly as printed; partial lines (input prompts) stay open.
        write: function (text, cls) {
            var self = this;
            var segs = String(text).split('\n');
            segs.forEach(function (seg, i) {
                if (i > 0) self.open = null;
                if (!seg) {
                    // a blank line the program printed ("a\n\nb"), kept so output looks the same as a real terminal
                    if (i > 0 && i < segs.length - 1) self.newLine().appendChild(document.createElement('br'));
                    return;
                }
                var line = self.open || self.newLine();
                var span = document.createElement('span');
                if (cls) span.className = cls;
                span.textContent = seg;
                var anchor = self.input && self.input.parentNode === line ? (self.mode === 'shell' ? self.promptEl : self.input) : null;
                line.insertBefore(span, anchor);
            });
            this.placeInput();
            this.scroll();
        },
        scroll: function () { var o = this.out(); if (o) o.scrollTop = o.scrollHeight; },
        ensureInput: function () {
            if (this.input) return;
            var self = this;
            this.promptEl = document.createElement('span');
            this.promptEl.className = 'term-prompt';
            this.input = document.createElement('input');
            this.input.className = 'term-input';
            this.input.setAttribute('aria-label', 'Terminal');
            this.input.autocomplete = 'off';
            this.input.spellcheck = false;
            this.input.addEventListener('keydown', function (e) { self.onKey(e); });
        },
        // Shell mode: prompt + input on their own last line. Program mode: input at the end of
        // the program's current line, so it sits right after prompts like "Name: ".
        placeInput: function () {
            this.ensureInput();
            if (this.mode === 'shell') {
                this.promptEl.innerHTML = '<span class="term-cwd">' + CWD + '</span> <span class="term-dollar">$</span> ';
                if (!this.open) this.newLine();
                this.open.appendChild(this.promptEl);
                this.open.appendChild(this.input);
                this.open.classList.add('term-line--prompt');
            } else {
                if (this.promptEl.parentNode) this.promptEl.remove();
                var line = this.open || this.newLine();
                if (this.input.parentNode !== line || line.lastChild !== this.input) line.appendChild(this.input);
            }
        },
        focus: function () { if (this.input) this.input.focus({ preventScroll: true }); },
        setMode: function (mode) {
            this.mode = mode;
            if (this.open && this.open.classList.contains('term-line--prompt') && mode === 'program') {
                // the program writes on this line now: drop the prompt and its flex layout
                if (this.promptEl.parentNode === this.open) this.promptEl.remove();
                this.open.classList.remove('term-line--prompt');
            }
            this.placeInput();
        },
        finish: function () { this.setWaiting(false); this.open = null; this.setMode('shell'); this.scroll(); },
        // Shows that the program wants input: the input line pulses with a placeholder, and a badge
        // next to the Terminal tab pulses too (the student may be looking at the editor).
        setWaiting: function (on) {
            on = !!on && this.mode === 'program';
            if (on === this.waiting) return;
            this.waiting = on;
            this.ensureInput();
            this.input.classList.toggle('term-input--waiting', on);
            this.input.placeholder = on ? '⌨ Waiting for input… type here and press Enter' : '';
            if (on) this.waitCount = (this.waitCount || 0) + 1;
            var badge = document.getElementById('termWaitBadge');
            if (!badge) {
                var tabs = document.querySelector('#terminal .panel-tabs');
                if (!tabs) return;
                badge = document.createElement('span');
                badge.id = 'termWaitBadge';
                badge.className = 'term-wait-badge';
                badge.setAttribute('role', 'status');
                badge.innerHTML = '<span class="term-wait-badge__dot"></span>Waiting for input';
                tabs.appendChild(badge);
            }
            badge.hidden = !on;
            if (on) { this.scroll(); log('terminal_input_wait', {}); }
        },
        // Freeze the current prompt line as a history line showing what was typed.
        echoCommand: function (cmd) {
            var line = this.open || this.newLine();
            var frozen = this.promptEl.cloneNode(true);
            if (this.promptEl.parentNode === line) this.promptEl.remove();
            if (this.input.parentNode === line) this.input.remove();
            line.appendChild(frozen);
            var span = document.createElement('span');
            span.className = 'term-command';
            span.textContent = cmd;
            line.appendChild(span);
            line.classList.remove('term-line--prompt');
            this.open = null;
        },

        onKey: function (e) {
            var input = this.input;
            if (this.mode === 'program') {
                if (this.onProgramKey) this.onProgramKey();
                if (e.key === 'Enter') {
                    e.preventDefault();
                    var v = input.value;
                    input.value = '';
                    this.write(v + '\n', 'term-stdin');
                    if (this.onProgramLine) this.onProgramLine(v + '\n');
                } else if (e.ctrlKey && (e.key === 'c' || e.key === 'C') && !window.getSelection().toString()) {
                    e.preventDefault();
                    this.write('^C\n', 'term-muted');
                    if (this.onProgramStop) this.onProgramStop();
                } else if (e.ctrlKey && (e.key === 'd' || e.key === 'D')) {
                    e.preventDefault();
                    if (this.onProgramEof) this.onProgramEof();
                }
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                var cmd = input.value;
                input.value = '';
                this.echoCommand(cmd);
                if (cmd.trim()) { this.history.push(cmd); }
                this.hIdx = this.history.length;
                Shell.execute(cmd, 'terminal');
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (this.hIdx > 0) { this.hIdx--; input.value = this.history[this.hIdx]; }
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (this.hIdx < this.history.length) { this.hIdx++; input.value = this.history[this.hIdx] || ''; }
            } else if (e.key === 'Tab') {
                e.preventDefault();
                Shell.complete(input);
            } else if (e.ctrlKey && (e.key === 'l' || e.key === 'L')) {
                e.preventDefault();
                Shell.clear();
            } else if (e.ctrlKey && (e.key === 'c' || e.key === 'C') && !window.getSelection().toString()) {
                e.preventDefault();
                var typed = input.value;
                input.value = '';
                this.echoCommand(typed + '^C');
                this.placeInput();
            }
        }
    };

    // ── Running programs ────────────────────────────────────────────────
    var running = null;   // { stop() }

    function setRunButton(isRunning) {
        var btn = document.getElementById('runButton');
        if (!btn) return;
        btn.classList.toggle('run-button--stop', isRunning);
        btn.innerHTML = isRunning
            ? '<svg viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" fill="currentColor"/></svg><span>Stop</span>'
            : '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M4 2.5v11l9-5.5z"/></svg><span>Run</span>';
        btn.title = isRunning ? 'Stop the program (Ctrl+C in the terminal)' : 'Run the current file';
    }

    function runOnServer(language, entry, compileOnly) {
        return new Promise(function (resolve) {
            var result = { status: 'error', output: '', error: '', exit_code: null, reason: '', stdin_lines: 0 };
            var proto = location.protocol === 'https:' ? 'wss' : 'ws';
            var ws = new WebSocket(proto + '://' + location.host + '/ws/run');
            var ended = false;
            function send(msg) { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }
            var autoEof = !compileOnly && readsToEof(language), eofTimer = null;
            function clearEof() { clearTimeout(eofTimer); eofTimer = null; }
            // (Re)start the wait once the student has entered a line; typing or new output restarts it.
            function armEof() {
                clearEof();
                if (!autoEof || !result.stdin_lines || ended) return;
                eofTimer = setTimeout(function () {
                    eofTimer = null;
                    if (ended) return;
                    if (term.input && term.input.value) { armEof(); return; }   // a line is still being typed
                    autoEof = false;
                    send({ type: 'eof' });
                    log('terminal_auto_eof', { entry_file: entry, stdin_lines: result.stdin_lines });
                }, AUTO_EOF_MS);
            }
            running = { stop: function () { send({ type: 'stop' }); } };
            term.onProgramLine = function (line) { result.stdin_lines++; send({ type: 'stdin', data: line }); armEof(); };
            term.onProgramKey = function () { if (eofTimer) armEof(); };
            term.onProgramEof = function () { send({ type: 'eof' }); };
            term.onProgramStop = function () { send({ type: 'stop' }); };
            term.setMode('program');
            term.focus();

            ws.onopen = function () {
                send({ session_id: currentSession.sessionId, language: language, files: WS.list(),
                       entry: entry, compile_only: !!compileOnly });
            };
            ws.onmessage = function (e) {
                var ev;
                try { ev = JSON.parse(e.data); } catch (err) { return; }
                if (ev.type === 'status') {
                    // Like a real terminal: a program waiting for input just shows the cursor.
                    if (ev.phase === 'queued') term.write('Waiting for a free slot on the server…\n', 'term-muted');
                } else if (ev.type === 'input_wait') {
                    if (!ended) term.setWaiting(ev.waiting);
                } else if (ev.type === 'output') {
                    term.setWaiting(false);
                    if (eofTimer) armEof();
                    term.write(ev.data, ev.stream === 'stderr' ? 'term-stderr' : '');
                    if (ev.stream === 'stderr') { if (result.error.length < 2000) result.error += ev.data; }
                    else if (result.output.length < 2000) result.output += ev.data;
                } else if (ev.type === 'exit') {
                    ended = true;
                    term.setWaiting(false);
                    clearEof();
                    result.exit_code = ev.exit_code;
                    result.duration_ms = ev.duration_ms;
                    result.compile_ms = ev.compile_ms;
                    result.reason = ev.reason || '';
                    result.compile_error = !!ev.compile_error;
                    result.status = ev.exit_code === 0 && !ev.reason ? 'success' : (ev.compile_error ? 'compile_error' : 'error');
                    term.open = null;
                    if (ev.message) term.write(ev.message + '\n', 'term-warning');
                    if (!ev.compile_error && !compileOnly && ev.exit_code !== 0 && !ev.reason && ev.exit_code !== null) {
                        term.write('Process exited with code ' + ev.exit_code + '\n', 'term-muted');
                    }
                } else if (ev.type === 'rejected') {
                    ended = true;
                    clearEof();
                    result.status = 'rejected';
                    result.error = ev.message || '';
                    term.write((ev.message || 'Could not run the program.') + '\n', 'term-warning');
                }
            };
            ws.onclose = function () {
                clearEof();
                if (!ended) term.write('\nLost connection to the server while running.\n', 'term-stderr');
                resolve(result);
            };
        });
    }

    // ── Shell ───────────────────────────────────────────────────────────
    function tokenize(line) {
        var out = [], re = /"([^"]*)"|'([^']*)'|(\S+)/g, m;
        while ((m = re.exec(line))) out.push(m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]);
        return out;
    }

    var Shell = {
        busy: false,
        built: {},          // C executables built in this session: name -> true

        clear: function () {
            var o = term.out();
            o.innerHTML = '';
            term.open = null;
            term.placeInput();
            term.focus();
            log('terminal_clear', { trigger: 'ctrl_l' });
        },

        complete: function (input) {
            var v = input.value;
            var parts = v.split(/\s+/);
            var last = parts[parts.length - 1];
            var names = WS.names();
            if (parts.length === 1) names = ['python', 'python3', 'gcc', 'g++', 'javac', 'java', 'node', 'ls', 'cat', 'clear', 'help', 'run', 'pwd', 'echo']
                .concat(Object.keys(this.built).map(function (n) { return './' + n; }));
            var hits = names.filter(function (n) { return n.indexOf(last) === 0; });
            if (hits.length === 1) {
                parts[parts.length - 1] = hits[0];
                input.value = parts.join(' ') + ' ';
            } else if (hits.length > 1) {
                term.echoCommand(v);
                term.write(hits.join('   ') + '\n', 'term-muted');
                input.value = v;
                term.placeInput();
                term.focus();
            }
        },

        execute: async function (line, trigger) {
            if (this.busy) return;
            var commands = line.split('&&').map(function (c) { return c.trim(); }).filter(Boolean);
            if (!commands.length) { term.placeInput(); return; }
            this.busy = true;
            setRunButton(true);
            try {
                for (var i = 0; i < commands.length; i++) {
                    var ok = await this.run(commands[i], trigger);
                    if (!ok) break;
                }
            } finally {
                this.busy = false;
                running = null;
                setRunButton(false);
                term.finish();
                term.focus();
            }
        },

        // Returns true if the command succeeded (so `&&` continues).
        run: async function (cmdline, trigger) {
            var argv = tokenize(cmdline);
            var cmd = argv[0];
            var args = argv.slice(1);
            log('terminal_command', { command: cmd, args: args.slice(0, 6).join(' ').slice(0, 120), trigger: trigger });
            var L = lang();

            if (cmd === 'help') {
                var lines = ['Available commands:'];
                if (L === 'python') lines.push('  python <file.py>        run a Python file (type input when it asks)');
                if (L === 'c') lines.push('  gcc <files.c> -o <name> compile C files', '  ./<name>                run the compiled program');
                if (L === 'cpp') lines.push('  g++ <files.cpp> -o <name> compile C++ files', '  ./<name>                run the compiled program');
                if (L === 'java') lines.push('  javac <files.java>      compile Java files', '  java <ClassName>        run a compiled class (e.g. java Main)');
                if (L === 'javascript') lines.push('  node <file.js>          run a JavaScript file');
                lines.push('  run                     run the current file (same as the Run button)',
                           '  ls                      list files        cat <file>   show a file',
                           '  clear                   clear the screen (Ctrl+L)',
                           'Keys: ↑/↓ history, Tab completes file names, Ctrl+C stops a running program, Ctrl+D ends input.');
                term.write(lines.join('\n') + '\n', 'term-muted');
                return true;
            }
            if (cmd === 'clear' || cmd === 'cls') { this.clear(); return true; }
            if (cmd === 'pwd') { term.write('/home/student/project\n'); return true; }
            if (cmd === 'echo') { term.write(args.join(' ') + '\n'); return true; }
            if (cmd === 'ls' || cmd === 'dir') {
                var detailed = args.indexOf('-l') >= 0 || args.indexOf('-la') >= 0;
                var names = WS.names().concat(Object.keys(this.built));
                term.write((detailed ? WS.list().map(function (f) { return String(f.content.length).padStart(7) + '  ' + f.filename; }).join('\n')
                    : names.join('   ')) + '\n');
                return true;
            }
            if (cmd === 'cat' || cmd === 'type') {
                if (!args.length) { term.write('usage: cat <file>\n', 'term-stderr'); return false; }
                var content = WS.get(args[0]);
                if (content === null) { term.write('cat: ' + args[0] + ': No such file\n', 'term-stderr'); return false; }
                term.write(content + (content.endsWith('\n') ? '' : '\n'));
                return true;
            }
            if (cmd === 'run') return this.runCurrent(trigger);

            if (cmd === 'python' || cmd === 'python3' || cmd === 'py') {
                if (L !== 'python') return this.unavailable(cmd);
                var file = args[0];
                if (!file) { term.write('usage: python <file.py>   (interactive Python is not available)\n', 'term-stderr'); return false; }
                if (WS.get(file) === null) { term.write(cmd + ": can't open file '" + file + "': No such file\n", 'term-stderr'); return false; }
                if (!/\.py$/.test(file)) { term.write(cmd + ': ' + file + ' is not a .py file\n', 'term-stderr'); return false; }
                return this.program('python', file, false, trigger);
            }
            var cxx = cmd === 'g++' || cmd === 'c++' || cmd === 'clang++';
            if (cmd === 'gcc' || cmd === 'cc' || cmd === 'clang' || cxx) {
                if (L !== (cxx ? 'cpp' : 'c')) return this.unavailable(cmd);
                var o = args.indexOf('-o');
                var out = o >= 0 && args[o + 1] ? args[o + 1] : 'a.out';
                var srcRe = cxx ? /\.(cpp|cc|cxx)$/ : /\.c$/;
                var sources = args.filter(function (a, i) { return srcRe.test(a) && args[i - 1] !== '-o'; });
                if (!sources.length) { term.write(cmd + ': fatal error: no input files\n', 'term-stderr'); return false; }
                var missing = sources.filter(function (s) { return WS.get(s) === null; });
                if (missing.length) { term.write(cmd + ': error: ' + missing[0] + ': No such file or directory\n', 'term-stderr'); return false; }
                var ok = await this.program(L, sources[0], true, trigger);
                if (ok) this.built[out] = true;
                return ok;
            }
            if (cmd === 'javac') {
                if (L !== 'java') return this.unavailable(cmd);
                var jsrc = args.filter(function (a) { return /\.java$/.test(a); });
                if (!jsrc.length) { term.write('error: no source files\n', 'term-stderr'); return false; }
                var jmissing = jsrc.filter(function (s) { return WS.get(s) === null; });
                if (jmissing.length) { term.write('error: file not found: ' + jmissing[0] + '\n', 'term-stderr'); return false; }
                var jok = await this.program('java', jsrc[0], true, trigger);
                if (jok) WS.names().forEach(function (n) { if (/\.java$/.test(n)) this.built[n.slice(0, -5)] = true; }, this);
                return jok;
            }
            if (cmd === 'java') {
                if (L !== 'java') return this.unavailable(cmd);
                var cls = (args.filter(function (a) { return a[0] !== '-'; })[0] || '').replace(/\.(java|class)$/, '');
                if (!cls) { term.write('usage: java <ClassName>   (e.g. java Main)\n', 'term-stderr'); return false; }
                // `java Main.java` runs a source file directly, like the real launcher.
                if (!this.built[cls] && !/\.java$/.test(args[0] || '')) {
                    term.write('Error: Could not find or load main class ' + cls + '  (compile first: javac ' + cls + '.java)\n', 'term-stderr');
                    return false;
                }
                if (WS.get(cls + '.java') === null) { term.write('Error: Could not find or load main class ' + cls + '\n', 'term-stderr'); return false; }
                return this.program('java', cls + '.java', false, trigger);
            }
            if (cmd && cmd.indexOf('./') === 0) {
                if (L !== 'c' && L !== 'cpp') return this.unavailable(cmd);
                var exe = cmd.slice(2);
                if (!this.built[exe]) {
                    term.write('no such file: ' + cmd + '  (compile first, e.g. gcc main.c -o ' + (exe || 'main') + ')\n', 'term-stderr');
                    return false;
                }
                return this.program(L, null, false, trigger);
            }
            if (cmd === 'node') {
                if (L !== 'javascript') return this.unavailable(cmd);
                if (!args[0] || WS.get(args[0]) === null) { term.write('node: cannot find ' + (args[0] || '<file>') + '\n', 'term-stderr'); return false; }
                return this.program('javascript', args[0], false, trigger);
            }
            term.write('command not found: ' + cmd + '   (type "help" to see available commands)\n', 'term-stderr');
            return false;
        },

        unavailable: function (cmd) {
            var hint = { python: 'python main.py', c: 'gcc main.c -o main && ./main', cpp: 'g++ main.cpp -o main && ./main',
                         java: 'javac Main.java && java Main', javascript: 'node main.js' }[lang()];
            term.write(cmd + ': not available in this ' + lang() + ' workspace. Try: ' + hint + '\n', 'term-stderr');
            return false;
        },

        // Command the Run button types for the current file.
        runCommandFor: function () {
            var L = lang(), active = WS.activeName(), names = WS.names();
            if (L === 'python') {
                var py = /\.py$/.test(active) ? active : (names.find(function (n) { return /\.py$/.test(n); }) || active);
                return 'python ' + py;
            }
            if (L === 'c') {
                var cs = names.filter(function (n) { return /\.c$/.test(n); });
                return 'gcc ' + (cs.length ? cs.join(' ') : active) + ' -o main && ./main';
            }
            if (L === 'cpp') {
                var cps = names.filter(function (n) { return /\.(cpp|cc|cxx)$/.test(n); });
                return 'g++ ' + (cps.length ? cps.join(' ') : active) + ' -o main && ./main';
            }
            if (L === 'java') {
                var js_ = names.filter(function (n) { return /\.java$/.test(n); });
                // Run the open file's class if it has a main method, else the first class that does.
                var hasMain = function (n) { return /public\s+static\s+(final\s+)?void\s+main\s*\(/.test(WS.get(n) || ''); };
                var mainFile = (/\.java$/.test(active) && hasMain(active)) ? active : (js_.find(hasMain) || js_[0] || active);
                return 'javac ' + (js_.length ? js_.join(' ') : active) + ' && java ' + mainFile.replace(/\.java$/, '');
            }
            var js = /\.js$/.test(active) ? active : (names.find(function (n) { return /\.js$/.test(n); }) || active);
            return 'node ' + js;
        },

        runCurrent: async function (trigger) {
            var L = lang();
            var commands = this.runCommandFor().split('&&').map(function (c) { return c.trim(); });
            if (commands.length === 2 && (L === 'c' || L === 'cpp' || L === 'java')) {
                // "compile && run" in one server call: the run step compiles again anyway, so two
                // calls would compile twice (slow, and doubles compiler load when a class runs at once).
                commands.forEach(function (c) {
                    var a = tokenize(c);
                    log('terminal_command', { command: a[0], args: a.slice(1, 7).join(' ').slice(0, 120), trigger: trigger });
                });
                var runArgv = tokenize(commands[1]);
                var entry = L === 'java' ? runArgv[1] + '.java' : tokenize(commands[0])[1];
                var res = await this.program(L, entry, false, trigger);
                if (this.lastResult && !this.lastResult.compile_error && this.lastResult.status !== 'rejected') {
                    if (L === 'java') WS.names().forEach(function (n) { if (/\.java$/.test(n)) this.built[n.slice(0, -5)] = true; }, this);
                    else this.built[runArgv[0].slice(2)] = true;
                }
                return res;
            }
            for (var i = 0; i < commands.length; i++) {
                if (!(await this.run(commands[i].trim(), trigger))) return false;
            }
            return true;
        },

        program: async function (language, entry, compileOnly, trigger) {
            var started = Date.now();
            term.waitCount = 0;
            currentSession.compilerClickCount = (currentSession.compilerClickCount || 0) + 1;
            var res;
            try {
                res = await runOnServer(language, entry, compileOnly);
            } catch (err) {
                res = { status: 'error', output: '', error: String(err && err.message || err), exit_code: null, reason: '', stdin_lines: 0 };
                term.write('Execution failed: ' + res.error + '\n', 'term-stderr');
            }
            this.lastResult = res;
            term.onProgramLine = term.onProgramKey = term.onProgramEof = term.onProgramStop = null;
            term.setMode('shell');
            var code = WS.list().map(function (f) { return f.content; }).join('\n');
            log('code_execution', {
                start_time: new Date(started).toISOString(),
                end_time: new Date().toISOString(),
                duration: Date.now() - started,
                execution_trigger: trigger,
                compile_only: !!compileOnly,
                entry_file: entry,
                file_count: WS.names().length,
                code_length: code.length,
                execution_number: currentSession.compilerClickCount,
                execution_result: res.status,
                exit_code: res.exit_code,
                stop_reason: res.reason || null,
                stdin_lines: res.stdin_lines,
                output_summary: res.output.substring(0, 100) + (res.output.length > 100 ? '...' : ''),
                error_message: res.error.substring(0, 100) + (res.error.length > 100 ? '...' : '')
            });
            try { queueActivityBatchUpload(); } catch (e) { /* ignore */ }
            logCompilation(language, entry, compileOnly, trigger, res, started, code);
            return res.status === 'success';
        }
    };

    // Run button: type the command for the current file into the terminal and run it.
    window.executeCode = function () {
        if (Shell.busy) { if (running) running.stop(); return; }
        if (!term.input) term.placeInput();
        var cmd = Shell.runCommandFor();
        term.input.value = '';
        term.echoCommand(cmd);
        term.history.push(cmd);
        term.hIdx = term.history.length;
        Shell.execute(cmd, 'run_button');
    };

    window.term = term;
    window.TerminalShell = Shell;

    window.initTerminal = function () {
        var o = term.out();
        if (!o) return;
        term.open = null;
        term.placeInput();
        o.addEventListener('click', function () {
            if (!String(window.getSelection())) term.focus();
        });
        setRunButton(false);
    };
})();
