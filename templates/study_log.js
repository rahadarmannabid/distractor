// Study log: the per-condition tables instructors download (study_log.py on the server).
// Rows are kept in localStorage while the participant works on a step and sent to /study-log when
// they move to another step (StudyLog.endStep: next problem, survey or submit). Whatever is left
// unsent (a closed tab, a failed request) is sent on the next page load, and as a beacon when the
// page closes. Rows are sent whole and keyed by row_id, so sending one again only overwrites it.
//
// Tables: "autocomplete" (one row per option of an inline suggestion), "agent" (one row per code
// version of an agent response) and "compilation" (one row per run). Which option is correct or
// buggy is never known here (it is hidden from students); the server adds it when exporting.
// Relies on globals from index.html: currentSession.
(function () {
    var state = null; // { rows: {id: row}, dirty: {id: 1}, idx: {problem: n}, lastAccepted: id }
    var finalizers = [];
    var sending = null;
    var SEND_CHUNK = 200;

    function storageKey() {
        return currentSession && currentSession.sessionId ? 'study_log_v1_' + currentSession.sessionId : null;
    }
    function load() {
        if (state) return state;
        state = { rows: {}, dirty: {}, idx: {}, lastAccepted: '' };
        try {
            var raw = storageKey() && localStorage.getItem(storageKey());
            if (raw) state = Object.assign(state, JSON.parse(raw));
        } catch (e) { /* keep the empty state */ }
        return state;
    }
    function save() {
        try { if (storageKey()) localStorage.setItem(storageKey(), JSON.stringify(state)); } catch (e) { /* full or blocked */ }
    }
    function problemIndex() {
        return typeof currentSession.currentProblemIndex === 'number' ? currentSession.currentProblemIndex : 0;
    }

    var StudyLog = {
        /** Merges `fields` into the row (created with its table, problem and time), and marks it to send. */
        upsert: function (table, id, fields) {
            var s = load();
            var row = s.rows[id];
            if (!row) {
                row = s.rows[id] = { table: table, row_id: id, problem_index: problemIndex(),
                                     timestamp: new Date().toISOString(),
                                     language: currentSession.programmingLanguage || '' };
            }
            Object.assign(row, fields || {});
            s.dirty[id] = 1;
            save();
            return row;
        },
        get: function (id) { return load().rows[id] || null; },
        rows: function (filter) {
            var s = load();
            return Object.keys(s.rows).map(function (k) { return s.rows[k]; }).filter(filter || function () { return true; });
        },
        /** The next interaction number on the current problem (1, 2, …). */
        nextIndex: function () {
            var s = load(), p = String(problemIndex());
            s.idx[p] = (s.idx[p] || 0) + 1;
            save();
            return s.idx[p];
        },
        id: function (prefix) { return prefix + '_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); },
        /** The suggestion or agent version accepted most recently (linked to the next run). */
        setLastAccepted: function (rowId) { load().lastAccepted = rowId; save(); },
        lastAccepted: function () { var s = load(); return s.lastAccepted ? s.rows[s.lastAccepted] || null : null; },
        /** "Show explanation" opened on a wrong-pick card: marks the accepted option of that suggestion. */
        explanationOpened: function (pairId) {
            this.rows(function (r) {
                return (r.suggestion_set_id === pairId || r.response_id === pairId) && r.suggestion_status === 'accepted';
            }).forEach(function (r) { StudyLog.upsert(r.table, r.row_id, { explanation_clicked: 'Yes' }); });
        },
        /** fn() runs before the rows are sent at the end of a step (e.g. to check edits to accepted code). */
        addFinalizer: function (fn) { finalizers.push(fn); },

        /** Sends every row not sent yet; rows of earlier problems are then dropped from local storage. */
        flush: function () {
            if (sending) return sending.then(function () { return StudyLog.flush(); });
            var s = load(), ids = Object.keys(s.dirty);
            if (!ids.length || !currentSession.sessionId) return Promise.resolve(true);
            var chunks = [];
            for (var i = 0; i < ids.length; i += SEND_CHUNK) chunks.push(ids.slice(i, i + SEND_CHUNK));
            sending = chunks.reduce(function (p, chunk) {
                return p.then(function (ok) {
                    var rows = chunk.map(function (id) { return s.rows[id]; }).filter(Boolean);
                    return fetch('/study-log', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
                        body: JSON.stringify({ session_id: currentSession.sessionId, rows: rows })
                    }).then(function (res) { return res.ok ? res.json() : null; }).then(function (data) {
                        if (!data || !data.stored) return false;
                        // Sent: unmark (unless the row changed again meanwhile, then it stays marked).
                        data.stored.forEach(function (id) {
                            var sentRow = rows.find(function (r) { return r.row_id === id; });
                            if (sentRow && s.rows[id] === sentRow) delete s.dirty[id];
                        });
                        return ok;
                    }).catch(function () { return false; });
                });
            }, Promise.resolve(true)).then(function (ok) {
                var current = problemIndex();
                Object.keys(s.rows).forEach(function (id) {
                    if (!s.dirty[id] && s.rows[id].problem_index < current && id !== s.lastAccepted) delete s.rows[id];
                });
                save();
                sending = null;
                return ok && !Object.keys(s.dirty).length;
            });
            return sending;
        },
        /** The participant leaves this step: settle the open rows and send everything. */
        endStep: function () {
            finalizers.forEach(function (fn) { try { fn(); } catch (e) { console.warn('study log finalizer', e); } });
            return StudyLog.flush();
        },
        /** Last resort when the page closes: the unsent rows as one beacon (no answer is awaited). */
        beacon: function () {
            var s = load(), ids = Object.keys(s.dirty);
            if (!ids.length || !navigator.sendBeacon || !currentSession.sessionId) return;
            var rows = ids.slice(0, SEND_CHUNK).map(function (id) { return s.rows[id]; });
            navigator.sendBeacon('/study-log', new Blob([JSON.stringify({ session_id: currentSession.sessionId, rows: rows })],
                                                       { type: 'text/plain' }));
        }
    };
    window.StudyLog = StudyLog;

    window.addEventListener('pagehide', function () {
        finalizers.forEach(function (fn) { try { fn(); } catch (e) { /* closing */ } });
        StudyLog.beacon();
    });
    // Rows a previous page could not send (closed tab, offline) go out once the session is known.
    var tries = 0;
    (function sendLeftovers() {
        if (typeof currentSession !== 'undefined' && currentSession && currentSession.sessionId) { state = null; StudyLog.flush(); return; }
        if (++tries < 40) setTimeout(sendLeftovers, 500);
    })();
})();
