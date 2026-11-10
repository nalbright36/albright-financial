// The header's notification bell (templates/index.html, labs/launcher.css; the rules: albright_labs.activity).
// The button toggles the panel; each time it opens, the panel's recent notifications are fetched fresh from
// data-panel-url (albright_labs.activity_views.panel). Tapping one opens the thing and marks it read (a plain link).
// "Mark all read" posts in place and clears the count. Escape, a click outside or Tab moving focus out closes it
// (Escape puts focus back on the bell); only one header menu is open at a time (the inline script's navDropdowns).
// While open, --notif-top holds where the panel starts on screen so it fits and scrolls inside, like the launcher.
(function () {
    var bell = document.getElementById('notif-bell');
    if (!bell) return;
    var button = document.getElementById('notif-bell-button');
    var panel = document.getElementById('notif-panel');
    var count = document.getElementById('notif-bell-count');

    function isOpen() { return bell.classList.contains('open'); }

    function fit() {
        if (!isOpen()) return;
        panel.style.setProperty('--notif-top', Math.max(0, Math.round(panel.getBoundingClientRect().top)) + 'px');
    }

    function setCount(n) {
        n = parseInt(n, 10) || 0;
        count.textContent = n > 99 ? '99+' : String(n);
        count.hidden = n === 0;
        button.setAttribute('aria-label', 'Notifications' + (n ? ' - ' + n + ' unread' : ''));
    }

    function close(refocus) {
        if (!isOpen()) return;
        bell.classList.remove('open');
        panel.hidden = true;
        button.setAttribute('aria-expanded', 'false');
        if (refocus) button.focus();
    }

    function load() {
        fetch(bell.dataset.panelUrl, { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
            .then(function (html) {
                panel.innerHTML = html;
                var list = panel.querySelector('[data-unread]');
                if (list) setCount(list.getAttribute('data-unread'));
                fit();
            })
            .catch(function () {
                panel.innerHTML = '<p class="notif-panel__empty">Couldn’t load notifications. <a href="' +
                    bell.dataset.activityUrl + '">Open Activity</a></p>';
            });
    }

    function open() {
        if (window.navDropdowns) window.navDropdowns.forEach(function (closeOther) { closeOther(); });
        var nav = bell.closest('.navbar'), toggle = document.getElementById('nav-toggle');
        if (nav && toggle && nav.classList.contains('navbar--menu-open')) toggle.click();   // the phone Menu
        bell.classList.add('open');
        panel.hidden = false;
        fit();
        panel.scrollTop = 0;
        button.setAttribute('aria-expanded', 'true');
        load();
    }
    if (window.navDropdowns) window.navDropdowns.push(function () { close(false); });

    button.addEventListener('click', function (e) {
        e.stopPropagation();
        if (isOpen()) close(true); else open();
    });
    bell.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && isOpen()) { e.preventDefault(); e.stopPropagation(); close(true); }
    });
    panel.addEventListener('submit', function (e) {
        var form = e.target.closest('[data-notif-readall]');
        if (!form) return;
        e.preventDefault();
        fetch(form.action, { method: 'POST', credentials: 'same-origin', body: new FormData(form),
                             headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' } })
            .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
            .then(function () {
                setCount(0);
                panel.querySelectorAll('.act-row.is-unread').forEach(function (row) {
                    row.classList.remove('is-unread');
                    var dot = row.querySelector('.act-row__dot');
                    if (dot) dot.remove();
                });
                form.remove();
            })
            .catch(function () { form.submit(); });
    });
    bell.addEventListener('focusout', function (e) {
        if (e.relatedTarget && !bell.contains(e.relatedTarget)) close(false);
    });
    document.addEventListener('click', function (e) {
        if (!bell.contains(e.target)) close(false);
    });
    window.addEventListener('resize', fit);
})();
