// AI assistance per study condition, included by index.html:
//   1 = No AI, 2 = single-line inline ghost-text suggestions (Copilot-style), 3 = coding agent
//   (Cursor-style), 4 = multi-line ghost-text suggestions (the server decides the length).
// Relies on globals from index.html: currentSession, logActivity; and window.WS (workspace.js).
(function () {
    'use strict';

    var CONDITION = { NO_AI: 1, SUGGESTION: 2, AGENT: 3, MULTILINE_SUGGESTION: 4 };
    var GHOST_DEBOUNCE_MS = 100;
    // The server answers within ~2 s; a suggestion still being written is asked for again this often.
    var PENDING_RETRIES = 6;
    // Text after the cursor on its line that still allows a suggestion (as in Copilot).
    var MID_LINE_OK = /^[\s)\]}"'`;,:]*$/;

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function lang() { return currentSession.programmingLanguage || 'javascript'; }
    function log(type, details) { try { logActivity(type, details || {}); } catch (e) { /* ignore */ } }


    // Every suggestion and agent change is offered as a pair (one correct, one with a single bug) in
    // random order. The browser only knows option ids; the server records which is which, and gets
    // "shown" / "accepted" events per option id here. With the quiz's "Correct wrong picks" or "Show
    // correct answer" on, the reply to accepting the buggy option carries `feedback`: the correct code,
    // why the pick was wrong, and `mode` ("correct": switch to the correct code; "reveal": only show it).
    var reportedShown = new Set();
    function pairEvent(pairId, oid, event) {
        if (!pairId || !oid) return Promise.resolve(null);
        if (event === 'shown') {
            if (reportedShown.has(pairId + ':' + oid)) return Promise.resolve(null);
            reportedShown.add(pairId + ':' + oid);
        }
        return fetch('/pair-event', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: currentSession.sessionId, pair_id: pairId, oid: oid, event: event })
        }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
    }

    // The lines that differ between the buggy and the correct code (common lines around them dropped),
    // without their shared indentation. `lead`: the indentation before the code's first line, which an
    // inline suggestion leaves out when it starts on an already indented line.
    function fixDiffHtml(buggy, correct, lead) {
        var a = ((lead || '') + String(buggy || '')).split('\n'), b = ((lead || '') + String(correct || '')).split('\n');
        while (a.length > 1 && b.length > 1 && a[0] === b[0]) { a.shift(); b.shift(); }
        while (a.length > 1 && b.length > 1 && a[a.length - 1] === b[b.length - 1]) { a.pop(); b.pop(); }
        var indent = Math.min.apply(null, a.concat(b).filter(function (l) { return l.trim(); })
            .map(function (l) { return l.match(/^[ \t]*/)[0].length; }).concat([1e9]));
        if (indent < 1e9) {
            a = a.map(function (l) { return l.slice(Math.min(indent, l.match(/^[ \t]*/)[0].length)); });
            b = b.map(function (l) { return l.slice(Math.min(indent, l.match(/^[ \t]*/)[0].length)); });
        }
        return a.map(function (l) { return '<div class="fix-diff__line is-del"><span>−</span><code>' + esc(l) + '</code></div>'; }).join('') +
            b.map(function (l) { return '<div class="fix-diff__line is-add"><span>+</span><code>' + esc(l) + '</code></div>'; }).join('');
    }
    var FIX_FALLBACK = 'This option would not solve the problem correctly.';
    // Text from the model with `code` spans.
    function richText(t) { return esc(t).replace(/`([^`\n]+)`/g, '<code>$1</code>'); }
    var LESSON_PARTS = [['what', 'What went wrong'], ['example', 'Try it on an input'], ['tip', 'How to catch it next time']];
    // The explanation of a wrong pick: its bug type (one of the quiz's bug skills), then the lesson the
    // bug writer gave (what went wrong, an example input, a tip), or else the classifier's explanation.
    function fixWhyHtml(fb) {
        var type = fb.bug_type ? '<div class="fix-why__type">Bug type <span>' + esc(fb.bug_type) + '</span></div>' : '';
        var lesson = fb.lesson || {};
        if (!lesson.what) return type + '<div>' + richText(fb.explanation || FIX_FALLBACK) + '</div>';
        return type + LESSON_PARTS.filter(function (p) { return lesson[p[0]]; }).map(function (p) {
            return '<div class="fix-lesson"><div class="fix-lesson__label">' + p[1] + '</div><div>' + richText(lesson[p[0]]) + '</div></div>';
        }).join('');
    }
    // "Correct wrong picks": the card shows the code, and the explanation (`why`, HTML) stays behind a
    // button until the student asks for it. "Show correct answer" (fb.mode "reveal") shows it at once.
    function fixWhyBlock(fb, why) {
        if (fb.mode === 'reveal') return why;
        return '<button type="button" class="fix-why-btn" aria-expanded="false">Show explanation</button>' +
            '<div class="fix-why-body" hidden>' + why + '</div>';
    }
    // The card explaining an agent version that had a bug: `head` and `done` (HTML) are its first and last line.
    function agentFixCard(fb, pairId, head, done) {
        var card = document.createElement('div');
        card.className = 'agent-fix';
        card.setAttribute('role', 'alert');
        card.innerHTML =
            '<div class="agent-fix__head"><span class="agent-fix__icon" aria-hidden="true">!</span>' + esc(head) + '</div>' +
            '<div class="fix-diff"><div class="fix-diff__file">' + SVG.file + esc(fb.filename) + '</div>' +
            fixDiffHtml(fb.buggy, fb.correct) + '</div>' +
            fixWhyBlock(fb, '<div class="agent-fix__why">' + fixWhyHtml(fb) + '</div>' +
                (fb.description_error ? '<p class="agent-fix__why"><strong>Its description was also wrong:</strong> ' +
                    esc(fb.description_error) + '</p>' : '')) +
            '<div class="agent-fix__done">' + done + '</div>';
        wireFixWhy(card, { pair_id: pairId }, null);
        return card;
    }
    function wireFixWhy(root, details, onToggle) {
        var btn = root.querySelector('.fix-why-btn');
        if (!btn) return;
        btn.onclick = function () {
            var body = btn.nextElementSibling, open = body.hidden;
            body.hidden = !open;
            btn.setAttribute('aria-expanded', String(open));
            btn.textContent = open ? 'Hide explanation' : 'Show explanation';
            if (open) {
                log('wrong_pick_explanation_opened', details);
                if (window.StudyLog && details && details.pair_id) StudyLog.explanationOpened(details.pair_id);
            }
            if (onToggle) onToggle();
        };
    }

    // Conditions 2 and 4: shown when the student accepted the buggy suggestion, and it was switched to
    // the correct one (or, with fb.mode "reveal", only shown). The diff is drawn in the editor itself:
    // the lines in the file are highlighted (green: the correct code, or red with "reveal": the buggy
    // code still there) and the other version's lines sit in a view zone next to them. The explanation
    // and bug type are in a card beside those lines. Without a range in the open file (the code was
    // edited since), the card is in the corner and carries the diff itself. It stays until dismissed,
    // and no new suggestion appears meanwhile.
    function fixNotice(monaco, editor) {
        var el = null, onClose = null, zoneId = null, decorations = [], subs = [], widget = null;
        function close() {
            if (!el) return;
            var gone = el;
            el = null;
            if (zoneId !== null) {
                var id = zoneId;
                editor.changeViewZones(function (acc) { acc.removeZone(id); });
                zoneId = null;
            }
            decorations = editor.deltaDecorations(decorations, []);
            subs.forEach(function (d) { d.dispose(); });
            subs = [];
            var goneWidget = widget;
            widget = null;
            gone.classList.add('is-out');
            setTimeout(function () {
                if (goneWidget) editor.removeOverlayWidget(goneWidget);
                gone.remove();
            }, 160);
            if (onClose) onClose();
            editor.focus();
        }
        // The full lines of the buggy and the correct version at `range` (the one in the file is
        // fb.correct, or fb.buggy with "reveal"), without the lines both share.
        function changedLines(model, range, fb) {
            var before = model.getLineContent(range.startLineNumber).slice(0, range.startColumn - 1);
            var after = model.getLineContent(range.endLineNumber).slice(range.endColumn - 1);
            var del = (before + fb.buggy + after).split('\n'), add = (before + fb.correct + after).split('\n');
            var top = 0;
            while (del.length > 1 && add.length > 1 && del[0] === add[0]) { del.shift(); add.shift(); top++; }
            while (del.length > 1 && add.length > 1 && del[del.length - 1] === add[add.length - 1]) { del.pop(); add.pop(); }
            return { first: range.startLineNumber + top, del: del, add: add };
        }
        function showInline(model, range, fb) {
            var reveal = fb.mode === 'reveal', d = changedLines(model, range, fb);
            var inFile = reveal ? d.del : d.add, other = reveal ? d.add : d.del;
            var last = d.first + inFile.length - 1;
            decorations = editor.deltaDecorations(decorations, [{
                range: new monaco.Range(d.first, 1, last, 1),
                options: { isWholeLine: true, className: reveal ? 'fix-line is-del' : 'fix-line is-add',
                           linesDecorationsClassName: reveal ? 'fix-gutter is-del' : 'fix-gutter is-add' }
            }]);
            var font = editor.getOption(monaco.editor.EditorOption.fontInfo);
            var lineHeight = editor.getOption(monaco.editor.EditorOption.lineHeight);
            var zone = document.createElement('div'), margin = document.createElement('div');
            zone.className = 'fix-zone ' + (reveal ? 'is-add' : 'is-del');
            zone.style.cssText = 'font-family:' + font.fontFamily + ';font-size:' + font.fontSize + 'px;line-height:' +
                lineHeight + 'px;tab-size:' + model.getOptions().tabSize + ';';
            zone.innerHTML = other.map(function (l) { return '<div class="fix-zone__line">' + (esc(l) || ' ') + '</div>'; }).join('');
            margin.className = 'fix-zone__margin ' + (reveal ? 'is-add' : 'is-del');
            margin.style.lineHeight = lineHeight + 'px';
            margin.innerHTML = other.map(function () { return '<div>' + (reveal ? '+' : '−') + '</div>'; }).join('');
            // Buggy lines above the correct ones, like a diff.
            var afterLine = reveal ? last : d.first - 1;
            editor.changeViewZones(function (acc) {
                zoneId = acc.addZone({ afterLineNumber: afterLine, heightInLines: other.length, domNode: zone, marginDomNode: margin });
            });
            editor.revealLinesInCenterIfOutsideViewport(Math.max(1, d.first - 1), last + other.length);
            // The card follows the changed lines (from the top of the diff) as the editor scrolls.
            var above = reveal ? 0 : other.length * lineHeight;
            function place() {
                if (!el) return;
                var layout = editor.getLayoutInfo();
                var top = editor.getTopForLineNumber(d.first) - above - editor.getScrollTop();
                top = Math.max(8, Math.min(top, layout.height - el.offsetHeight - 8));
                el.style.top = top + 'px';
                el.style.right = (layout.verticalScrollbarWidth + layout.minimap.minimapWidth + 12) + 'px';
            }
            subs.push(editor.onDidScrollChange(place), editor.onDidLayoutChange(place),
                      editor.onDidChangeModel(close));
            return place;
        }
        return {
            isOpen: function () { return !!el; },
            // range: where fb's code is in the open file (null: not found), replaced: it was switched.
            show: function (fb, model, range, replaced, filename, lead, closed) {
                if (el) close();
                onClose = closed;
                var reveal = fb.mode === 'reveal';
                var inline = !!(range && model === editor.getModel());
                el = document.createElement('div');
                el.className = 'fix-notice' + (inline ? ' is-beside' : '');
                el.setAttribute('role', 'alertdialog');
                el.setAttribute('aria-labelledby', 'fixNoticeTitle');
                el.setAttribute('aria-describedby', 'fixNoticeWhy');
                el.innerHTML =
                    '<div class="fix-notice__head"><span class="fix-notice__icon" aria-hidden="true">!</span>' +
                    '<span class="fix-notice__title" id="fixNoticeTitle">The suggestion you accepted had a bug</span>' +
                    '<button type="button" class="fix-notice__close" aria-label="Dismiss">' + SVG.close + '</button></div>' +
                    fixWhyBlock(fb, '<div class="fix-notice__why" id="fixNoticeWhy">' + fixWhyHtml(fb) + '</div>') +
                    (inline ? '' : '<div class="fix-diff">' + fixDiffHtml(fb.buggy, fb.correct, lead) + '</div>') +
                    '<div class="fix-notice__foot"><span class="fix-notice__done' + (replaced || reveal ? '' : ' is-manual') + '">' +
                    (reveal ? 'Your code was not changed.'
                     : replaced ? SVG.check + 'Replaced with the correct code in ' + esc(filename)
                     : 'Could not replace it automatically: please change it as shown.') + '</span>' +
                    '<button type="button" class="fix-notice__ok">Got it</button></div>';
                el.querySelector('.fix-notice__close').onclick = close;
                el.querySelector('.fix-notice__ok').onclick = close;
                el.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
                var place = null;
                wireFixWhy(el, { pair_id: fb.pair_id || '' }, function () { if (place) place(); });
                if (inline) {
                    place = showInline(model, range, fb);
                    var node = el;
                    widget = { getId: function () { return 'fix-notice'; }, getDomNode: function () { return node; },
                               getPosition: function () { return null; } };
                    editor.addOverlayWidget(widget);
                    place();
                } else {
                    editor.getContainerDomNode().parentNode.appendChild(el);
                }
                el.querySelector('.fix-notice__ok').focus();
            }
        };
    }

    // ── Conditions 2 and 4: inline ghost-text completions ─────────────────────
    // All options of the pair are returned as inline items: Monaco shows one as ghost text,
    // Alt+] / Alt+[ switches, Tab accepts the one on screen. Monaco's own toolbar sits on top of
    // the line above, so it is off; instead the ghost text is colored per option and a strip
    // below the line (a view zone, which pushes code down rather than covering it) shows the
    // other option.
    function initGhostText(monaco, editor) {
        // Monaco 0.43 has no showToolbar: 'never'. 'onHover' would open a hover box on the ghost text;
        // 'always' puts the toolbar in its own widget, which GHOST_STYLES hides.
        editor.updateOptions({ inlineSuggest: { enabled: true, showToolbar: 'always' } });
        var style = document.createElement('style');
        style.textContent = GHOST_STYLES;
        document.head.appendChild(style);
        var strip = pairStrip(monaco, editor);
        var ghostLog = suggestionLog(monaco, editor);
        // Option/Alt + Left/Right switches between the options, only while a suggestion is on screen
        // (otherwise the keys keep their usual word-jump meaning).
        ['showNext', 'showPrevious'].forEach(function (action) {
            var key = action === 'showNext' ? monaco.KeyCode.RightArrow : monaco.KeyCode.LeftArrow;
            editor.addCommand(monaco.KeyMod.Alt | key, function () {
                editor.trigger('pair-keys', 'editor.action.inlineSuggest.' + action, {});
            }, 'inlineSuggestionVisible');
        });
        var status = suggestStatus();
        var notice = fixNotice(monaco, editor);
        var cache = new Map();
        var recent = null; // the last suggestion shown: { prefix, suffix, hit }
        var acceptCmd = editor.addCommand(0, function (_ctx, meta) {
            var via = strip.takeClickAccept() ? 'click' : 'tab';
            strip.hide();
            recent = null;
            log('inline_suggestion_accepted', Object.assign({}, meta, { via: via }));
            // Track the accepted code (it ends at the cursor) in case the server says it was the buggy option.
            var model = editor.getModel(), end = model.getOffsetAt(editor.getPosition());
            var start = Math.max(0, end - meta.typed_through - meta.chars);
            ghostLog.accepted(meta, via, model, start, end);
            var mark = model.deltaDecorations([], [{
                range: monaco.Range.fromPositions(model.getPositionAt(start), model.getPositionAt(end)),
                options: { stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges }
            }]);
            pairEvent(meta.pair_id, meta.option_id, 'accepted').then(function (res) {
                if (model.isDisposed()) return;
                var range = model.getDecorationRange(mark[0]);
                model.deltaDecorations(mark, []);
                if (res && res.feedback) correctPick(model, range, res.feedback, meta);
            });
        }, '');

        // The student accepted the buggy option: put the correct one in its place (unless fb.mode is
        // "reveal") and explain why.
        function correctPick(model, range, fb, meta) {
            var reveal = fb.mode === 'reveal';
            if (!range || model.getValueInRange(range) !== fb.buggy) range = nearest(model, fb.buggy, range);
            var lead = '';
            if (range) {
                lead = model.getLineContent(range.startLineNumber).slice(0, range.startColumn - 1);
                if (lead.trim()) lead = '';
            }
            var replaced = false;
            if (range && !reveal) {
                var cursor = editor.getModel() === model ? editor.getPosition() : null;
                var cursorAtEnd = cursor && cursor.equals(range.getEndPosition());
                var start = model.getOffsetAt(range.getStartPosition());
                model.pushStackElement();
                model.pushEditOperations([], [{ range: range, text: fb.correct }], function () { return null; });
                model.pushStackElement();
                if (cursorAtEnd) editor.setPosition(model.getPositionAt(start + fb.correct.length));
                // Where the correct code now is, for the diff drawn in the editor.
                range = monaco.Range.fromPositions(model.getPositionAt(start), model.getPositionAt(start + fb.correct.length));
                replaced = true;
            }
            editor.trigger('fix-notice', 'editor.action.inlineSuggest.hide', {});
            ghostLog.feedback(meta, reveal ? 'revealed' : 'corrected', replaced ? range : null, fb.correct);
            log(reveal ? 'wrong_pick_revealed' : 'wrong_pick_corrected', {
                pair_id: meta.pair_id, option_id: meta.option_id, correct_option_id: fb.correct_oid,
                file: meta.file, replaced: replaced, bug_type: fb.bug_type || '', explanation: (fb.explanation || '').slice(0, 1000) });
            fb.pair_id = meta.pair_id;
            notice.show(fb, model, range, replaced, meta.file, lead, function () {
                log('wrong_pick_notice_closed', { pair_id: meta.pair_id });
            });
        }

        // The occurrence of `text` closest to where it was accepted (the student may have typed since).
        function nearest(model, text, near) {
            if (!text) return null;
            var all = model.getValue(), at = near ? model.getOffsetAt(near.getStartPosition()) : 0, best = -1;
            for (var i = all.indexOf(text); i >= 0; i = all.indexOf(text, i + 1)) {
                if (best < 0 || Math.abs(i - at) < Math.abs(best - at)) best = i;
            }
            return best < 0 ? null : monaco.Range.fromPositions(model.getPositionAt(best), model.getPositionAt(best + text.length));
        }

        // The rest of the last suggestion's options after the student typed their start (same
        // pair and option ids), or null. Once the typed text no longer matches every option, the
        // server is asked again, so a suggestion always shows all its options. Closing
        // brackets/quotes the editor auto-inserted after the cursor are replaced by the suggestion.
        function typeThrough(prefix, suffix) {
            if (!recent || prefix.length <= recent.prefix.length ||
                prefix.slice(0, recent.prefix.length) !== recent.prefix) return null;
            if (suffix.length < recent.suffix.length || suffix.slice(suffix.length - recent.suffix.length) !== recent.suffix) return null;
            var autoClosed = suffix.slice(0, suffix.length - recent.suffix.length);
            if (!/^[)\]}"'`]*$/.test(autoClosed)) return null;
            var typed = prefix.slice(recent.prefix.length);
            var options = recent.hit.options
                .filter(function (o) { return o.text.length > typed.length && o.text.slice(0, typed.length) === typed; })
                .map(function (o) { return { oid: o.oid, text: o.text.slice(typed.length) }; });
            if (!options.length || options.length < recent.hit.options.length) return null;
            return { id: recent.hit.id, options: options, latency_ms: 0, typed_through: typed.length,
                     replace: autoClosed.length };
        }

        function toItems(hit, position) {
            var range = new monaco.Range(position.lineNumber, position.column, position.lineNumber,
                                         position.column + (hit.replace || 0));
            return hit.options.map(function (o, i) {
                var meta = {
                    suggestion_id: hit.id,
                    pair_id: hit.id,
                    option_id: o.oid,
                    option_index: i + 1,
                    option_count: hit.options.length,
                    file: activeFileName(),
                    line: position.lineNumber,
                    lines: o.text.split('\n').length,
                    chars: o.text.length,
                    text: o.text.slice(0, 2000),
                    latency_ms: hit.latency_ms,
                    typed_through: hit.typed_through || 0
                };
                return {
                    insertText: o.text,
                    range: range,
                    command: { id: acceptCmd, title: 'Accept suggestion', arguments: [meta] },
                    _meta: meta
                };
            });
        }

        function cacheKey(prefix, suffix) { return prefix.slice(-3000) + '\u0000' + suffix.slice(0, 500); }
        function requestBody(model, prefix, suffix) {
            return JSON.stringify({
                session_id: currentSession.sessionId,
                language: model.getLanguageId() || lang(),
                filename: activeFileName(),
                prefix: prefix,
                suffix: suffix
            });
        }
        function cursorText() {
            var model = editor.getModel(), position = editor.getPosition();
            if (!model || !position) return null;
            var last = model.getLineCount();
            return {
                model: model,
                prefix: model.getValueInRange(new monaco.Range(1, 1, position.lineNumber, position.column)),
                suffix: model.getValueInRange(new monaco.Range(position.lineNumber, position.column, last, model.getLineMaxColumn(last)))
            };
        }

        // A reply of "pending" (the suggestion takes longer than the server waits): trigger the
        // suggestion again while the cursor stays at that spot, up to PENDING_RETRIES times.
        var pendingRetry = null; // { key, tries, started: when this spot was first asked for }
        function retryPending(key, req, started) {
            var same = pendingRetry && pendingRetry.key === key;
            var tries = same ? pendingRetry.tries + 1 : 1;
            if (tries > PENDING_RETRIES) { pendingRetry = null; status.finish(req, 0); return; }
            pendingRetry = { key: key, tries: tries, started: same ? pendingRetry.started : started };
            setTimeout(function () {
                var at = cursorText();
                if (!pendingRetry || pendingRetry.key !== key || !at || cacheKey(at.prefix, at.suffix) !== key) return;
                editor.trigger('pending-retry', 'editor.action.inlineSuggest.trigger', {});
            }, 150);
        }

        // Has the server write the program and first suggestions for the cursor before the student
        // asks: when the editor opens, when another file is opened, and after a click somewhere new.
        var warmTimer = null;
        function warmUp() {
            var at = cursorText();
            if (!at || !at.prefix.trim() || editor.getOption(monaco.editor.EditorOption.readOnly)) return;
            if (!MID_LINE_OK.test(at.suffix.split('\n', 1)[0]) || cache.has(cacheKey(at.prefix, at.suffix))) return;
            if (typeof currentSession === 'undefined' || !currentSession || !currentSession.sessionId) return;
            fetch('/complete/warm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: requestBody(at.model, at.prefix, at.suffix) }).catch(function () {});
        }
        function warmSoon() { clearTimeout(warmTimer); warmTimer = setTimeout(warmUp, 400); }
        editor.onDidChangeCursorPosition(function (e) { if (e.source === 'mouse') warmSoon(); });
        editor.onDidChangeModel(warmSoon);
        warmSoon();

        // Every language: the open file's language can differ from the session's (main.c in a
        // Python session), and a provider registered for one language is never asked about others.
        monaco.languages.registerInlineCompletionsProvider('*', {
            provideInlineCompletions: async function (model, position, context, token) {
                if (editor.getOption(monaco.editor.EditorOption.readOnly)) return { items: [] };
                // A corrected wrong pick is being explained: no new suggestion until it is dismissed.
                if (notice.isOpen()) return { items: [] };
                var last = model.getLineCount();
                var prefix = model.getValueInRange(new monaco.Range(1, 1, position.lineNumber, position.column));
                var suffix = model.getValueInRange(new monaco.Range(position.lineNumber, position.column, last, model.getLineMaxColumn(last)));
                if (!prefix.trim()) return { items: [] };
                // Like Copilot: no suggestion in the middle of a line, unless only closing
                // brackets, quotes or punctuation follow the cursor.
                if (!MID_LINE_OK.test(suffix.split('\n', 1)[0])) return { items: [] };

                // Like Copilot: while the student types the start of a suggestion, keep showing the
                // rest of it at once instead of asking the server again.
                var through = typeThrough(prefix, suffix);
                if (through) return { items: toItems(through, position) };

                var key = cacheKey(prefix, suffix);
                var hit = cache.get(key);
                if (!hit) {
                    if (context.triggerKind === monaco.languages.InlineCompletionTriggerKind.Automatic) {
                        await sleep(GHOST_DEBOUNCE_MS);
                    }
                    if (token.isCancellationRequested) return { items: [] };
                    var ctrl = new AbortController();
                    token.onCancellationRequested(function () { ctrl.abort(); });
                    var started = Date.now();
                    var req = status.start();
                    try {
                        var res = await fetch('/complete', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            signal: ctrl.signal,
                            body: requestBody(model, prefix, suffix)
                        });
                        if (!res.ok) { status.finish(req, -1); return { items: [] }; }
                        var data = await res.json();
                        if (data.pending) {
                            // Still being written: ask again for this spot (the spinner keeps going).
                            retryPending(key, req, started);
                            return { items: [] };
                        }
                        // The time to generate counts from the first request at this spot (before any retries).
                        var firstAsked = pendingRetry && pendingRetry.key === key ? pendingRetry.started : started;
                        pendingRetry = null;
                        hit = { id: data.id || '', options: (data.options || []).filter(function (o) { return o.text; }),
                                latency_ms: Date.now() - firstAsked };
                    } catch (e) {
                        // Aborted because the student kept typing: not a failure, the next request takes over.
                        status.finish(req, ctrl.signal.aborted ? null : -1);
                        return { items: [] };
                    }
                    status.finish(req, hit.options.length);
                    cache.set(key, hit);
                    if (cache.size > 200) cache.delete(cache.keys().next().value);
                } else {
                    status.show(hit.options.length);
                }
                if (!hit.options.length || token.isCancellationRequested) return { items: [] };
                recent = { prefix: prefix, suffix: suffix, hit: hit };
                return { items: toItems(hit, position) };
            },
            handleItemDidShow: function (completions, item) {
                var meta = item && item._meta;
                strip.show(completions.items || [], item);
                ghostLog.shown(completions.items || [], item);
                if (meta && !reportedShown.has(meta.pair_id + ':' + meta.option_id)) {
                    log('inline_suggestion_shown', meta);
                    pairEvent(meta.pair_id, meta.option_id, 'shown');
                }
            },
            freeInlineCompletions: function () {}
        });
    }

    // Status chip in the Code card header: spins while a suggestion is generating, then green (two or
    // more options), yellow (only one) or red (none: the model offered nothing, or the request failed).
    // Only the newest request may change it, so a slow older reply never overwrites a newer state.
    function suggestStatus() {
        var el = document.getElementById('suggestStatus');
        var noop = { start: function () { return 0; }, finish: function () {}, show: function () {} };
        if (!el) return noop;
        el.hidden = false;
        var text = el.querySelector('.suggest-status__text');
        var seq = 0, settled = null;
        var STATES = {
            loading: ['is-loading', 'Suggesting…', 'Generating a code suggestion…'],
            ok: ['is-ok', '{n} options', '{n} options ready: compare them and press Tab to accept one.'],
            one: ['is-one', '1 option', 'Only one option was generated for this spot.'],
            none: ['is-none', 'No suggestion', 'No suggestion for this spot. Keep typing, or start a new line to try again.'],
            error: ['is-none', 'No suggestion', 'Suggestions are unavailable right now. Keep typing to try again.'],
            idle: ['', 'Suggestions on', 'Code suggestions appear as you type.']
        };
        var count = 0;
        function set(name) {
            var s = STATES[name];
            el.className = 'suggest-status' + (s[0] ? ' ' + s[0] : '');
            text.textContent = s[1].replace('{n}', count);
            el.title = s[2].replace('{n}', count);
        }
        function fromCount(n) { count = n; return n < 0 ? 'error' : n >= 2 ? 'ok' : n === 1 ? 'one' : 'none'; }
        set('idle');
        return {
            start: function () { set('loading'); return ++seq; },
            // count: options received, -1 on failure, null when cancelled (keep the last settled state).
            finish: function (req, count) {
                if (req !== seq) return;
                if (count === null) { set(settled || 'idle'); return; }
                settled = fromCount(count);
                set(settled);
            },
            show: function (count) { seq++; settled = fromCount(count); set(settled); }
        };
    }

    var PAIR_LETTERS = ['A', 'B', 'C', 'D'];

    // Study log rows for inline suggestions (study_log.js, table "autocomplete"): one row per option of
    // a suggestion, made when it first appears. It records which options were put in the editor and for
    // how long, how the suggestion ended (accepted with Tab or a click, the other options not chosen;
    // Esc: rejected; typed on or moved away: ignored), the wrong-pick feedback, and at the end of the step
    // whether the accepted code was edited and is still in the code.
    function suggestionLog(monaco, editor) {
        var root = editor.getDomNode();
        var set = null;      // the suggestion on screen: { id, shownAt, oids, cur, since, watch }
        var esc = false;     // Esc was pressed while it was on screen
        var tracked = {};    // row id -> { model, deco, text }: accepted code, checked at the end of the step
        function rowId(pairId, oid) { return 'ac_' + pairId + '_' + oid; }
        function ghostVisible() {
            return !!root.querySelector('.ghost-text-decoration, .ghost-text-decoration-preview, .ghost-text');
        }
        // Adds the time the current option has been in the editor to its row.
        function account() {
            if (!set || !set.cur || !window.StudyLog) return;
            var row = StudyLog.get(rowId(set.id, set.cur));
            if (row) StudyLog.upsert('autocomplete', row.row_id, { view_time_ms: (row.view_time_ms || 0) + (Date.now() - set.since) });
            set.since = Date.now();
        }
        function close(status, acceptedOid, via) {
            if (!set || !window.StudyLog) return;
            account();
            clearInterval(set.watch);
            var elapsed = Date.now() - set.shownAt;
            set.oids.forEach(function (oid) {
                var fields = { time_to_decision_ms: elapsed };
                if (acceptedOid) {
                    fields.suggestion_status = oid === acceptedOid ? 'accepted' : 'not_chosen';
                    if (oid === acceptedOid) { fields.accept_method = via; fields.time_to_accept_ms = elapsed; }
                } else {
                    fields.suggestion_status = status;
                }
                StudyLog.upsert('autocomplete', rowId(set.id, oid), fields);
            });
            set = null;
            esc = false;
        }
        editor.onKeyDown(function (e) { if (set && e.keyCode === monaco.KeyCode.Escape) esc = true; });
        if (window.StudyLog) StudyLog.addFinalizer(function () {
            close(esc ? 'rejected' : 'ignored');
            var code = editor.getModel() ? editor.getModel().getValue() : '';
            var squash = function (t) { return String(t || '').replace(/\s+/g, ''); };
            Object.keys(tracked).forEach(function (id) {
                var t = tracked[id];
                if (!t.model || t.model.isDisposed()) return;
                var range = t.model.getDecorationRange(t.deco);
                var now = range ? t.model.getValueInRange(range) : '';
                var finalCode = t.model === editor.getModel() ? code : t.model.getValue();
                StudyLog.upsert('autocomplete', id, {
                    corrected_code: squash(now) === squash(t.text) ? 'No' : 'Yes',
                    code_in_final: squash(t.text) && squash(finalCode).indexOf(squash(t.text)) >= 0 ? 'Yes' : 'No'
                });
            });
        });
        return {
            shown: function (items, active) {
                var meta = active && active._meta;
                if (!window.StudyLog || !meta) return;
                if (!set || set.id !== meta.pair_id) {
                    close(esc ? 'rejected' : 'ignored');
                    var model = editor.getModel(), pos = editor.getPosition(), idx = StudyLog.nextIndex();
                    set = { id: meta.pair_id, shownAt: Date.now(), cur: null, since: Date.now(),
                            oids: items.map(function (it) { return it._meta.option_id; }) };
                    set.watch = setInterval(function () { if (!ghostVisible()) close(esc ? 'rejected' : 'ignored'); }, 250);
                    items.forEach(function (it, i) {
                        var m = it._meta, id = rowId(m.pair_id, m.option_id), row = StudyLog.get(id);
                        if (row) {
                            StudyLog.upsert('autocomplete', id, { times_shown: (row.times_shown || 1) + 1, suggestion_status: 'pending' });
                            return;
                        }
                        StudyLog.upsert('autocomplete', id, {
                            interaction_index: idx, suggestion_set_id: m.pair_id, suggestion_id: m.option_id,
                            option_label: PAIR_LETTERS[i] || String(i + 1), options_in_set: items.length,
                            suggestion_code: String(it.insertText || ''), suggestion_lines: String(it.insertText || '').replace(/^\n/, '').split('\n').length,
                            generation_latency_ms: m.latency_ms, times_shown: 1, previewed: 'No', preview_count: 0,
                            view_time_ms: 0, suggestion_status: 'pending', feedback_shown: '', explanation_clicked: 'No',
                            editor_snapshot: model ? model.getValue().slice(0, 20000) : '', file: m.file,
                            cursor_offset: model && pos ? model.getOffsetAt(pos) : '',
                            suggestion_position: pos ? 'Line ' + pos.lineNumber + ', Col ' + pos.column : ''
                        });
                    });
                }
                if (set.cur !== meta.option_id) {
                    account();
                    set.cur = meta.option_id;
                    var row = StudyLog.get(rowId(set.id, meta.option_id));
                    if (row) StudyLog.upsert('autocomplete', row.row_id, { previewed: 'Yes', preview_count: (row.preview_count || 0) + 1 });
                }
            },
            accepted: function (meta, via, model, start, end) {
                if (!window.StudyLog) return;
                if (set && set.id === meta.pair_id) {
                    close('', meta.option_id, via);
                } else {
                    // Already closed (the ghost text went away just before the accept arrived).
                    StudyLog.rows(function (r) { return r.suggestion_set_id === meta.pair_id; }).forEach(function (r) {
                        var mine = r.suggestion_id === meta.option_id;
                        StudyLog.upsert('autocomplete', r.row_id, mine
                            ? { suggestion_status: 'accepted', accept_method: via, time_to_accept_ms: r.time_to_decision_ms }
                            : { suggestion_status: 'not_chosen' });
                    });
                }
                var id = rowId(meta.pair_id, meta.option_id);
                var range = monaco.Range.fromPositions(model.getPositionAt(start), model.getPositionAt(end));
                tracked[id] = { model: model, text: model.getValueInRange(range), deco: model.deltaDecorations([], [{
                    range: range, options: { stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges } }])[0] };
                StudyLog.setLastAccepted(id);
            },
            // The accepted option was the buggy one: the correct code replaced it ("corrected", then
            // `range` is where it is now) or was only shown ("revealed").
            feedback: function (meta, mode, range, correct) {
                if (!window.StudyLog) return;
                var id = rowId(meta.pair_id, meta.option_id), t = tracked[id];
                StudyLog.upsert('autocomplete', id, { feedback_shown: mode });
                if (t && range) {
                    t.deco = t.model.deltaDecorations([t.deco], [{ range: range,
                        options: { stickiness: monaco.editor.TrackedRangeStickiness.NeverGrowsWhenTypingAtEdges } }])[0];
                    t.text = correct;
                }
            }
        };
    }
    var STRIP_MAX_LINES = 8;
    var ALT_KEY = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌥ Option' : 'Alt';

    // The options box: pinned to the editor's right edge, level with the suggestion's line, listing
    // every option so they can be compared side by side. (Below the line, the other option looked
    // like the next lines of code.)
    function pairStrip(monaco, editor) {
        var root = editor.getDomNode();
        var widget = null, timer = null, listeners = [];
        // Option id a click asked to accept, until that option is the one in the editor.
        var pendingAccept = null, clickedAt = 0;
        var hoverTimer = null; // a hovered option about to be put in the editor
        // Hovering an option card shows it in the editor after this delay (passing over a card does not).
        var HOVER_PREVIEW_MS = 120;
        // Where the participant dragged the box (offset from its default spot) and the size they gave
        // it; kept for later suggestions and reloads. Double-clicking the header puts it back.
        var LAYOUT_KEY = 'pairBoxLayout';
        var layout = {};
        try { layout = JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}') || {}; } catch (e) { layout = {}; }
        function saveLayout() {
            try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { /* ignore */ }
        }

        function ghostVisible() {
            return !!root.querySelector('.ghost-text-decoration, .ghost-text-decoration-preview, .ghost-text');
        }
        function hide() {
            if (timer) { clearInterval(timer); timer = null; }
            clearTimeout(hoverTimer);
            listeners.forEach(function (l) { l.dispose(); });
            listeners = [];
            root.classList.remove('pair-opt-1', 'pair-opt-2', 'pair-opt-3', 'pair-opt-4');
            if (widget) {
                editor.removeOverlayWidget(widget);
                widget = null;
            }
        }
        function switchTo(steps) {
            editor.focus();
            for (var i = 0; i < steps; i++) editor.trigger('pair-strip', 'editor.action.inlineSuggest.showNext', {});
        }
        // Accepts the option in the editor as Tab does (its command logs and reports the pick).
        function commit() {
            pendingAccept = null;
            clickedAt = Date.now();
            editor.focus();
            editor.trigger('pair-strip', 'editor.action.inlineSuggest.commit', {});
        }
        function show(items, active) {
            hide();
            var meta = active && active._meta;
            if (!meta || items.length < 2) return;
            var idx = items.indexOf(active);
            // A clicked option is accepted once switching has put it in the editor.
            if (pendingAccept && pendingAccept.id === meta.option_id) { pendingAccept = null; setTimeout(commit, 0); return; }
            root.classList.add('pair-opt-' + (idx + 1));

            var node = document.createElement('div');
            node.className = 'pair-strip';
            var chips = items.map(function (it, i) {
                return '<button type="button" class="pair-chip pair-chip-' + (i + 1) + (i === idx ? ' active' : '') +
                    '" data-steps="' + ((i - idx + items.length) % items.length) + '">' + PAIR_LETTERS[i] + '</button>';
            }).join('');
            // On a blank line the editor already holds the first line's indentation: put it back, then
            // drop the indentation all lines share, so each option reads as a block.
            var pos = editor.getPosition();
            var head = pos ? editor.getModel().getLineContent(pos.lineNumber).slice(0, pos.column - 1) : '';
            var indent = /^\s*$/.test(head) ? head : '';
            var options = items.map(function (it, i) {
                var lines = (indent + String(it.insertText || '').replace(/^\n/, '')).split('\n');
                var common = Math.min.apply(null, lines.filter(function (l) { return l.trim(); })
                    .map(function (l) { return l.match(/^\s*/)[0].length; }).concat([indent.length]));
                lines = lines.map(function (l) { return l.slice(Math.min(common, l.match(/^\s*/)[0].length)); });
                var more = lines.length > STRIP_MAX_LINES ? '\n…' : '';
                return '<div class="pair-alt pair-alt-' + (i + 1) + (i === idx ? ' is-active' : '') +
                    '" data-steps="' + ((i - idx + items.length) % items.length) + '" data-oid="' + esc(String(it._meta.option_id)) + '">' +
                    '<div class="pair-alt-label">Option ' + PAIR_LETTERS[i] +
                    (i === idx ? '<span class="pair-alt-tag">in editor</span>' : '') + '</div>' +
                    '<pre>' + esc(lines.slice(0, STRIP_MAX_LINES).join('\n') + more) + '</pre></div>';
            }).join('');
            node.innerHTML =
                '<div class="pair-head"><span class="pair-grip" aria-label="Drag to move">⠿</span>' + chips +
                '<span class="pair-hint"><span><kbd>' + ALT_KEY + '</kbd><kbd>←</kbd><kbd>→</kbd> switch ·</span>' +
                '<span>hover to preview ·</span><span><kbd>Tab</kbd> or click accept ·</span><span><kbd>Esc</kbd> dismiss</span></span></div>' +
                '<div class="pair-body">' + options + '</div>' +
                '<div class="pair-resize" aria-label="Drag to resize"></div>';
            // Clicks must not reach Monaco: a click in the editor moves the cursor and drops the suggestion.
            ['mousedown', 'pointerdown'].forEach(function (evt) {
                node.addEventListener(evt, function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    if (e.target.closest('.pair-resize')) { if (evt === 'pointerdown') startDrag(e, 'resize'); return; }
                    if (e.target.closest('.pair-head') && !e.target.closest('.pair-chip')) {
                        if (evt === 'pointerdown') startDrag(e, 'move');
                        return;
                    }
                    var target = e.target.closest && e.target.closest('[data-steps]');
                    if (!target) return;
                    var steps = Number(target.getAttribute('data-steps'));
                    // A chip only switches; an option card accepts that option.
                    if (!target.classList.contains('pair-alt')) { if (steps) switchTo(steps); return; }
                    if (!steps) { commit(); return; }
                    pendingAccept = { id: target.getAttribute('data-oid') };
                    var asked = pendingAccept;
                    setTimeout(function () { if (pendingAccept === asked) pendingAccept = null; }, 1500);
                    switchTo(steps);
                });
            });
            // Hovering an option card puts it in the editor as the ghost text, so it can be read in place.
            node.addEventListener('mouseover', function (e) {
                var card = e.target.closest && e.target.closest('.pair-alt');
                if (!card || node.classList.contains('pair-moving') || node.classList.contains('pair-resizing')) return;
                var steps = Number(card.getAttribute('data-steps'));
                clearTimeout(hoverTimer);
                if (steps) hoverTimer = setTimeout(function () { switchTo(steps); }, HOVER_PREVIEW_MS);
            });
            node.addEventListener('mouseleave', function () { clearTimeout(hoverTimer); });
            // Double-clicking the header puts the box back at its default spot and size.
            node.addEventListener('dblclick', function (e) {
                if (!e.target.closest('.pair-head') || e.target.closest('.pair-chip')) return;
                layout = {};
                saveLayout();
                place();
            });
            node.style.setProperty('--pair-font', editor.getOption(monaco.editor.EditorOption.fontInfo).fontFamily);
            node.style.setProperty('--pair-size', editor.getOption(monaco.editor.EditorOption.fontSize) + 'px');

            // Moving shifts the box from its spot at the editor's right edge; resizing keeps its
            // left edge where it is (the box is anchored on the right, so the offset absorbs the change).
            function startDrag(e, kind) {
                clearTimeout(hoverTimer);
                var sx = e.clientX, sy = e.clientY;
                var start = { dx: layout.dx || 0, dy: layout.dy || 0, w: node.offsetWidth, h: node.offsetHeight };
                if (kind === 'move' && layout.dx == null) start.dy = node.offsetTop;
                node.classList.add(kind === 'move' ? 'pair-moving' : 'pair-resizing');
                function onMove(ev) {
                    var mx = ev.clientX - sx, my = ev.clientY - sy;
                    if (kind === 'move') {
                        layout.dx = start.dx + mx;
                        layout.dy = start.dy + my;
                    } else {
                        if (layout.dx == null) { layout.dx = 0; layout.dy = node.offsetTop; }
                        var w = clampW(start.w + mx);
                        layout.w = w;
                        layout.h = clampH(start.h + my);
                        layout.dx = start.dx + (w - start.w);
                    }
                    place();
                }
                function onUp() {
                    window.removeEventListener('pointermove', onMove);
                    window.removeEventListener('pointerup', onUp);
                    node.classList.remove('pair-moving', 'pair-resizing');
                    saveLayout();
                }
                window.addEventListener('pointermove', onMove);
                window.addEventListener('pointerup', onUp);
            }
            function clampW(w) { return Math.max(220, Math.min(w, editor.getLayoutInfo().width - 16)); }
            function clampH(h) { return Math.max(90, Math.min(h, editor.getLayoutInfo().height - 8)); }

            function place() {
                var info = editor.getLayoutInfo();
                if (layout.w) {
                    node.style.maxWidth = 'none';
                    node.style.width = clampW(layout.w) + 'px';
                    node.style.height = clampH(layout.h) + 'px';
                } else {
                    node.style.width = node.style.height = '';
                    node.style.maxWidth = Math.max(220, Math.min(460, Math.round(info.contentWidth * 0.45))) + 'px';
                }
                node.style.maxHeight = (info.height - 8) + 'px';
                if (layout.dx == null) {
                    // Top of the suggestion's line, kept inside the editor.
                    node.style.transform = '';
                    var top = editor.getTopForLineNumber(meta.line) - editor.getScrollTop();
                    var room = info.height - node.offsetHeight - 8;
                    node.style.marginTop = Math.max(4, Math.min(top, room)) + 'px';
                    return;
                }
                // Where the participant put it, kept inside the editor.
                node.style.marginTop = '0px';
                node.style.transform = '';
                layout.dx = Math.max(-node.offsetLeft, Math.min(layout.dx, 0));
                layout.dy = Math.max(0, Math.min(layout.dy, info.height - node.offsetHeight));
                node.style.transform = 'translate(' + layout.dx + 'px,' + layout.dy + 'px)';
            }
            widget = {
                getId: function () { return 'pair.options.box'; },
                getDomNode: function () { return node; },
                getPosition: function () {
                    return { preference: monaco.editor.OverlayWidgetPositionPreference.TOP_RIGHT_CORNER };
                }
            };
            editor.addOverlayWidget(widget);
            place();
            listeners.push(editor.onDidScrollChange(place), editor.onDidLayoutChange(place));
            // Monaco has no "suggestion hidden" event: drop the box once the ghost text is gone.
            timer = setInterval(function () { if (!ghostVisible()) hide(); }, 200);
        }
        editor.onDidChangeModel(hide);
        return { show: show, hide: hide, takeClickAccept: function () {
            var clicked = Date.now() - clickedAt < 1000;
            clickedAt = 0;
            return clicked;
        } };
    }

    var GHOST_STYLES = [
        '.monaco-editor .inlineSuggestionsHints{display:none!important;}',
        '.pair-opt-1 .ghost-text-decoration,.pair-opt-1 .ghost-text-decoration-preview,.pair-opt-1 .suggest-preview-text .ghost-text,.pair-opt-1 .ghost-text{color:#6cb6ff!important;opacity:.9;}',
        '.pair-opt-2 .ghost-text-decoration,.pair-opt-2 .ghost-text-decoration-preview,.pair-opt-2 .suggest-preview-text .ghost-text,.pair-opt-2 .ghost-text{color:#e0a3ff!important;opacity:.9;}',
        '.pair-opt-3 .ghost-text-decoration,.pair-opt-3 .ghost-text-decoration-preview,.pair-opt-3 .suggest-preview-text .ghost-text,.pair-opt-3 .ghost-text{color:#7ee2b8!important;opacity:.9;}',
        '.pair-opt-4 .ghost-text-decoration,.pair-opt-4 .ghost-text-decoration-preview,.pair-opt-4 .suggest-preview-text .ghost-text,.pair-opt-4 .ghost-text{color:#ffc46b!important;opacity:.9;}',
        '.pair-strip{box-sizing:border-box;display:flex;flex-direction:column;min-width:220px;min-height:90px;margin-right:8px;padding:6px 8px 8px;border:1px solid #454a54;border-radius:8px;background:#252526;box-shadow:0 6px 18px rgba(0,0,0,.45);font:12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#9da5b4;pointer-events:auto;z-index:10;}',
        '.pair-head{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;flex:none;cursor:move;user-select:none;}',
        '.pair-grip{font-size:13px;line-height:18px;color:#6b7280;}',
        '.pair-head:hover .pair-grip,.pair-moving .pair-grip{color:#c5cad3;}',
        '.pair-moving,.pair-resizing{user-select:none;box-shadow:0 10px 28px rgba(0,0,0,.6);}',
        '.pair-body{flex:1 1 auto;min-height:0;overflow:auto;}',
        '.pair-resize{position:absolute;right:0;bottom:0;width:14px;height:14px;cursor:nwse-resize;border-bottom-right-radius:8px;background:linear-gradient(135deg,transparent 50%,#5c6370 50%,#5c6370 60%,transparent 60%,transparent 72%,#5c6370 72%,#5c6370 82%,transparent 82%);}',
        '.pair-chip{border:1px solid transparent;border-radius:4px;padding:0 7px;height:18px;font:600 11px/16px inherit;cursor:pointer;opacity:.55;}',
        '.pair-chip.active{opacity:1;box-shadow:0 0 0 1px rgba(255,255,255,.35);}',
        '.pair-chip-1{background:rgba(108,182,255,.2);color:#6cb6ff;border-color:rgba(108,182,255,.5);}',
        '.pair-chip-2{background:rgba(224,163,255,.2);color:#e0a3ff;border-color:rgba(224,163,255,.5);}',
        '.pair-chip-3{background:rgba(126,226,184,.2);color:#7ee2b8;border-color:rgba(126,226,184,.5);}',
        '.pair-chip-4{background:rgba(255,196,107,.2);color:#ffc46b;border-color:rgba(255,196,107,.5);}',
        '.pair-hint{display:flex;flex-wrap:wrap;gap:2px 4px;margin-left:2px;font-size:11px;}',
        '.pair-hint>span{white-space:nowrap;}',
        '.pair-hint kbd{font:inherit;font-size:10px;padding:0 4px;border:1px solid #555;border-radius:3px;background:#2a2a2a;color:#ccc;}',
        '.pair-hint kbd+kbd{margin-left:2px;}',
        '.pair-alt{margin-top:6px;padding:4px 8px;border-radius:6px;border:1px solid transparent;background:rgba(255,255,255,.03);cursor:pointer;}',
        '.pair-alt:hover{background:rgba(255,255,255,.08);}',
        '.pair-alt-1.is-active{border-color:rgba(108,182,255,.55);}',
        '.pair-alt-2.is-active{border-color:rgba(224,163,255,.55);}',
        '.pair-alt-3.is-active{border-color:rgba(126,226,184,.55);}',
        '.pair-alt-4.is-active{border-color:rgba(255,196,107,.55);}',
        '.pair-alt-label{font-weight:600;font-size:11px;line-height:18px;}',
        '.pair-alt-tag{margin-left:6px;font-weight:400;color:#9da5b4;}',
        '.pair-alt pre{margin:0;font-family:var(--pair-font);font-size:var(--pair-size);line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;}',
        '.pair-alt-1{color:#6cb6ff;}',
        '.pair-alt-2{color:#e0a3ff;}',
        '.pair-alt-3{color:#7ee2b8;}',
        '.pair-alt-4{color:#ffc46b;}'
    ].join('\n');

    function activeFileName() { return window.WS ? WS.activeName() : 'main'; }

    // ── Condition 3: Cursor-style agent ───────────────────────────────────────
    var SVG = {
        agent: '<svg viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.4" d="M4.5 5.5c-1.7 0-3 1.1-3 2.5s1.3 2.5 3 2.5c2.4 0 4.6-5 7-5 1.7 0 3 1.1 3 2.5s-1.3 2.5-3 2.5c-2.4 0-4.6-5-7-5z"/></svg>',
        plan: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M2 3h1.5v1.5H2zm3 .25h9v1H5zM2 7.25h1.5v1.5H2zm3 .25h9v1H5zM2 11.5h1.5V13H2zm3 .25h9v1H5z"/></svg>',
        debug: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M5.5 3.5a2.5 2.5 0 015 0v.6l1.6-1 .5.9-1.9 1.1c.2.4.3.8.3 1.2v.2h2v1h-2v.5c0 .4 0 .7-.1 1l2 1.2-.5.9-1.9-1.1A3.5 3.5 0 018 13a3.5 3.5 0 01-3-1.6L3.1 12.5l-.5-.9 2-1.2c-.1-.3-.1-.6-.1-1V9h-2V8h2v-.2c0-.4.1-.8.3-1.2L2.9 5.5l.5-.9 1.6 1v-.6zm1 .4v.3h3v-.3a1.5 1.5 0 00-3 0zM6 7.8V9.4a2 2 0 004 0V7.8a1.5 1.5 0 00-1.5-1.5h-1A1.5 1.5 0 006 7.8z"/></svg>',
        ask: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M2 3.5A1.5 1.5 0 013.5 2h9A1.5 1.5 0 0114 3.5v6a1.5 1.5 0 01-1.5 1.5H6.2L3 13.7V11h0a1.5 1.5 0 01-1-1.4v-6zM3.5 3a.5.5 0 00-.5.5v6a.5.5 0 00.5.5H4v1.6L5.8 10h6.7a.5.5 0 00.5-.5v-6a.5.5 0 00-.5-.5h-9z"/></svg>',
        chevron: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M4 6l4 4 4-4z"/></svg>',
        send: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M8 2.5l4.5 4.5-.9.9L8.6 5v8.5H7.4V5L4.4 7.9l-.9-.9z"/></svg>',
        stop: '<svg viewBox="0 0 16 16"><rect x="4.5" y="4.5" width="7" height="7" rx="1" fill="currentColor"/></svg>',
        plus: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M7.4 2.5h1.2v4.9h4.9v1.2H8.6v4.9H7.4V8.6H2.5V7.4h4.9z"/></svg>',
        history: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M8 2a6 6 0 11-5.6 3.9l1.1.4A4.8 4.8 0 108 3.2a4.8 4.8 0 00-3.6 1.6H6V6H2.5V2.5h1.2v1.4A6 6 0 018 2zm-.6 2.5h1.2v3.2l2.1 1.3-.6 1-2.7-1.6z"/></svg>',
        more: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M3.5 7a1 1 0 110 2 1 1 0 010-2zm4.5 0a1 1 0 110 2 1 1 0 010-2zm4.5 0a1 1 0 110 2 1 1 0 010-2z"/></svg>',
        file: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M10 1H3.5L3 1.5v13l.5.5h9l.5-.5V4l-3-3zm0 1.4L11.6 4H10V2.4zM12 14H4V2h5v3h3v9z"/></svg>',
        read: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M8 3.5c3 0 5.3 2.1 6 4.5-.7 2.4-3 4.5-6 4.5S2.7 10.4 2 8c.7-2.4 3-4.5 6-4.5zm0 1.2c-2.3 0-4.1 1.5-4.8 3.3.7 1.8 2.5 3.3 4.8 3.3s4.1-1.5 4.8-3.3C12.1 6.2 10.3 4.7 8 4.7zm0 1.3a2 2 0 110 4 2 2 0 010-4z"/></svg>',
        run: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M2 3h12v10H2V3zm1 1v8h10V4H3zm1.3 1.7l2.5 2.3-2.5 2.3-.7-.7L5.4 8 3.6 6.4l.7-.7zM7.5 10h4v1h-4v-1z"/></svg>',
        skill: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M9.3 1.5L4 9h3.5l-1 5.5L12 7H8.5z"/></svg>',
        check: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M13.6 3.6l.8.8-8 8L2 8l.8-.8 3.6 3.6z"/></svg>',
        undo: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M3.6 5H9a4 4 0 010 8H6v-1h3a3 3 0 000-6H3.6l2.2 2.2-.7.7L1.7 5.5 5.1 2.1l.7.7z"/></svg>',
        close: '<svg viewBox="0 0 16 16"><path fill="currentColor" d="M8 7.2l3.6-3.6.8.8L8.8 8l3.6 3.6-.8.8L8 8.8l-3.6 3.6-.8-.8L7.2 8 3.6 4.4l.8-.8z"/></svg>'
    };

    // Study log rows for the agent (study_log.js, table "agent"): one row per code version of a response
    // (or one row for a response without code). Versions are rows "ag_{pair}_{oid}"; what happens to
    // them is updated by showVersions. At the end of the step, the applied version's files are compared
    // with the workspace to see whether the student edited them.
    var AgentLog = {
        tracked: {}, // row id -> files applied ([{filename, content, action}])
        rowId: function (pairId, oid) { return 'ag_' + pairId + '_' + oid; },
        code: function (files) {
            return (files || []).map(function (f) {
                return '// ' + f.filename + (f.action === 'delete' ? ' (deleted)' : '') + '\n' + (f.action === 'delete' ? '' : (f.content || ''));
            }).join('\n\n').slice(0, 20000);
        },
        set: function (id, fields) { if (window.StudyLog) StudyLog.upsert('agent', id, fields); },
        filesJson: function (files) {
            return JSON.stringify((files || []).slice(0, 20).map(function (f) {
                return { filename: f.filename, action: f.action || '', content: f.content == null ? null : String(f.content).slice(0, 20000) };
            }));
        },
        decide: function (ev, chosenOid, status, extra) {
            if (!window.StudyLog) return;
            var now = Date.now();
            ev.versions.forEach(function (v) {
                var id = AgentLog.rowId(ev.pair_id, v.oid), row = StudyLog.get(id);
                if (!row) return;
                var fields = { time_to_decision_ms: now - (row.available_at || now) };
                if (chosenOid) {
                    fields.suggestion_status = v.oid === chosenOid ? 'accepted' : 'not_chosen';
                    if (v.oid === chosenOid) {
                        fields.time_to_accept_ms = fields.time_to_decision_ms;
                        fields.applied_files = AgentLog.filesJson(v.files);
                        Object.assign(fields, extra || {});
                        AgentLog.tracked[id] = v.files;
                        StudyLog.setLastAccepted(id);
                    }
                    fields.selected_code_block = chosenOid;
                } else {
                    fields.suggestion_status = status;
                }
                StudyLog.upsert('agent', id, fields);
            });
        },
        // "Undo" on one file in the review bar: the applied version touching it is marked reverted.
        reverted: function (filename) {
            Object.keys(AgentLog.tracked).forEach(function (id) {
                if (AgentLog.tracked[id].some(function (f) { return f.filename === filename; })) AgentLog.set(id, { reverted: 'Yes' });
            });
        },
        run: function (ev) {
            if (!window.StudyLog) return;
            var last = StudyLog.lastAccepted(), id = StudyLog.id('cr');
            StudyLog.upsert('compilation', id, {
                compilation_attempt_id: id, run_by: 'agent', run_trigger: 'agent_tool', entry_file: ev.filename || '',
                compilation_status: ev.success ? 'success' : 'failure', run_status: ev.success ? 'success' : 'error',
                runtime_error: String(ev.error || '').slice(0, 2000), output_excerpt: String(ev.output || '').slice(0, 2000),
                suggestion_id: last ? last.suggestion_id || '' : ''
            });
        }
    };
    if (window.StudyLog) StudyLog.addFinalizer(function () {
        Object.keys(AgentLog.tracked).forEach(function (id) {
            var edited = AgentLog.tracked[id].some(function (f) {
                var now = WS.get(f.filename);
                return f.action === 'delete' ? now !== null : now !== (f.content || '');
            });
            AgentLog.set(id, { corrected_code: edited ? 'Yes' : 'No' });
        });
    });

    var MODES = [
        { id: 'agent', label: 'Agent', icon: 'agent', hint: 'Plan, search, and edit your files' },
        { id: 'plan', label: 'Plan', icon: 'plan', hint: 'Draft a step-by-step plan before changes' },
        { id: 'debug', label: 'Debug', icon: 'debug', hint: 'Run your code to find and fix bugs' },
        { id: 'ask', label: 'Ask', icon: 'ask', hint: 'Ask questions; no changes to files' }
    ];
    function modeOf(id) { return MODES.find(function (m) { return m.id === id; }) || MODES[0]; }

    function renderMarkdown(src) {
        return String(src).split('```').map(function (part, i) {
            if (i % 2 === 1) {
                var nl = part.indexOf('\n');
                var code = nl >= 0 ? part.slice(nl + 1) : part;
                return '<pre class="agent-code"><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>';
            }
            var h = esc(part)
                .replace(/`([^`\n]+)`/g, '<code>$1</code>')
                .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
                .replace(/^#{1,4} (.+)$/gm, '<div class="agent-h">$1</div>')
                .replace(/^\s*[-*] (.+)$/gm, '<div class="agent-li"><span>•</span><span>$1</span></div>')
                .replace(/^\s*(\d+)\. (.+)$/gm, '<div class="agent-li"><span>$1.</span><span>$2</span></div>')
                .replace(/_\(([^)]+)\)_/g, '<em>($1)</em>');
            return h.replace(/<\/div>\n/g, '</div>');
        }).join('');
    }

    // Line numbers (1-based) in `after` that are new or changed relative to `before` (LCS).
    function changedLines(before, after) {
        var a = before.split('\n'), b = after.split('\n');
        if (a.length * b.length > 400000) return b.map(function (_, i) { return i + 1; });
        var n = a.length, m = b.length, dp = [];
        for (var i = 0; i <= n; i++) dp.push(new Uint16Array(m + 1));
        for (i = n - 1; i >= 0; i--) {
            for (var j = m - 1; j >= 0; j--) {
                dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
        var out = [];
        i = 0; j = 0;
        while (i < n && j < m) {
            if (a[i] === b[j]) { i++; j++; }
            else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
            else { out.push(j + 1); j++; }
        }
        for (; j < m; j++) out.push(j + 1);
        return out;
    }

    // Line diff as [{t: ' ' | '+' | '-', s}] (LCS; whole-file replace when too large to compare).
    function diffOps(before, after) {
        var a = before === '' ? [] : before.split('\n'), b = after === '' ? [] : after.split('\n');
        if (a.length * b.length > 400000) {
            return a.map(function (s) { return { t: '-', s: s }; }).concat(b.map(function (s) { return { t: '+', s: s }; }));
        }
        var n = a.length, m = b.length, dp = [], i, j;
        for (i = 0; i <= n; i++) dp.push(new Uint16Array(m + 1));
        for (i = n - 1; i >= 0; i--) {
            for (j = m - 1; j >= 0; j--) {
                dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
        var out = [];
        i = 0; j = 0;
        while (i < n && j < m) {
            if (a[i] === b[j]) { out.push({ t: ' ', s: a[i] }); i++; j++; }
            else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: '-', s: a[i] }); i++; }
            else { out.push({ t: '+', s: b[j] }); j++; }
        }
        for (; i < n; i++) out.push({ t: '-', s: a[i] });
        for (; j < m; j++) out.push({ t: '+', s: b[j] });
        return out;
    }

    // Changed lines plus `ctx` lines of context around them; skipped stretches become one '…' entry.
    function lineCounts(before, after) {
        var added = changedLines(before || '', after || '').length;
        var removed = changedLines(after || '', before || '').length;
        return { added: added, removed: removed };
    }

    // The offered versions, reviewed in the editor (Cursor-style): an inline diff of one changed file
    // over the editor, with the version's changed files and change navigation above it. The student
    // picks the version, and applies or undoes it, in the Agent panel.
    var PREVIEW_LANG = { py: 'python', c: 'c', h: 'cpp', cpp: 'cpp', hpp: 'cpp', cc: 'cpp', java: 'java', js: 'javascript' };
    var Preview = {
        el: null, diff: null, models: null, state: null, changes: [], at: -1, whole: null, wholeDeco: [],

        build: function (monaco, editor) {
            if (this.el) return true;
            var wrap = document.querySelector('.editor-wrapper');
            if (!wrap) return false;
            var self = this, opt = monaco.editor.EditorOption;
            this.el = document.createElement('div');
            this.el.className = 'agent-preview';
            this.el.hidden = true;
            this.el.innerHTML = '<div class="agent-preview__bar"></div><div class="agent-preview__diff"></div>';
            wrap.appendChild(this.el);
            this.diff = monaco.editor.createDiffEditor(this.el.querySelector('.agent-preview__diff'), {
                readOnly: true, originalEditable: false, renderSideBySide: false, automaticLayout: true,
                theme: 'vs-dark', minimap: { enabled: false }, scrollBeyondLastLine: false, wordWrap: 'on',
                ignoreTrimWhitespace: false, renderIndicators: true, renderOverviewRuler: true,
                fontFamily: editor.getOption(opt.fontFamily), fontSize: editor.getOption(opt.fontSize)
            });
            this.diff.onDidUpdateDiff(function () {
                if (self.whole) return;
                self.changes = self.diff.getLineChanges() || [];
                if (self.at < 0 && self.changes.length) self.go(0); else self.renderNav();
            });
            return true;
        },

        // state: {ev, index, file, labels, onSelect(i, file)}
        show: function (state) {
            if (!this.build(Agent.monaco, Agent.editor)) return;
            this.state = state;
            var v = state.ev.versions[state.index];
            var f = v.files.find(function (x) { return x.filename === state.file; }) || v.files[0];
            state.file = f.filename;
            this.el.hidden = false;
            this.renderBar();
            this.setModels(f);
        },
        hide: function () {
            if (!this.el || this.el.hidden) return;
            this.el.hidden = true;
            this.state = null;
            this.diff.setModel(null);
            if (this.models) { this.models.original.dispose(); this.models.modified.dispose(); this.models = null; }
        },
        isOpen: function () { return !!(this.el && !this.el.hidden && this.state); },
        hasFile: function (name) {
            return this.isOpen() && this.state.ev.versions[this.state.index].files.some(function (f) { return f.filename === name; });
        },

        setModels: function (f) {
            var monaco = Agent.monaco, ws = WS.find(f.filename);
            var lang = ws ? ws.model.getLanguageId() : PREVIEW_LANG[f.filename.split('.').pop().toLowerCase()] || 'plaintext';
            var old = this.models, before = WS.get(f.filename);
            // A new or deleted file is shown whole, all green or all red: diffing it against an empty
            // file would show a stray removed (or added) empty line.
            this.whole = f.action === 'delete' ? 'deleted' : before === null ? 'new' : null;
            var a = this.whole === 'new' ? f.content || '' : before || '', b = this.whole === 'deleted' ? before || '' : f.content || '';
            this.models = { original: monaco.editor.createModel(a, lang), modified: monaco.editor.createModel(b, lang) };
            this.changes = [];
            this.at = -1;
            this.diff.setModel(this.models);
            if (old) { old.original.dispose(); old.modified.dispose(); }
            var ed = this.diff.getModifiedEditor(), cls = this.whole === 'deleted' ? 'agent-line-removed' : 'agent-line-added';
            this.wholeDeco = ed.deltaDecorations(this.wholeDeco, !this.whole ? [] : [{
                range: new monaco.Range(1, 1, this.models.modified.getLineCount(), 1),
                options: { isWholeLine: true, className: cls, linesDecorationsClassName: cls === 'agent-line-added' ? 'agent-line-gutter' : 'agent-line-gutter--del' }
            }]);
            if (this.whole) ed.revealLine(1);
            this.renderNav();
        },

        go: function (i) {
            if (!this.changes.length) return;
            this.at = (i + this.changes.length) % this.changes.length;
            var c = this.changes[this.at], ed = this.diff.getModifiedEditor();
            var line = Math.max(1, c.modifiedEndLineNumber ? c.modifiedStartLineNumber : c.modifiedStartLineNumber + 1);
            ed.revealLineInCenter(line);
            ed.setPosition({ lineNumber: line, column: 1 });
            this.renderNav();
        },

        renderBar: function () {
            var self = this, st = this.state, v = st.ev.versions[st.index];
            var bar = this.el.querySelector('.agent-preview__bar');
            bar.innerHTML =
                '<div class="agent-preview__row">' +
                '<span class="agent-preview__title">' + SVG.agent + ' Agent changes</span>' +
                '<span class="agent-preview__version">Version ' + st.labels[st.index] + '</span>' +
                '<span class="agent-preview__nav"><button type="button" data-go="-1" title="Previous change">&uarr;</button>' +
                '<span class="agent-preview__count"></span>' +
                '<button type="button" data-go="1" title="Next change">&darr;</button></span>' +
                '<button type="button" class="agent-ibtn" data-close="1" title="Close (choose, apply or undo in the Agent panel)">&times;</button>' +
                '</div>' +
                '<div class="agent-preview__files">' + v.files.map(function (f) {
                    var tag;
                    if (f.action === 'delete') tag = '<span class="agent-del">deleted</span>';
                    else {
                        var c = lineCounts(WS.get(f.filename) || '', f.content || '');
                        tag = (WS.get(f.filename) === null ? '<span class="agent-add">new</span> ' : '') +
                            '<span class="agent-add">+' + c.added + '</span> <span class="agent-del">-' + c.removed + '</span>';
                    }
                    return '<button type="button" data-file="' + esc(f.filename) + '" class="agent-preview__file' +
                        (f.filename === st.file ? ' is-active' : '') + '">' + SVG.file + '<span>' + esc(f.filename) + '</span>' + tag + '</button>';
                }).join('') + '</div>';
            bar.querySelectorAll('[data-file]').forEach(function (b) { b.onclick = function () { st.onSelect(st.index, b.dataset.file); }; });
            bar.querySelectorAll('[data-go]').forEach(function (b) { b.onclick = function () { self.go(self.at + +b.dataset.go); }; });
            bar.querySelector('[data-close]').onclick = function () { self.hide(); };
            this.renderNav();
        },
        renderNav: function () {
            if (!this.el) return;
            var n = this.changes.length, count = this.el.querySelector('.agent-preview__count');
            if (count) count.textContent = this.whole === 'new' ? 'New file' : this.whole === 'deleted' ? 'File deleted'
                : n ? 'Change ' + (this.at + 1 > 0 ? this.at + 1 : 1) + ' of ' + n : 'No changes';
            this.el.querySelectorAll('[data-go]').forEach(function (b) { b.disabled = n < 2; });
        }
    };

    var Agent = {
        monaco: null,
        editor: null,
        mode: 'agent',
        busy: false,
        abort: null,
        chats: [],            // [{id, title, mode, history:[{role, content}]}]
        chatId: '',
        pending: {},          // filename -> {baseline: string|null, turn_id}
        decorations: {},      // filename -> decoration ids
        skills: [],           // [{name, instructions, source, enabled}]
        attachedSkills: [],   // skills picked with "/" for the next message
        popup: null,          // {kind: 'slash'|'at'|'mode', items, index, start}
        reviewOpen: false,
        els: {},

        storageKey: function () { return 'agentChats_' + currentSession.sessionId; },
        chat: function () { var id = this.chatId; return this.chats.find(function (c) { return c.id === id; }); },

        init: function (monaco, editor) {
            this.monaco = monaco;
            this.editor = editor;
            this.loadSkills();
            this.loadChats();
            this.buildPanel();
            this.renderChat();
            var self = this;
            var t = null;
            editor.onDidChangeModelContent(function () {
                if (!Object.keys(self.pending).length) return;
                clearTimeout(t);
                t = setTimeout(function () { self.refreshDecorations(); self.renderReview(); }, 300);
            });
            WS.listeners.push(function (ev) {
                if (ev.type === 'rename' && self.pending[ev.from]) {
                    self.pending[ev.name] = self.pending[ev.from];
                    delete self.pending[ev.from];
                    self.syncBadges();
                    self.renderReview();
                }
                if (ev.type === 'open') {
                    self.refreshDecorations();
                    if (Preview.isOpen()) {
                        if (Preview.hasFile(ev.name) && self.versionsShow) self.versionsShow(Preview.state.index, ev.name);
                        else Preview.hide();
                    }
                }
            });
        },

        // Chats ----------------------------------------------------------------
        loadChats: function () {
            try {
                var saved = JSON.parse(sessionStorage.getItem(this.storageKey()) || 'null');
                if (saved && Array.isArray(saved.chats) && saved.chats.length) {
                    this.chats = saved.chats;
                    this.chatId = saved.current;
                }
            } catch (e) { /* ignore */ }
            if (!this.chat()) this.newChat(true);
            this.mode = this.chat().mode || 'agent';
        },
        persist: function () {
            try {
                var chats = this.chats.slice(-15).map(function (c) {
                    return { id: c.id, title: c.title, mode: c.mode, history: c.history.slice(-40) };
                });
                sessionStorage.setItem(this.storageKey(), JSON.stringify({ chats: chats, current: this.chatId }));
            } catch (e) { /* ignore */ }
        },
        newChat: function (silent) {
            var current = this.chat();
            if (current && !current.history.length && !silent) { this.els.input && this.els.input.focus(); return; }
            var id = 'c' + Date.now().toString(36);
            this.chats.push({ id: id, title: 'New Agent', mode: this.mode || 'agent', history: [] });
            this.chatId = id;
            this.persist();
            if (!silent) {
                if (this.busy) this.stop();
                log('agent_new_chat', {});
                this.renderChat();
                this.els.input.focus();
            }
        },
        switchChat: function (id) {
            if (this.busy || id === this.chatId) return;
            this.chatId = id;
            this.setMode(this.chat().mode || 'agent');
            this.persist();
            this.renderChat();
        },

        // Skills ---------------------------------------------------------------
        loadSkills: function () {
            var instr = (currentSession.agentSkillsInstructor || []).map(function (s) {
                return { name: s.name, instructions: s.instructions, source: 'instructor', enabled: true };
            });
            var mine = (currentSession.agentSkillsStudent || []).map(function (s) {
                return { name: s.name, instructions: s.instructions, source: 'student', enabled: true };
            });
            var off = [];
            try { off = JSON.parse(localStorage.getItem('agentSkillsOff_' + currentSession.sessionId) || '[]'); } catch (e) { /* ignore */ }
            this.skills = instr.concat(mine).map(function (s) { s.enabled = off.indexOf(s.source + ':' + s.name) < 0; return s; });
        },
        saveSkillToggles: function () {
            var off = this.skills.filter(function (s) { return !s.enabled; }).map(function (s) { return s.source + ':' + s.name; });
            try { localStorage.setItem('agentSkillsOff_' + currentSession.sessionId, JSON.stringify(off)); } catch (e) { /* ignore */ }
        },
        saveStudentSkills: function () {
            var mine = this.skills.filter(function (s) { return s.source === 'student'; })
                .map(function (s) { return { name: s.name, instructions: s.instructions }; });
            fetch('/agent/skills', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: currentSession.sessionId, skills: mine })
            }).catch(function () { /* ignore */ });
            try {
                var ad = JSON.parse(sessionStorage.getItem('authData') || '{}');
                ad.agentSkillsStudent = mine;
                sessionStorage.setItem('authData', JSON.stringify(ad));
            } catch (e) { /* ignore */ }
            currentSession.agentSkillsStudent = mine;
        },

        // Layout ---------------------------------------------------------------
        setupResize: function (handle, card) {
            var app = document.getElementById('application');
            try {
                var saved = parseInt(localStorage.getItem('agentCardWidth') || '', 10);
                if (saved >= 280) app.style.setProperty('--agent-w', saved + 'px');
            } catch (e) { /* ignore */ }
            handle.addEventListener('mousedown', function (e) {
                e.preventDefault();
                document.body.style.cursor = 'ew-resize';
                function move(ev) {
                    var w = Math.max(280, Math.min(window.innerWidth * 0.45, window.innerWidth - ev.clientX - 8));
                    app.style.setProperty('--agent-w', w + 'px');
                    if (window.editor) window.editor.layout();
                }
                function up() {
                    document.body.style.cursor = '';
                    document.removeEventListener('mousemove', move);
                    document.removeEventListener('mouseup', up);
                    try { localStorage.setItem('agentCardWidth', String(Math.round(card.getBoundingClientRect().width))); } catch (e) { /* ignore */ }
                }
                document.addEventListener('mousemove', move);
                document.addEventListener('mouseup', up);
            });
        },

        buildPanel: function () {
            // The agent gets its own card on the right, so the problem (left) and the
            // chat (right) are visible at the same time, with the code in the middle.
            var app = document.getElementById('application');
            if (!app) return;
            var handle = document.createElement('div');
            handle.id = 'resize-agent';
            var card = document.createElement('div');
            card.id = 'agentCard';
            card.className = 'lc-card agent-card';
            card.innerHTML =
                '<div class="lc-card__header">' +
                '  <span class="lc-card__title" id="problemTabAgent">' +
                '    <svg viewBox="0 0 16 16" class="tab-icon" style="color:#a78bfa"><path fill="currentColor" d="M8 1l1.6 4.4L14 7l-4.4 1.6L8 13l-1.6-4.4L2 7l4.4-1.6zM13 11l.7 1.3L15 13l-1.3.7L13 15l-.7-1.3L11 13l1.3-.7z"/></svg>Agent' +
                '  </span>' +
                '</div>';
            app.appendChild(handle);
            app.appendChild(card);
            app.classList.add('has-agent');

            var tab = card.querySelector('#problemTabAgent');
            var panel = document.createElement('div');
            panel.className = 'agent-panel';
            panel.id = 'problemPanelAgent';
            panel.innerHTML =
                '<div class="agent-head">' +
                '  <div class="agent-head__title" id="agentTitle">New Agent</div>' +
                '  <button type="button" class="agent-ibtn" id="agentNewChatBtn" title="New chat">' + SVG.plus + '</button>' +
                '  <button type="button" class="agent-ibtn" id="agentHistoryBtn" title="Chat history">' + SVG.history + '</button>' +
                '  <button type="button" class="agent-ibtn" id="agentMoreBtn" title="Skills">' + SVG.more + '</button>' +
                '</div>' +
                '<div class="agent-body" id="agentBody">' +
                '  <div class="agent-messages" id="agentMessages"></div>' +
                '  <div class="agent-dock" id="agentDock">' +
                '    <div class="agent-review" id="agentReview" hidden></div>' +
                '    <div class="agent-composer" id="agentComposer">' +
                '      <div class="agent-chips" id="agentChips"></div>' +
                '      <textarea id="agentInput" rows="1" placeholder="Plan, Build, / for skills, @ for context" aria-label="Message the agent"></textarea>' +
                '      <div class="agent-composer__row">' +
                '        <button type="button" class="agent-mode-pill" id="agentModeBtn" aria-haspopup="listbox"></button>' +
                '        <span class="agent-model" title="Model used by the agent">gpt-6-luna</span>' +
                '        <button type="button" class="agent-send" id="agentSendBtn" title="Send (Enter)" aria-label="Send">' + SVG.send + '</button>' +
                '      </div>' +
                '    </div>' +
                '    <div class="agent-popup" id="agentPopup" hidden role="listbox"></div>' +
                '  </div>' +
                '</div>' +
                '<div class="agent-sheet" id="agentSheet" hidden></div>';
            card.appendChild(panel);
            this.setupResize(handle, card);

            var els = this.els = {
                tab: tab, panel: panel,
                title: panel.querySelector('#agentTitle'),
                body: panel.querySelector('#agentBody'),
                messages: panel.querySelector('#agentMessages'),
                dock: panel.querySelector('#agentDock'),
                input: panel.querySelector('#agentInput'),
                chips: panel.querySelector('#agentChips'),
                send: panel.querySelector('#agentSendBtn'),
                modeBtn: panel.querySelector('#agentModeBtn'),
                review: panel.querySelector('#agentReview'),
                popup: panel.querySelector('#agentPopup'),
                sheet: panel.querySelector('#agentSheet')
            };
            var self = this;

            els.input.addEventListener('focus', function () {
                if (!self._focusLogged) { self._focusLogged = true; log('agent_panel_opened', {}); }
            });

            els.send.addEventListener('click', function () { if (self.busy) self.stop(); else self.send(); });
            els.input.addEventListener('input', function () { self.autosize(); self.updatePopup(); });
            els.input.addEventListener('keydown', function (e) { self.onKey(e); });
            els.input.addEventListener('blur', function () { setTimeout(function () { if (self.popup && self.popup.kind !== 'mode') self.closePopup(); }, 150); });
            els.modeBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (self.popup && self.popup.kind === 'mode') self.closePopup(); else self.openModeMenu();
            });
            panel.querySelector('#agentNewChatBtn').addEventListener('click', function () { self.newChat(); });
            panel.querySelector('#agentHistoryBtn').addEventListener('click', function (e) { e.stopPropagation(); self.openHistory(); });
            panel.querySelector('#agentMoreBtn').addEventListener('click', function () { self.openSkillsSheet(); });
            document.addEventListener('click', function (e) {
                if (!self.popup || els.popup.contains(e.target)) return;
                if (e.target !== els.input || self.popup.kind === 'mode' || self.popup.kind === 'history') self.closePopup();
            });
            document.addEventListener('keydown', function (e) {
                if (e.key === 'Escape' && self.popup) { self.closePopup(); els.input.focus(); }
            });
            this.setMode(this.mode);
        },

        autosize: function () {
            var t = this.els.input;
            t.style.height = 'auto';
            t.style.height = Math.min(t.scrollHeight, 200) + 'px';
        },

        setMode: function (mode, byUser) {
            this.mode = mode;
            var m = modeOf(mode);
            this.els.modeBtn.innerHTML = SVG[m.icon] + '<span>' + m.label + '</span>' + SVG.chevron;
            this.els.modeBtn.title = m.hint;
            this.els.modeBtn.dataset.mode = mode;
            var c = this.chat();
            if (c) c.mode = mode;
            if (byUser) { log('agent_mode_changed', { mode: mode }); this.persist(); }
        },

        // "/" skills, "@" files, and the mode menu share one popup -------------
        updatePopup: function () {
            var t = this.els.input, pos = t.selectionStart, before = t.value.slice(0, pos);
            var m = before.match(/(^|\s)([\/@])([\w.-]*)$/);
            if (!m) { if (this.popup) this.closePopup(); return; }
            var kind = m[2] === '/' ? 'slash' : 'at', q = m[3].toLowerCase();
            var items;
            if (kind === 'slash') {
                items = this.skills.filter(function (s) { return s.name.toLowerCase().indexOf(q) >= 0; })
                    .map(function (s) { return { value: s.name, label: s.name, desc: s.source === 'instructor' ? 'Instructor skill' : 'Your skill', icon: SVG.skill }; });
                items.push({ value: '__new__', label: 'Create a skill…', desc: 'Reusable instructions for the agent', icon: SVG.plus });
            } else {
                items = WS.names().filter(function (n) { return n.toLowerCase().indexOf(q) >= 0; })
                    .map(function (n) { return { value: n, label: n, desc: 'File', icon: SVG.file }; });
            }
            this.popup = { kind: kind, items: items, index: 0, start: pos - m[3].length - 1 };
            this.renderPopup(kind === 'slash' ? 'Skills' : 'Files');
        },
        openModeMenu: function () {
            var self = this;
            this.popup = {
                kind: 'mode', index: Math.max(0, MODES.findIndex(function (m) { return m.id === self.mode; })),
                items: MODES.map(function (m) { return { value: m.id, label: m.label, desc: m.hint, icon: SVG[m.icon] }; })
            };
            this.renderPopup('Mode');
        },
        renderPopup: function (title) {
            var self = this, p = this.popup, box = this.els.popup;
            if (!p.items.length) { box.hidden = true; return; }
            box.className = 'agent-popup agent-popup--' + p.kind;
            box.innerHTML = '<div class="agent-popup__title">' + title + '</div>' + p.items.map(function (it, i) {
                return '<div class="agent-popup__item' + (i === p.index ? ' is-active' : '') + '" data-i="' + i + '" role="option">' +
                    '<span class="agent-popup__icon">' + it.icon + '</span><span class="agent-popup__label">' + esc(it.label) + '</span>' +
                    '<span class="agent-popup__desc">' + esc(it.desc) + '</span>' +
                    (p.kind === 'mode' && it.value === self.mode ? '<span class="agent-popup__check">✓</span>' : '') + '</div>';
            }).join('');
            box.hidden = false;
            box.querySelectorAll('[data-i]').forEach(function (el) {
                el.addEventListener('mousedown', function (e) { e.preventDefault(); self.pick(+el.dataset.i); });
            });
        },
        closePopup: function () { this.popup = null; this.els.popup.hidden = true; },
        pick: function (i) {
            var p = this.popup;
            if (!p) return;
            var it = p.items[i];
            this.closePopup();
            if (!it) return;
            if (p.kind === 'mode') { this.setMode(it.value, true); this.els.input.focus(); return; }
            var t = this.els.input, end = t.selectionStart;
            if (p.kind === 'slash') {
                t.value = t.value.slice(0, p.start) + t.value.slice(end);
                t.setSelectionRange(p.start, p.start);
                if (it.value === '__new__') { this.openSkillsSheet(true); return; }
                if (this.attachedSkills.indexOf(it.value) < 0) this.attachedSkills.push(it.value);
                this.renderChips();
            } else {
                var insert = '@' + it.value + ' ';
                t.value = t.value.slice(0, p.start) + insert + t.value.slice(end);
                t.setSelectionRange(p.start + insert.length, p.start + insert.length);
            }
            this.autosize();
            t.focus();
        },
        renderChips: function () {
            var self = this;
            this.els.chips.innerHTML = this.attachedSkills.map(function (n, i) {
                return '<span class="agent-chip-tag">' + SVG.skill + esc(n) + '<button type="button" data-rm="' + i + '" aria-label="Remove">×</button></span>';
            }).join('');
            this.els.chips.querySelectorAll('[data-rm]').forEach(function (b) {
                b.addEventListener('click', function () { self.attachedSkills.splice(+b.dataset.rm, 1); self.renderChips(); });
            });
        },
        onKey: function (e) {
            var p = this.popup;
            if (p && !this.els.popup.hidden) {
                if (e.key === 'ArrowDown') { e.preventDefault(); p.index = (p.index + 1) % p.items.length; this.renderPopup(this.els.popup.querySelector('.agent-popup__title').textContent); return; }
                if (e.key === 'ArrowUp') { e.preventDefault(); p.index = (p.index - 1 + p.items.length) % p.items.length; this.renderPopup(this.els.popup.querySelector('.agent-popup__title').textContent); return; }
                if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); this.pick(p.index); return; }
                if (e.key === 'Escape') { e.preventDefault(); this.closePopup(); return; }
            }
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
                e.preventDefault();
                if (!this.busy) this.send();
            } else if (e.key === 'Backspace' && !this.els.input.value && this.attachedSkills.length) {
                this.attachedSkills.pop();
                this.renderChips();
            } else if ((e.metaKey || e.ctrlKey) && e.key === '.') {
                e.preventDefault();
                this.openModeMenu();
            }
        },

        openHistory: function () {
            var self = this;
            var items = this.chats.slice().reverse().filter(function (c) { return c.history.length || c.id === self.chatId; });
            this.popup = {
                kind: 'history', index: 0,
                items: items.map(function (c) {
                    return { value: c.id, label: c.title, desc: c.history.length ? Math.ceil(c.history.length / 2) + ' messages' : 'Empty', icon: SVG[modeOf(c.mode).icon] };
                })
            };
            this.renderPopup('Chats');
            var box = this.els.popup, me = this;
            box.querySelectorAll('[data-i]').forEach(function (el) {
                el.onmousedown = function (e) { e.preventDefault(); me.closePopup(); me.switchChat(items[+el.dataset.i].id); };
            });
        },

        openSkillsSheet: function (startNew) {
            var self = this, sheet = this.els.sheet;
            sheet.hidden = false;
            sheet.innerHTML =
                '<div class="agent-sheet__head"><span>Skills</span><button type="button" class="agent-ibtn" id="agentSheetClose" aria-label="Close">×</button></div>' +
                '<p class="agent-sheet__hint">Skills are instructions the agent follows. Enabled skills apply to every message; type <code>/</code> in the chat to attach one to a single message.</p>' +
                (this.skills.length ? '' : '<p class="agent-sheet__hint">No skills yet.</p>') +
                this.skills.map(function (s, i) {
                    return '<div class="agent-skill"><label class="agent-switch"><input type="checkbox" data-i="' + i + '"' + (s.enabled ? ' checked' : '') + '><span></span></label>' +
                        '<div class="agent-skill__main"><div class="agent-skill__name">' + esc(s.name) +
                        (s.source === 'instructor' ? ' <span class="agent-skill__tag">Instructor</span>' : '') + '</div>' +
                        '<div class="agent-skill__text">' + esc(s.instructions) + '</div></div>' +
                        (s.source === 'student' ? '<button type="button" class="agent-link" data-del="' + i + '">Delete</button>' : '') + '</div>';
                }).join('') +
                '<div class="agent-skill-new"' + (startNew ? '' : ' hidden') + ' id="agentSkillForm">' +
                '<input type="text" id="agentSkillName" maxlength="60" placeholder="Skill name, e.g. explain-simply">' +
                '<textarea id="agentSkillText" rows="4" maxlength="4000" placeholder="Instructions for the agent…"></textarea>' +
                '<div class="agent-skill-new__row"><button type="button" class="agent-btn" id="agentSkillCancel">Cancel</button>' +
                '<button type="button" class="agent-btn agent-btn--primary" id="agentSkillAdd">Add skill</button></div></div>' +
                '<button type="button" class="agent-btn" id="agentSkillShow"' + (startNew ? ' hidden' : '') + '>' + SVG.plus + ' New skill</button>';
            sheet.querySelector('#agentSheetClose').onclick = function () { sheet.hidden = true; self.els.input.focus(); };
            sheet.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
                cb.onchange = function () {
                    var s = self.skills[+cb.dataset.i];
                    s.enabled = cb.checked;
                    self.saveSkillToggles();
                    log('agent_skill_toggled', { name: s.name, source: s.source, enabled: s.enabled });
                };
            });
            sheet.querySelectorAll('[data-del]').forEach(function (b) {
                b.onclick = function () {
                    var s = self.skills.splice(+b.dataset.del, 1)[0];
                    log('agent_skill_deleted', { name: s.name });
                    self.saveStudentSkills();
                    self.saveSkillToggles();
                    self.openSkillsSheet();
                };
            });
            var form = sheet.querySelector('#agentSkillForm'), show = sheet.querySelector('#agentSkillShow');
            show.onclick = function () { form.hidden = false; show.hidden = true; sheet.querySelector('#agentSkillName').focus(); };
            sheet.querySelector('#agentSkillCancel').onclick = function () { form.hidden = true; show.hidden = false; };
            sheet.querySelector('#agentSkillAdd').onclick = function () {
                var name = sheet.querySelector('#agentSkillName').value.trim();
                var text = sheet.querySelector('#agentSkillText').value.trim();
                if (!name || !text) return;
                self.skills.push({ name: name, instructions: text, source: 'student', enabled: true });
                log('agent_skill_created', { name: name, length: text.length });
                self.saveStudentSkills();
                self.saveSkillToggles();
                self.openSkillsSheet();
            };
            if (startNew) sheet.querySelector('#agentSkillName').focus();
        },

        // Messages ---------------------------------------------------------------
        renderChat: function () {
            var c = this.chat(), self = this;
            this.els.title.textContent = c.title;
            this.els.messages.innerHTML = '';
            c.history.forEach(function (m) {
                if (m.role === 'user') self.addUser(m.content);
                else {
                    var turn = self.newTurn();
                    var div = document.createElement('div');
                    div.className = 'agent-text';
                    div.innerHTML = renderMarkdown(m.content);
                    turn.appendChild(div);
                }
            });
            this.els.body.classList.toggle('agent-body--empty', !c.history.length);
            this.scroll();
        },
        addUser: function (text) {
            this.els.body.classList.remove('agent-body--empty');
            var div = document.createElement('div');
            div.className = 'agent-user';
            div.textContent = text;
            this.els.messages.appendChild(div);
            this.scroll();
        },
        newTurn: function () {
            var turn = document.createElement('div');
            turn.className = 'agent-turn';
            this.els.messages.appendChild(turn);
            return turn;
        },
        addStep: function (turn, icon, html, cls) {
            var row = document.createElement('div');
            row.className = 'agent-step' + (cls ? ' ' + cls : '');
            row.innerHTML = '<span class="agent-step__icon">' + icon + '</span><span class="agent-step__text">' + html + '</span>';
            turn.appendChild(row);
            this.scroll();
            return row;
        },
        scroll: function () { var m = this.els.messages; m.scrollTop = m.scrollHeight; },
        stop: function () { if (this.abort) this.abort.abort(); log('agent_stopped', { mode: this.mode }); },
        setBusy: function (busy) {
            this.busy = busy;
            this.els.send.innerHTML = busy ? SVG.stop : SVG.send;
            this.els.send.title = busy ? 'Stop' : 'Send (Enter)';
            this.els.send.classList.toggle('agent-send--stop', busy);
        },

        send: async function (override) {
            var text = (override || this.els.input.value || '').trim();
            if (!text || this.busy) return;
            if (this.editor.getOption(this.monaco.editor.EditorOption.readOnly)) return;
            var chat = this.chat(), mode = this.mode, self = this;
            if (this.expireVersions) this.expireVersions();
            if (!override) { this.els.input.value = ''; this.autosize(); }
            this.closePopup();
            this.addUser(text);
            if (!chat.history.length) { chat.title = text.length > 40 ? text.slice(0, 40) + '…' : text; this.els.title.textContent = chat.title; }

            var attached = this.attachedSkills.slice();
            this.attachedSkills = [];
            this.renderChips();
            var skills = this.skills.filter(function (s) { return s.enabled || attached.indexOf(s.name) >= 0; })
                .map(function (s) { return { name: s.name, instructions: s.instructions, source: s.source }; });
            var mentions = (text.match(/@([\w.-]+)/g) || []).map(function (m) { return m.slice(1); })
                .filter(function (n) { return WS.get(n) !== null; });
            var term = document.getElementById('terminalOutput');
            var payload = {
                session_id: currentSession.sessionId,
                mode: mode,
                message: text + (mentions.length ? '\n\n(Files the student referenced: ' + mentions.join(', ') + ')' : ''),
                language: lang(),
                files: WS.list(),
                active_file: WS.activeName(),
                history: chat.history.slice(-20),
                skills: skills,
                terminal_output: term ? term.innerText.slice(-3000) : ''
            };
            log('agent_message_sent', { mode: mode, length: text.length, skills: skills.map(function (s) { return s.name; }), mentions: mentions, attached_skills: attached });

            var turn = this.newTurn();
            var sentAt = Date.now(), firstTextAt = 0, tools = { read: 0, edit: 0, run: 0 };
            // The student's files when they asked (the study log's code snapshot).
            var workspace = WS.list();
            var snapshot = { code_snapshot: AgentLog.code(workspace), workspace_files: AgentLog.filesJson(workspace),
                             prompted_at: new Date(sentAt).toISOString() };
            var turnIndex = chat.history.filter(function (h) { return h.role === 'user'; }).length + 1;
            var interaction = window.StudyLog ? StudyLog.nextIndex() : 0;
            var textEl = null, blockText = '', answer = '', turnId = '', failed = false;
            var proposed = [], gotVersions = false, preparing = null, appliedDescription = '';
            var thinking = this.addStep(turn, '<span class="agent-spinner"></span>', '<span class="agent-shimmer">Thinking</span>', 'agent-step--thinking');
            this.setBusy(true);
            this.abort = new AbortController();

            function handle(ev) {
                if (thinking) { thinking.remove(); thinking = null; }
                if (ev.type === 'text') {
                    if (!firstTextAt) firstTextAt = Date.now();
                    if (!textEl) {
                        textEl = document.createElement('div');
                        textEl.className = 'agent-text';
                        turn.appendChild(textEl);
                        blockText = '';
                        if (answer) answer += '\n\n';
                    }
                    blockText += ev.delta;
                    answer += ev.delta;
                    textEl.innerHTML = renderMarkdown(blockText);
                    self.scroll();
                } else if (ev.type === 'tool') {
                    tools.read++;
                    textEl = null;
                    if (ev.name === 'list_files') self.addStep(turn, SVG.file, 'Listed files');
                    else if (ev.name === 'read_file') self.addStep(turn, SVG.read, 'Read <span class="agent-file">' + esc(ev.filename) + '</span>');
                    else if (ev.name === 'check_tests') self.addStep(turn, SVG.debug, 'Checked <span class="agent-file">' + esc(ev.filename) + '</span> against the test cases: fixing a failure');
                } else if (ev.type === 'file') {
                    // Not applied yet: the turn ends with versions to choose from (see showVersions).
                    proposed.push(ev);
                    tools.edit++;
                    var verb = ev.action === 'delete' ? 'Deleted' : ev.action === 'create' ? 'Created' : 'Edited';
                    self.addStep(turn, SVG.file, verb + ' <span class="agent-file">' + esc(ev.filename) + '</span>' +
                        (ev.action === 'delete' ? '' : ' <span class="agent-add">+' + ev.added + '</span> <span class="agent-del">-' + ev.removed + '</span>'));
                } else if (ev.type === 'versions_pending') {
                    textEl = null;
                    preparing = self.addStep(turn, '<span class="agent-spinner"></span>', '<span class="agent-shimmer">Preparing versions</span>', 'agent-step--thinking');
                } else if (ev.type === 'versions') {
                    if (preparing) { preparing.remove(); preparing = null; }
                    gotVersions = true;
                    if (!firstTextAt) firstTextAt = Date.now();
                    ev.versions.forEach(function (v, i) {
                        AgentLog.set(AgentLog.rowId(ev.pair_id, v.oid), {
                            interaction_index: interaction, chat_id: chat.id, turn_index: turnIndex, agent_mode: mode,
                            response_id: ev.pair_id, user_prompt: text.slice(0, 20000),
                            agent_response: ((answer ? answer + '\n\n' : '') + (v.description || '')).slice(0, 20000),
                            tool_calls: 'read ' + tools.read + ' · edit ' + tools.edit + ' · run ' + tools.run,
                            files_changed: v.files.map(function (f) { return f.filename; }).join(', '),
                            suggestion_id: v.oid, option_label: ['A', 'B', 'C', 'D'][i] || String(i + 1),
                            options_in_set: ev.versions.length, suggestion_code: AgentLog.code(v.files),
                            previewed: 'No', preview_count: 0, suggestion_status: 'pending', feedback_shown: '',
                            explanation_clicked: 'No', reverted: 'No', available_at: Date.now(),
                            response_time_ms: Date.now() - sentAt, time_to_first_token_ms: firstTextAt - sentAt
                        });
                        AgentLog.set(AgentLog.rowId(ev.pair_id, v.oid), snapshot);
                    });
                    if (ev.versions.length === 1) {
                        // A single version: the agent's summary is shown as its reply, as usual.
                        var only = ev.versions[0];
                        if (only.description) handle({ type: 'text', delta: only.description });
                        self.applyFiles(only.files, turnId);
                        AgentLog.decide(ev, only.oid, '', { accept_method: 'auto', previewed: 'Yes', preview_count: 1 });
                        // "1 option, randomly correct or buggy": it may have had a bug, so applying it is
                        // reported like choosing a version, and the reply carries the fix if so.
                        if (ev.single) {
                            pairEvent(ev.pair_id, only.oid, 'accepted').then(function (res) {
                                var fb = res && res.feedback;
                                if (fb) self.fixSingleVersion(turn, fb, ev, turnId);
                            });
                        }
                    } else {
                        self.showVersions(turn, ev, turnId, function (v) {
                            // The chat history (sent back to the agent) keeps the description the student chose.
                            appliedDescription = v.description || '';
                            var last = chat.history[chat.history.length - 1];
                            if (last && last.role === 'assistant' && last.turn_id === turnId) {
                                last.content = (answer ? answer + '\n\n' : '') + appliedDescription;
                                self.persist();
                            }
                        });
                    }
                } else if (ev.type === 'run') {
                    tools.run++;
                    AgentLog.run(ev);
                    var out = ((ev.output || '') + (ev.error ? '\n' + ev.error : '')).trim();
                    var r = self.addStep(turn, SVG.run,
                        '<details><summary>Ran <span class="agent-file">' + esc(ev.filename) + '</span> <span class="' + (ev.success ? 'agent-ok' : 'agent-err') + '">' +
                        (ev.success ? 'succeeded' : 'failed') + '</span></summary><pre class="agent-code">' + esc(out || '(no output)') + '</pre></details>');
                    r.classList.add('agent-step--run');
                } else if (ev.type === 'done') {
                    turnId = ev.turn_id;
                } else if (ev.type === 'error') {
                    failed = true;
                    self.addStep(turn, '!', esc(ev.message), 'agent-step--err');
                }
            }

            try {
                var res = await fetch('/agent/chat', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload), signal: this.abort.signal
                });
                if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
                var reader = res.body.getReader(), decoder = new TextDecoder(), buf = '';
                while (true) {
                    var chunk = await reader.read();
                    if (chunk.done) break;
                    buf += decoder.decode(chunk.value, { stream: true });
                    var parts = buf.split('\n\n');
                    buf = parts.pop();
                    parts.forEach(function (p) {
                        if (p.indexOf('data: ') === 0) { try { handle(JSON.parse(p.slice(6))); } catch (e) { console.warn('agent event', e); } }
                    });
                }
            } catch (e) {
                if (thinking) { thinking.remove(); thinking = null; }
                if (e.name === 'AbortError') self.addStep(turn, SVG.stop, 'Stopped', 'agent-step--muted');
                else { failed = true; self.addStep(turn, '!', 'Could not reach the agent. Please try again.', 'agent-step--err'); }
            }
            if (thinking) thinking.remove();
            if (preparing) preparing.remove();
            // Stopped or failed before the versions arrived: apply what the agent did, as before.
            if (!gotVersions && proposed.length) this.applyFiles(proposed, turnId);
            this.setBusy(false);
            this.abort = null;

            chat.history.push({ role: 'user', content: text });
            var reply = (answer && appliedDescription ? answer + '\n\n' : answer) + appliedDescription;
            if (reply || gotVersions) chat.history.push({ role: 'assistant', content: reply, turn_id: turnId });
            this.persist();
            log('agent_response_received', { mode: mode, turn_id: turnId, length: answer.length, failed: failed });
            if (!gotVersions && window.StudyLog) {
                // A reply without code versions (Ask or Plan, or changes applied directly): one row.
                AgentLog.set(StudyLog.id('ag'), {
                    interaction_index: interaction, chat_id: chat.id, turn_index: turnIndex, agent_mode: mode,
                    response_id: turnId, user_prompt: text.slice(0, 20000), agent_response: answer.slice(0, 20000),
                    tool_calls: 'read ' + tools.read + ' · edit ' + tools.edit + ' · run ' + tools.run,
                    files_changed: proposed.map(function (f) { return f.filename; }).join(', '),
                    suggestion_status: failed ? 'failed' : (proposed.length ? 'applied_directly' : 'no_code'),
                    response_time_ms: Date.now() - sentAt, time_to_first_token_ms: firstTextAt ? firstTextAt - sentAt : '',
                    code_snapshot: snapshot.code_snapshot, workspace_files: snapshot.workspace_files, prompted_at: snapshot.prompted_at
                });
            }

            if (mode === 'plan' && answer && !failed) {
                var build = document.createElement('button');
                build.type = 'button';
                build.className = 'agent-btn agent-btn--primary agent-build';
                build.innerHTML = SVG.agent + ' Build this plan';
                build.addEventListener('click', function () {
                    build.remove();
                    log('agent_plan_build', { turn_id: turnId });
                    self.setMode('agent', true);
                    self.send('Implement the plan above.');
                });
                turn.appendChild(build);
                this.scroll();
            }
        },

        // Edits: applied immediately, reviewed with Keep / Undo ------------------
        applyFiles: function (files, turnId) {
            var self = this;
            files.forEach(function (f) { self.applyFileEvent(f, turnId); });
        },

        // Versions of the turn's changes (one correct, the others with a bug each, in random order):
        // the student reviews them as diffs and applies one.
        // The single version shown ("1 option" setting) had the bug: like correctPick in showVersions,
        // with the correct files from the server (they were never among the options).
        fixSingleVersion: function (turn, fb, ev, turnId) {
            var reveal = fb.mode === 'reveal';
            if (!reveal && Array.isArray(fb.correct_files)) this.applyFiles(fb.correct_files, turnId);
            var onlyId = AgentLog.rowId(ev.pair_id, ev.versions[0].oid);
            AgentLog.set(onlyId, Object.assign({ feedback_shown: reveal ? 'revealed' : 'corrected' },
                (!reveal && Array.isArray(fb.correct_files)) ? { applied_files: AgentLog.filesJson(fb.correct_files) } : {}));
            if (!reveal && Array.isArray(fb.correct_files)) AgentLog.tracked[onlyId] = fb.correct_files;
            log(reveal ? 'wrong_pick_revealed' : 'wrong_pick_corrected', { pair_id: ev.pair_id, option_id: fb.correct_oid,
                single: true, file: fb.filename, bug_type: fb.bug_type || '', explanation: (fb.explanation || '').slice(0, 1000) });
            turn.appendChild(agentFixCard(fb, ev.pair_id, "The agent's changes had a bug",
                reveal ? 'Your files were not changed: fix it as shown.' : SVG.check + 'Switched to the correct changes.'));
            this.scroll();
        },

        showVersions: function (turn, ev, turnId, onApply) {
            var self = this, current = 0, labels = ['A', 'B', 'C', 'D'];
            var box = document.createElement('div');
            box.className = 'agent-versions';
            box.innerHTML =
                '<div class="agent-versions__tabs">' + ev.versions.map(function (v, i) {
                    return '<button type="button" class="agent-versions__tab" data-v="' + i + '">Version ' + labels[i] + '</button>';
                }).join('') + '</div>' +
                '<div class="agent-versions__body"></div>' +
                '<div class="agent-versions__foot"><span class="agent-versions__note">Review ' + (ev.versions.length === 2 ? 'both' : 'all ' + ev.versions.length) + ', then apply one.</span>' +
                '<span class="agent-versions__actions">' +
                '<button type="button" class="agent-versions__btn agent-versions__undo" title="Discard all versions">' + SVG.undo + '<span>Undo</span></button>' +
                '<button type="button" class="agent-versions__btn agent-versions__btn--primary agent-versions__apply">' + SVG.check + '<span></span></button>' +
                '</span></div>';
            var body = box.querySelector('.agent-versions__body'), apply = box.querySelector('.agent-versions__apply');
            function show(i, file) {
                var changedVersion = i !== current || !body.innerHTML;
                current = i;
                var v = ev.versions[i];
                box.querySelectorAll('.agent-versions__tab').forEach(function (b) { b.classList.toggle('is-active', +b.dataset.v === i); });
                body.innerHTML = (v.description ? '<div class="agent-text agent-versions__desc">' + renderMarkdown(v.description) + '</div>' : '') +
                    v.files.map(function (f) {
                        var tag = f.action === 'delete' ? '<span class="agent-del">deleted</span>' : (function () {
                            var c = lineCounts(WS.get(f.filename) || '', f.content || '');
                            return (WS.get(f.filename) === null ? '<span class="agent-add">new</span> ' : '') +
                                '<span class="agent-add">+' + c.added + '</span> <span class="agent-del">-' + c.removed + '</span>';
                        })();
                        return '<button type="button" class="agent-versions__file" data-file="' + esc(f.filename) + '">' + SVG.file +
                            '<span>' + esc(f.filename) + '</span>' + tag + '</button>';
                    }).join('');
                body.querySelectorAll('[data-file]').forEach(function (b) { b.onclick = function () { show(current, b.dataset.file); }; });
                apply.lastChild.textContent = 'Apply Version ' + labels[i];
                if (changedVersion) {
                    var vrow = window.StudyLog && StudyLog.get(AgentLog.rowId(ev.pair_id, v.oid));
                    if (vrow) AgentLog.set(vrow.row_id, { previewed: 'Yes', preview_count: (vrow.preview_count || 0) + 1 });
                    log('agent_version_viewed', { pair_id: ev.pair_id, option_id: v.oid, label: labels[i] });
                    pairEvent(ev.pair_id, v.oid, 'shown');
                }
                Preview.show({ ev: ev, index: i, file: file, labels: labels, onSelect: show });
                if (Preview.state) log('agent_version_file_viewed', { pair_id: ev.pair_id, option_id: v.oid, label: labels[i], filename: Preview.state.file });
            }
            function applyCurrent() {
                var v = ev.versions[current], label = labels[current];
                Preview.hide();
                self.applyFiles(v.files, turnId);
                if (onApply) onApply(v);
                log('agent_version_applied', { pair_id: ev.pair_id, option_id: v.oid, label: label });
                AgentLog.decide(ev, v.oid, '', { accept_method: 'apply_button' });
                self.expireVersions = null;
                var step = document.createElement('div');
                step.className = 'agent-step';
                step.innerHTML = '<span class="agent-step__icon">' + SVG.file + '</span>' +
                    '<span class="agent-step__text">Applied Version ' + label + '</span>';
                box.replaceWith(step);
                pairEvent(ev.pair_id, v.oid, 'accepted').then(function (res) {
                    var fb = res && res.feedback;
                    var right = fb && ev.versions.filter(function (o) { return o.oid === fb.correct_oid; })[0];
                    if (right) correctPick(step, fb, right, label, labels[ev.versions.indexOf(right)]);
                });
            }
            // The applied version had the bug. "Correct wrong picks": the correct one replaces it.
            // "Show correct answer" (fb.mode "reveal"): it is only shown, the files stay as applied.
            function correctPick(step, fb, right, wrongLabel, rightLabel) {
                var reveal = fb.mode === 'reveal';
                if (!reveal) {
                    self.applyFiles(right.files, turnId);
                    if (onApply) onApply(right);
                }
                var wrongId = AgentLog.rowId(ev.pair_id, ev.versions[current].oid);
                AgentLog.set(wrongId, Object.assign({ feedback_shown: reveal ? 'revealed' : 'corrected' },
                                                    reveal ? {} : { applied_files: AgentLog.filesJson(right.files) }));
                if (!reveal) AgentLog.tracked[wrongId] = right.files;
                log(reveal ? 'wrong_pick_revealed' : 'wrong_pick_corrected', { pair_id: ev.pair_id, option_id: fb.correct_oid, wrong_label: wrongLabel,
                                              correct_label: rightLabel, file: fb.filename, bug_type: fb.bug_type || '',
                                              explanation: (fb.explanation || '').slice(0, 1000) });
                step.replaceWith(agentFixCard(fb, ev.pair_id, 'Version ' + wrongLabel + ' had a bug',
                    reveal ? 'Version ' + rightLabel + ' was the correct one. Your files were not changed.'
                           : SVG.check + 'Switched to Version ' + rightLabel + ', the correct one.'));
                self.scroll();
            }
            function undo() {
                // No version is applied: the files stay as they were.
                Preview.hide();
                log('agent_versions_undone', { pair_id: ev.pair_id, label_viewed: labels[current] });
                AgentLog.decide(ev, '', 'rejected');
                ev.versions.forEach(function (v) { pairEvent(ev.pair_id, v.oid, 'rejected'); });
                self.expireVersions = null;
                box.outerHTML = '<div class="agent-step"><span class="agent-step__icon">' + SVG.stop + '</span>' +
                    '<span class="agent-step__text">Undid the changes (no version applied)</span></div>';
            }
            box.querySelectorAll('.agent-versions__tab').forEach(function (b) {
                b.onclick = function () { show(+b.dataset.v, +b.dataset.v === current && Preview.state ? Preview.state.file : undefined); };
            });
            box.querySelector('.agent-versions__undo').onclick = undo;
            apply.onclick = applyCurrent;
            this.expireVersions = function () {
                self.expireVersions = null;
                Preview.hide();
                log('agent_versions_skipped', { pair_id: ev.pair_id });
                AgentLog.decide(ev, '', 'ignored');
                box.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
                box.querySelector('.agent-versions__foot').innerHTML = '<span class="agent-versions__note">Not applied (a new message was sent).</span>';
            };
            this.versionsShow = show;
            turn.appendChild(box);
            show(0);
            this.scroll();
        },

        applyFileEvent: function (ev, turnId) {
            var name = ev.filename;
            if (!(name in this.pending)) this.pending[name] = { baseline: WS.get(name), turn_id: turnId };
            if (ev.action === 'delete') {
                WS.remove(name, { source: 'agent' });
            } else {
                WS.set(name, ev.content, { source: 'agent' });
                WS.open(name);
            }
            log('agent_file_change', { filename: name, action: ev.action, added: ev.added || 0, removed: ev.removed || 0 });
            this.syncBadges();
            this.renderReview();
            this.refreshDecorations();
        },
        syncBadges: function () {
            var self = this;
            Object.keys(WS.badges).forEach(function (n) { if (!self.pending[n]) delete WS.badges[n]; });
            Object.keys(this.pending).forEach(function (n) {
                WS.badges[n] = '<span class="agent-tree-dot" title="Changed by the agent — review in the Agent panel">' +
                    (self.pending[n].baseline === null ? 'U' : 'M') + '</span>';
            });
            WS.render();
        },
        renderReview: function () {
            var self = this, names = Object.keys(this.pending), box = this.els.review;
            box.hidden = !names.length;
            if (!names.length) { box.innerHTML = ''; return; }
            var rows = names.map(function (n) {
                var cur = WS.get(n), p = self.pending[n];
                var counts = cur === null ? null : lineCounts(p.baseline, cur);
                return '<div class="agent-review__row">' +
                    '<button type="button" class="agent-review__file" data-open="' + esc(n) + '">' + SVG.file + '<span>' + esc(n) + '</span></button>' +
                    (cur === null ? '<span class="agent-del">deleted</span>' : '<span class="agent-add">+' + counts.added + '</span><span class="agent-del">-' + counts.removed + '</span>') +
                    '<button type="button" class="agent-link" data-undo="' + esc(n) + '">Undo</button>' +
                    '<button type="button" class="agent-link agent-link--keep" data-keep="' + esc(n) + '">Keep</button></div>';
            }).join('');
            box.innerHTML =
                '<div class="agent-review__head"><button type="button" class="agent-review__toggle" data-toggle="1">' +
                '<span class="agent-review__chev' + (this.reviewOpen ? ' is-open' : '') + '">' + SVG.chevron + '</span>' +
                names.length + ' File' + (names.length > 1 ? 's' : '') + '</button>' +
                '<span class="agent-review__actions"><button type="button" class="agent-btn" data-all="undo">Undo All</button>' +
                '<button type="button" class="agent-btn agent-btn--primary" data-all="keep">Keep All</button></span></div>' +
                (this.reviewOpen ? '<div class="agent-review__list">' + rows + '</div>' : '');
            box.querySelector('[data-toggle]').onclick = function () { self.reviewOpen = !self.reviewOpen; self.renderReview(); };
            box.querySelectorAll('[data-open]').forEach(function (b) { b.onclick = function () { WS.open(b.dataset.open); }; });
            box.querySelectorAll('[data-undo]').forEach(function (b) { b.onclick = function () { self.decide(b.dataset.undo, 'undo'); }; });
            box.querySelectorAll('[data-keep]').forEach(function (b) { b.onclick = function () { self.decide(b.dataset.keep, 'keep'); }; });
            box.querySelectorAll('[data-all]').forEach(function (b) {
                b.onclick = function () { Object.keys(self.pending).forEach(function (n) { self.decide(n, b.dataset.all); }); };
            });
        },
        decide: function (name, decision) {
            var p = this.pending[name];
            if (!p) return;
            delete this.pending[name];
            if (decision === 'undo') {
                if (p.baseline === null) WS.remove(name, { source: 'agent_undo' });
                else WS.set(name, p.baseline, { source: 'agent_undo' });
            }
            log('agent_edit_decision', { filename: name, decision: decision, turn_id: p.turn_id });
            if (decision === 'undo') AgentLog.reverted(name);
            this.syncBadges();
            this.renderReview();
            this.refreshDecorations();
        },
        refreshDecorations: function () {
            if (!this.monaco) return;
            var self = this, monaco = this.monaco;
            WS.files.forEach(function (f) {
                var p = self.pending[f.name];
                var lines = !p ? [] : p.baseline === null
                    ? f.model.getValue().split('\n').map(function (_, i) { return i + 1; })
                    : changedLines(p.baseline, f.model.getValue());
                self.decorations[f.name] = f.model.deltaDecorations(self.decorations[f.name] || [], lines.map(function (ln) {
                    return { range: new monaco.Range(ln, 1, ln, 1), options: { isWholeLine: true, className: 'agent-line-added', linesDecorationsClassName: 'agent-line-gutter' } };
                }));
            });
        }
    };

    var STYLES = [
        '.agent-panel{display:flex;flex-direction:column;padding:0!important;min-height:0;height:100%;position:relative;font-family:var(--font-ui-sans);}',
        '.agent-panel[hidden]{display:none!important;}',
        '.agent-head{display:flex;align-items:center;gap:2px;height:36px;padding:0 6px 0 12px;border-bottom:1px solid var(--ide-border,#2b2b2b);flex-shrink:0;}',
        '.agent-head__title{flex:1;min-width:0;font:var(--fw-medium) var(--fs-md)/1.4 var(--ui-font);color:var(--ui-text-strong);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
        '.agent-ibtn{width:24px;height:24px;border:0;border-radius:var(--radius-sm);background:transparent;color:var(--ui-text);display:inline-flex;align-items:center;justify-content:center;cursor:pointer;font-size:16px;transition:background-color var(--dur-fast) var(--ease-out);}',
        '.agent-ibtn svg{width:16px;height:16px;}',
        '.agent-ibtn:hover{background:#2a2d2e;}',
        '.agent-body{flex:1;min-height:0;display:flex;flex-direction:column;}',
        '.agent-body--empty{flex-direction:column-reverse;justify-content:flex-end;}',
        '.agent-messages{flex:1;min-height:0;overflow-y:auto;padding:12px 12px 4px;display:flex;flex-direction:column;gap:10px;}',
        '.agent-body--empty .agent-messages{display:none;}',
        '.agent-dock{position:relative;padding:8px 10px 10px;flex-shrink:0;}',
        '.agent-body--empty .agent-dock{padding-top:12px;}',
        '.agent-composer{background:var(--ui-surface);border:1px solid var(--ui-border-strong);border-radius:var(--radius-xl);padding:8px 8px 6px 10px;transition:border-color var(--dur) var(--ease-out),box-shadow var(--dur) var(--ease-out);}',
        '.agent-composer:focus-within{border-color:#5a5a5a;box-shadow:0 0 0 3px rgba(255,255,255,.03);}',
        '.agent-composer textarea{display:block;width:100%;min-height:22px;max-height:200px;resize:none;border:0;outline:none;background:transparent;color:#e7e7e7;font:13px/1.5 var(--font-ui-sans);padding:2px 0 6px;}',
        '.agent-body--empty .agent-composer textarea{min-height:88px;}',
        '.agent-composer textarea::placeholder{color:#6f6f6f;}',
        '.agent-composer__row{display:flex;align-items:center;gap:6px;}',
        '.agent-mode-pill{display:inline-flex;align-items:center;gap:4px;height:var(--h-btn-sm);padding:0 6px 0 8px;border:0;border-radius:999px;background:#2b2b2b;color:var(--ui-text);font:var(--fw-medium) var(--fs-sm)/1 var(--ui-font);cursor:pointer;transition:background-color var(--dur-fast) var(--ease-out),color var(--dur) var(--ease-out);}',
        '.agent-mode-pill:hover{background:#353535;}',
        '.agent-mode-pill svg{width:13px;height:13px;}',
        '.agent-mode-pill[data-mode=plan]{color:#e5c07b;}',
        '.agent-mode-pill[data-mode=debug]{color:#e06c75;}',
        '.agent-mode-pill[data-mode=ask]{color:#61afef;}',
        '.agent-model{font:var(--fs-sm)/1 var(--ui-font);color:var(--ui-text-muted);margin-right:auto;}',
        '.agent-send{width:var(--h-btn);height:var(--h-btn);border:0;border-radius:50%;background:var(--ui-text-strong);color:var(--ui-bg);display:inline-flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:background-color var(--dur-fast) var(--ease-out),transform var(--dur-fast) var(--ease-out);}',
        '.agent-send svg{width:15px;height:15px;}',
        '.agent-send:hover{background:#fff;}','.agent-send:active{transform:scale(.92);}',
        '.agent-send--stop{background:#3c3c3c;color:#e7e7e7;}',
        '.agent-chips{display:flex;flex-wrap:wrap;gap:4px;}',
        '.agent-chips:not(:empty){margin-bottom:6px;}',
        '.agent-chip-tag{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 4px 0 6px;border:1px solid #3c3c3c;border-radius:5px;background:#252526;color:#cccccc;font-size:12px;}',
        '.agent-chip-tag svg{width:11px;height:11px;color:#e5c07b;}',
        '.agent-chip-tag button{border:0;background:none;color:#8b8b8b;cursor:pointer;font-size:13px;padding:0 2px;}',
        '.agent-popup{position:absolute;left:10px;right:10px;bottom:calc(100% - 6px);max-height:260px;overflow-y:auto;background:var(--ui-raised);border:1px solid var(--ui-border-strong);border-radius:var(--radius-lg);box-shadow:var(--shadow-pop);padding:4px;z-index:20;animation:ui-pop-in var(--dur-fast) var(--ease-out);transform-origin:bottom center;}',
        '.agent-body--empty .agent-popup{bottom:auto;top:calc(100% - 4px);animation-name:ui-pop-in-down;transform-origin:top center;}',
        '.agent-popup__title{font:var(--fs-xs)/1 var(--ui-font);color:var(--ui-text-muted);padding:6px 8px 4px;}',
        '.agent-popup__item{display:flex;align-items:center;gap:8px;height:28px;padding:0 8px;border-radius:var(--radius-sm);cursor:pointer;font:var(--fs-md)/1 var(--ui-font);color:var(--ui-text);transition:background-color 80ms linear;}',
        '.agent-popup__item.is-active,.agent-popup__item:hover{background:#04395e;color:#fff;}',
        '.agent-popup__icon{width:14px;height:14px;display:inline-flex;flex-shrink:0;}',
        '.agent-popup__icon svg{width:14px;height:14px;}',
        '.agent-popup__label{white-space:nowrap;}',
        '.agent-popup__desc{color:var(--ui-text-muted);font-size:var(--fs-sm);margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
        '.agent-popup__item.is-active .agent-popup__desc{color:#c5c5c5;}',
        '.agent-popup__check{color:#cccccc;margin-left:4px;}',
        '.agent-user{background:var(--ui-raised);border:1px solid #333;border-radius:var(--radius-lg);padding:8px 10px;color:var(--ui-text-strong);font:var(--fs-md)/1.5 var(--ui-font);white-space:pre-wrap;word-wrap:break-word;animation:ui-rise-in var(--dur) var(--ease-out) both;}',
        '.agent-turn{display:flex;flex-direction:column;gap:6px;padding:0 2px;}','.agent-turn>*{animation:ui-rise-in var(--dur) var(--ease-out) both;}',
        '.agent-text{font:var(--fs-md)/1.6 var(--ui-font);color:var(--ui-text);white-space:pre-wrap;word-wrap:break-word;}',
        '.agent-text code{background:#2b2b2b;border-radius:4px;padding:1px 4px;font-family:var(--font-code-mono);font-size:12px;color:#e7e7e7;}',
        '.agent-code{background:#181818;border:1px solid #2b2b2b;border-radius:6px;padding:8px 10px;overflow-x:auto;white-space:pre;font:12px/1.5 var(--font-code-mono);color:#d4d4d4;margin:4px 0;}',
        '.agent-code code{background:none;padding:0;}',
        '.agent-h{font-weight:600;color:#e7e7e7;margin-top:6px;}',
        '.agent-li{display:flex;gap:6px;padding-left:4px;}',
        '.agent-li>span:first-child{color:#8b8b8b;flex-shrink:0;}',
        '.agent-step{display:flex;align-items:flex-start;gap:7px;font:var(--fs-sm)/1.5 var(--ui-font);color:#9d9d9d;}',
        '.agent-step__icon{width:14px;height:14px;margin-top:2px;flex-shrink:0;display:inline-flex;color:#8b8b8b;}',
        '.agent-step__icon svg{width:14px;height:14px;}',
        '.agent-step__text{min-width:0;flex:1;}',
        '.agent-step details summary{cursor:pointer;list-style:none;}',
        '.agent-step details summary::-webkit-details-marker{display:none;}',
        '.agent-step--err{color:#f48771;}',
        '.agent-versions{border:1px solid #3c3c3c;border-radius:8px;background:#1e1e1e;margin:6px 0;overflow:hidden;}',
        '.agent-versions__tabs{display:flex;gap:4px;padding:6px 8px 0;}',
        '.agent-versions__tab{background:transparent;border:1px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;color:#9d9d9d;font:600 var(--fs-sm)/1 var(--ui-font);padding:7px 12px;cursor:pointer;}',
        '.agent-versions__tab:hover{color:#e0e0e0;}',
        '.agent-versions__tab.is-active{background:#252526;border-color:#3c3c3c;color:#ffffff;}',
        '.agent-versions__tab:disabled{cursor:default;}',
        '.agent-versions__body{background:#252526;border-top:1px solid #3c3c3c;max-height:420px;overflow:auto;}',
        '.agent-versions__desc{padding:8px 10px;border-bottom:1px solid #3c3c3c;}',
        '.agent-versions__file{display:flex;align-items:center;gap:6px;width:100%;padding:6px 10px;border:0;background:transparent;color:#cccccc;font:var(--fs-sm)/1.3 var(--ui-font);cursor:pointer;text-align:left;}',
        '.agent-versions__file:hover{background:#2a2d2e;}',
        '.agent-versions__file svg,.agent-preview__file svg{width:14px;height:14px;flex-shrink:0;}',
        '.agent-preview__version{font:var(--fw-medium) var(--fs-sm)/1 var(--ui-font);color:#fff;background:#04395e;border-radius:4px;padding:4px 8px;}',
        '.agent-preview{position:absolute;inset:0;z-index:30;display:flex;flex-direction:column;background:#1e1e1e;}',
        '.agent-preview[hidden]{display:none;}',
        '.agent-preview__bar{flex-shrink:0;background:#252526;border-bottom:1px solid #3c3c3c;}',
        '.agent-preview__row{display:flex;align-items:center;gap:8px;padding:5px 8px;flex-wrap:wrap;}',
        '.agent-preview__title{display:inline-flex;align-items:center;gap:6px;font:var(--fw-medium) var(--fs-sm)/1 var(--ui-font);color:var(--ui-text-strong);margin-right:4px;}',
        '.agent-preview__title svg{width:14px;height:14px;}',
        '.agent-preview__nav{display:inline-flex;align-items:center;gap:4px;margin-left:auto;color:var(--ui-text-muted);font:var(--fs-sm)/1 var(--ui-font);}',
        '.agent-preview__nav button{width:24px;height:24px;border:0;border-radius:4px;background:transparent;color:#cccccc;cursor:pointer;}',
        '.agent-preview__nav button:hover:not(:disabled){background:#2a2d2e;}',
        '.agent-preview__nav button:disabled{opacity:.35;cursor:default;}',
        '.agent-preview__files{display:flex;gap:2px;padding:0 8px;overflow-x:auto;}',
        '.agent-preview__file{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border:0;border-bottom:2px solid transparent;background:transparent;color:#a0a0a0;font:var(--fs-sm)/1 var(--ui-font);cursor:pointer;white-space:nowrap;}',
        '.agent-preview__file:hover{color:#e7e7e7;}',
        '.agent-preview__file.is-active{color:#fff;border-bottom-color:#0078d4;}',
        '.agent-preview__diff{flex:1;min-height:0;}',
        '.agent-versions__file svg{width:13px;height:13px;flex-shrink:0;}',
        // Footer actions in the VS Code / Cursor style: compact 24px buttons, a quiet secondary, a solid primary.
        '.agent-versions__foot{display:flex;align-items:center;gap:8px;padding:6px 6px 6px 10px;border-top:1px solid #2b2b2b;}',
        '.agent-versions__note{flex:1;min-width:0;font:12px/1.4 var(--ui-font);color:#8b8b8b;}',
        '.agent-versions__actions{display:flex;gap:4px;flex-shrink:0;}',
        '.agent-versions__btn{display:inline-flex;align-items:center;gap:4px;height:24px;padding:0 8px 0 6px;border:0;border-radius:4px;background:rgba(255,255,255,.06);color:#cccccc;font:500 12px/1 var(--ui-font);white-space:nowrap;cursor:pointer;transition:background-color var(--dur-fast) var(--ease-out),color var(--dur-fast) var(--ease-out);}',
        '.agent-versions__btn svg{width:14px;height:14px;flex-shrink:0;}',
        '.agent-versions__btn:hover{background:rgba(255,255,255,.1);color:#ffffff;}',
        '.agent-versions__btn:active{background:rgba(255,255,255,.14);}',
        '.agent-versions__btn--primary{background:#0078d4;color:#ffffff;}',
        '.agent-versions__btn--primary:hover{background:#026ec1;}',
        '.agent-versions__btn--primary:active{background:#005ba4;}',
        '.agent-versions__btn:focus-visible{outline:1px solid var(--ui-focus);outline-offset:2px;}',
        '.agent-step--muted{color:#6f6f6f;}',
        '.agent-file{color:#cccccc;font-family:var(--font-code-mono);font-size:12px;}',
        '.agent-file--link{background:none;border:0;padding:0;cursor:pointer;text-decoration:underline;text-decoration-color:#555;text-underline-offset:2px;}',
        '.agent-file--link:hover{color:#fff;}',
        '.agent-ok{color:#89d185;}.agent-err{color:#f48771;}',
        '.agent-add{color:#89d185;font-family:var(--font-code-mono);font-size:12px;}',
        '.agent-del{color:#f48771;font-family:var(--font-code-mono);font-size:12px;}',
        '.agent-spinner{display:inline-block;width:11px;height:11px;border:1.5px solid #555;border-top-color:#cccccc;border-radius:50%;animation:agentspin .8s linear infinite;}',
        '@keyframes agentspin{to{transform:rotate(360deg);}}',
        '.agent-shimmer{background:linear-gradient(90deg,#6f6f6f 0%,#e7e7e7 50%,#6f6f6f 100%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:agentshim 1.6s linear infinite;}',
        '@keyframes agentshim{from{background-position:200% 0;}to{background-position:-200% 0;}}',
        '.agent-btn{display:inline-flex;align-items:center;gap:5px;height:var(--h-btn);padding:0 12px;border:1px solid var(--ui-border-strong);border-radius:var(--radius);background:transparent;color:var(--ui-text);font:var(--fw-medium) var(--fs-sm)/1 var(--ui-font);cursor:pointer;transition:background-color var(--dur-fast) var(--ease-out),transform var(--dur-fast) var(--ease-out);}','.agent-btn:active{transform:scale(.97);}',
        '.agent-btn svg{width:13px;height:13px;}',
        '.agent-btn:hover{background:#2a2d2e;}',
        '.agent-btn--primary{background:var(--ui-accent);border-color:transparent;color:#fff;}',
        '.agent-btn--primary:hover{background:var(--ui-accent-hover);}',
        '.agent-build{align-self:flex-start;margin-top:4px;}',
        '.agent-link{background:none;border:0;color:var(--ui-text-muted);cursor:pointer;font:var(--fs-sm)/1 var(--ui-font);padding:4px 6px;border-radius:var(--radius-sm);transition:background-color var(--dur-fast),color var(--dur-fast);}',
        '.agent-link:hover{color:#e7e7e7;background:#2a2d2e;}',
        '.agent-link--keep{color:#cccccc;}',
        '.agent-review{border:1px solid var(--ui-border-strong);border-bottom:0;border-radius:var(--radius-lg) var(--radius-lg) 0 0;margin:0 8px;background:#1c1c1c;padding:4px 6px;font:var(--fs-sm)/1.4 var(--ui-font);color:var(--ui-text);animation:ui-rise-in var(--dur) var(--ease-out);}',
        '.agent-review+.agent-composer{border-top-left-radius:12px;border-top-right-radius:12px;}',
        '.agent-review__head{display:flex;align-items:center;justify-content:space-between;gap:6px;}',
        '.agent-review__toggle{display:inline-flex;align-items:center;gap:4px;border:0;background:none;color:#cccccc;font:12px var(--font-ui-sans);cursor:pointer;padding:2px;}',
        '.agent-review__chev{display:inline-flex;transform:rotate(-90deg);transition:transform .15s;}',
        '.agent-review__chev.is-open{transform:none;}',
        '.agent-review__chev svg{width:12px;height:12px;}',
        '.agent-review__actions{display:flex;gap:4px;}',
        '.agent-review__actions .agent-btn{height:var(--h-btn-sm);padding:0 8px;}',
        '.agent-review__list{margin-top:4px;border-top:1px solid var(--ui-border);padding-top:2px;animation:ui-fade-in var(--dur) var(--ease-out);}',
        '.agent-review__row{display:flex;align-items:center;gap:6px;padding:2px 0;}',
        '.agent-review__file{display:inline-flex;align-items:center;gap:5px;border:0;background:none;color:#cccccc;cursor:pointer;font:12px var(--font-code-mono);margin-right:auto;padding:2px;min-width:0;}',
        '.agent-review__file svg{width:13px;height:13px;flex-shrink:0;color:#8b8b8b;}',
        '.agent-review__file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
        '.agent-review__file:hover{color:#fff;}',
        '.agent-sheet{position:absolute;inset:36px 0 0 0;background:var(--ui-bg);z-index:30;overflow-y:auto;padding:0 12px 12px;animation:ui-rise-in var(--dur) var(--ease-out);}',
        '.agent-sheet__head{display:flex;align-items:center;justify-content:space-between;height:36px;font:var(--fw-semibold) var(--fs-md)/1 var(--ui-font);color:var(--ui-text-strong);}',
        '.agent-sheet__hint{font:var(--fs-sm)/1.5 var(--ui-font);color:var(--ui-text-muted);margin:0 0 10px;}',
        '.agent-sheet__hint code{background:#2b2b2b;padding:0 4px;border-radius:3px;}',
        '.agent-skill{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid #2b2b2b;}',
        '.agent-skill__main{flex:1;min-width:0;}',
        '.agent-skill__name{font:var(--fw-medium) var(--fs-md)/1.4 var(--ui-font);color:var(--ui-text-strong);}',
        '.agent-skill__tag{font-size:10px;color:#8b8b8b;border:1px solid #3c3c3c;border-radius:4px;padding:0 4px;margin-left:4px;}',
        '.agent-skill__text{font:var(--fs-sm)/1.5 var(--ui-font);color:var(--ui-text-muted);white-space:pre-wrap;margin-top:2px;}',
        '.agent-switch{position:relative;width:28px;height:16px;flex-shrink:0;margin-top:2px;}',
        '.agent-switch input{opacity:0;width:0;height:0;position:absolute;}',
        '.agent-switch span{position:absolute;inset:0;background:#3c3c3c;border-radius:16px;cursor:pointer;transition:background .15s;}',
        '.agent-switch span::after{content:"";position:absolute;left:2px;top:2px;width:12px;height:12px;border-radius:50%;background:#cccccc;transition:transform .15s;}',
        '.agent-switch input:checked+span{background:var(--ui-accent);}',
        '.agent-switch input:checked+span::after{transform:translateX(12px);background:#fff;}',
        '.agent-skill-new{display:flex;flex-direction:column;gap:6px;margin:10px 0;}',
        '.agent-skill-new input,.agent-skill-new textarea{width:100%;box-sizing:border-box;background:#1f1f1f;border:1px solid #3c3c3c;color:#e7e7e7;border-radius:6px;padding:6px 8px;font:13px var(--font-ui-sans);outline:none;}',
        '.agent-skill-new input:focus,.agent-skill-new textarea:focus{border-color:#0078d4;}',
        '.agent-skill-new__row{display:flex;justify-content:flex-end;gap:6px;}',
        '.agent-line-added{background:rgba(137,209,133,.10);transition:background-color var(--dur-slow);}',
        '.agent-line-gutter{background:#89d185;width:3px!important;margin-left:3px;}',
        '.agent-line-removed{background:rgba(244,135,113,.14);}',
        '.agent-line-gutter--del{background:#f48771;width:3px!important;margin-left:3px;}'
    ].join('\n');

    window.AIModes = {
        // The agent lives in its own right-hand card, so the Problem tab stays as index.html set it.
        onProblemTabsReady: function () {},
        init: function (monaco, editor) {
            var condition = Number(currentSession.condition);
            var historyTab = document.getElementById('problemTabAiHistory');
            if (historyTab) historyTab.style.display = 'none'; // legacy interpretation history is retired

            if (condition === CONDITION.SUGGESTION || condition === CONDITION.MULTILINE_SUGGESTION) {
                initGhostText(monaco, editor);
            } else if (condition === CONDITION.AGENT) {
                var style = document.createElement('style');
                style.textContent = STYLES;
                document.head.appendChild(style);
                Agent.init(monaco, editor);
            }
        }
    };
})();
