// "Show in my navigation" (albright_labs/_app_settings.html): the up/down
// buttons move a row in place instead of submitting (the hidden "order"
// inputs follow the rows, so Save keeps the new order), and the last
// ticked app can't be unticked - at least one app stays in the nav.
(function () {
    var list = document.getElementById('nav-order');
    if (!list) return;
    var note = document.getElementById('nav-form-note');

    function refresh() {
        var rows = list.querySelectorAll('.labs-nav-row');
        rows.forEach(function (row, i) {
            row.querySelector('[data-move="up"]').disabled = i === 0;
            row.querySelector('[data-move="down"]').disabled = i === rows.length - 1;
        });
    }

    list.addEventListener('click', function (e) {
        var button = e.target.closest('[data-move]');
        if (!button) return;
        e.preventDefault();
        var row = button.closest('.labs-nav-row');
        if (button.dataset.move === 'up' && row.previousElementSibling) {
            list.insertBefore(row, row.previousElementSibling);
        } else if (button.dataset.move === 'down' && row.nextElementSibling) {
            list.insertBefore(row.nextElementSibling, row);
        }
        refresh();
        var again = row.querySelector('[data-move="' + button.dataset.move + '"]');
        (again.disabled ? row.querySelector('[data-move]:not([disabled])') : again).focus();
        if (note) note.textContent = 'Moved - press Save to keep this order.';
    });

    list.addEventListener('change', function (e) {
        if (e.target.name !== 'visible' || e.target.checked) return;
        if (!list.querySelector('input[name="visible"]:checked')) {
            e.target.checked = true;
            if (note) note.textContent = 'Keep at least one app in your navigation.';
        }
    });
})();
