/*
 * Attachments on Planner tasks and notes (albright_labs/_attachments.html; the rules: albright_labs.attachments).
 * Vanilla JS, event delegation on document - so a section the Planner loads into its view-panel dialog works too.
 *
 *   Add files     the file input (multiple; on a phone "image/*" offers camera / photo library / Files) uploads
 *                 each file with a progress bar, without leaving the page. XMLHttpRequest, not fetch: fetch
 *                 can't report upload progress.
 *   drop          dragging files over a section (desktop) highlights it; dropping uploads them
 *   viewer        a thumbnail opens a full-screen viewer: arrows / swipe between the section's images, X or Esc
 *                 closes
 *   remove        the trash button asks "Remove?" in place; Remove deletes it, Cancel (or Esc) puts it back
 *   storage       "312 MB of 1 GB used" under Add files follows each upload and remove (the JSON's storage) -
 *                 amber at 80%, red at 100%, where Add files turns off until something is removed
 *
 * window.Attachments.upload(section, file, onDone) is the same upload for other scripts - Notes uses it to put
 * a pasted or dropped image in the note (notes.js).
 */
(function () {
    "use strict";

    function csrfToken(form) {
        var input = form && form.querySelector("input[name=csrfmiddlewaretoken]");
        if (input) return input.value;
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }

    function parts(section) {
        return {
            form: section.querySelector("[data-att-form]"),
            grid: section.querySelector("[data-att-grid]"),
            files: section.querySelector("[data-att-files]"),
            count: section.querySelector("[data-att-count]"),
            progress: section.querySelector("[data-att-progress]"),
            error: section.querySelector("[data-att-error]")
        };
    }

    function setCount(section, n) {
        var p = parts(section);
        section.dataset.count = n;
        if (p.count) p.count.textContent = n ? String(n) : "";
        if (p.grid) p.grid.hidden = !p.grid.children.length;
        if (p.files) p.files.hidden = !p.files.children.length;
    }

    // ---------- Storage line: it's the person's, so every section on the page shows the same ----------
    var FULL_TITLE = "Your storage is full - delete some attachments to add more.";

    function setStorage(storage) {
        if (!storage) return;
        document.querySelectorAll("[data-att-storage]").forEach(function (line) {
            var label = line.querySelector("[data-att-storage-label]");
            var full = line.querySelector("[data-att-storage-full]");
            if (label) label.textContent = storage.label;
            if (full) full.hidden = !storage.full;
            line.className = "att__storage att__storage--" + storage.status;
        });
        document.querySelectorAll("[data-att-btn]").forEach(function (btn) {
            var input = btn.querySelector("[data-att-input]");
            btn.classList.toggle("is-disabled", !!storage.full);
            if (storage.full) btn.title = FULL_TITLE; else btn.removeAttribute("title");
            if (input) input.disabled = !!storage.full;
        });
    }

    function showError(section, text) {
        var p = parts(section);
        if (p.error) p.error.textContent = text || "";
    }

    function addRows(section, rows) {
        var p = parts(section);
        rows.forEach(function (row) {
            var holder = document.createElement("ul");
            holder.innerHTML = row.html;
            var li = holder.querySelector("li");
            if (!li) return;
            var list = row.is_image ? p.grid : p.files;
            if (list) list.appendChild(li);
        });
    }

    function changed(section, action, extra) {
        var detail = extra || {};
        detail.action = action;
        section.dispatchEvent(new CustomEvent("att:changed", {bubbles: true, detail: detail}));
    }

    // ---------- Upload (one file per request, so each has its own progress) ----------
    var queue = [];
    var busy = false;

    function upload(section, file, onDone) {
        queue.push({section: section, file: file, onDone: onDone});
        if (!busy) next();
    }

    function next() {
        var job = queue.shift();
        if (!job) { busy = false; return; }
        busy = true;
        var section = job.section, file = job.file, p = parts(section);
        var max = parseInt(section.dataset.maxBytes, 10) || 0;
        if (max && file.size > max) {
            showError(section, "“" + file.name + "” is over the " + Math.round(max / 1048576) + " MB limit.");
            if (job.onDone) job.onDone(null);
            return next();
        }
        var data = new FormData();
        ["parent", "id", "csrfmiddlewaretoken"].forEach(function (name) {
            var input = p.form.querySelector("input[name=" + name + "]");
            if (input) data.append(name, input.value);
        });
        data.append("file", file, file.name);
        var xhr = new XMLHttpRequest();
        xhr.open("POST", p.form.action);
        xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        xhr.setRequestHeader("X-CSRFToken", csrfToken(p.form));
        xhr.setRequestHeader("Accept", "application/json");
        var bar = p.progress && p.progress.querySelector("progress");
        var label = p.progress && p.progress.querySelector("[data-att-progress-text]");
        if (p.progress) { p.progress.hidden = false; bar.value = 0; label.textContent = "Uploading " + file.name + "…"; }
        xhr.upload.addEventListener("progress", function (event) {
            if (event.lengthComputable && bar) bar.value = Math.round(event.loaded / event.total * 100);
        });
        xhr.addEventListener("load", function () {
            var res = {};
            try { res = JSON.parse(xhr.responseText); } catch (e) { res = {}; }
            setStorage(res.storage);
            if (xhr.status === 201 && res.attachments) {
                addRows(section, res.attachments);
                setCount(section, res.count);
                showError(section, (res.errors || []).join(" "));
                changed(section, "add", {attachments: res.attachments, count: res.count});
                if (job.onDone) job.onDone(res.attachments[0] || null);
            } else {
                showError(section, (res.errors && res.errors.join(" ")) ||
                    (xhr.status === 413 ? "That file is too big." : "Couldn't attach “" + file.name + "”."));
                if (job.onDone) job.onDone(null);
            }
            done();
        });
        xhr.addEventListener("error", function () {
            showError(section, "Couldn't attach “" + file.name + "” - check your connection and try again.");
            if (job.onDone) job.onDone(null);
            done();
        });
        function done() {
            if (p.progress && !queue.length) p.progress.hidden = true;
            next();
        }
        xhr.send(data);
    }

    function uploadAll(section, fileList) {
        showError(section, "");
        var input = section.querySelector("[data-att-input]");
        if (input && input.disabled) { showError(section, FULL_TITLE); return; }
        Array.prototype.slice.call(fileList || []).forEach(function (f) { upload(section, f); });
    }

    document.addEventListener("change", function (event) {
        var input = event.target.closest && event.target.closest("[data-att-input]");
        if (!input) return;
        var section = input.closest("[data-att]");
        uploadAll(section, input.files);
        input.value = "";
    });
    // With JavaScript the input uploads on its own; the form never submits (the <noscript> button is gone).
    document.addEventListener("submit", function (event) {
        if (event.target.matches && event.target.matches("[data-att-form]")) event.preventDefault();
    });

    // ---------- Drag and drop (desktop) ----------
    function hasFiles(event) {
        var types = event.dataTransfer && event.dataTransfer.types;
        return types && Array.prototype.indexOf.call(types, "Files") !== -1;
    }
    function dropTarget(event) {
        var section = event.target.closest && event.target.closest("[data-att]");
        return section && section.querySelector("[data-att-form]") ? section : null;
    }
    document.addEventListener("dragover", function (event) {
        var section = dropTarget(event);
        if (!section || !hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        section.classList.add("is-dropping");
    });
    document.addEventListener("dragleave", function (event) {
        var section = dropTarget(event);
        if (section && !section.contains(event.relatedTarget)) section.classList.remove("is-dropping");
    });
    document.addEventListener("drop", function (event) {
        var section = dropTarget(event);
        if (!section || !hasFiles(event)) return;
        event.preventDefault();
        section.classList.remove("is-dropping");
        uploadAll(section, event.dataTransfer.files);
    });

    // ---------- Remove (a confirm step in place) ----------
    function confirmMode(form, on) {
        form.querySelector(".att__trash").hidden = on;
        form.querySelector(".att__confirm").hidden = !on;
        form.classList.toggle("is-confirming", on);
        (on ? form.querySelector("[data-att-confirm]") : form.querySelector(".att__trash")).focus();
    }
    document.addEventListener("click", function (event) {
        var trash = event.target.closest && event.target.closest(".att__trash");
        if (trash) {
            event.preventDefault();
            confirmMode(trash.closest("[data-att-delete]"), true);
            return;
        }
        var cancel = event.target.closest && event.target.closest("[data-att-cancel]");
        if (cancel) confirmMode(cancel.closest("[data-att-delete]"), false);
    });
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        var form = event.target.closest && event.target.closest("[data-att-delete].is-confirming");
        if (form) { event.preventDefault(); event.stopPropagation(); confirmMode(form, false); }
    }, true);
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.matches || !form.matches("[data-att-delete]")) return;
        event.preventDefault();
        if (!form.classList.contains("is-confirming")) { confirmMode(form, true); return; }
        var section = form.closest("[data-att]");
        var item = form.closest("[data-att-item]");
        var xhr = new XMLHttpRequest();
        xhr.open("POST", form.action);
        xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        xhr.setRequestHeader("X-CSRFToken", csrfToken(form));
        xhr.setRequestHeader("Accept", "application/json");
        xhr.addEventListener("load", function () {
            var res = {};
            try { res = JSON.parse(xhr.responseText); } catch (e) { res = {}; }
            setStorage(res.storage);
            if (xhr.status === 200 && res.ok) {
                item.remove();
                setCount(section, res.count);
                showError(section, "");
                changed(section, "remove", {count: res.count});
                var add = section.querySelector("[data-att-input]");
                if (add) add.focus();
            } else {
                showError(section, "Couldn't remove it - reload the page and try again.");
                confirmMode(form, false);
            }
        });
        xhr.addEventListener("error", function () {
            showError(section, "Couldn't remove it - check your connection.");
            confirmMode(form, false);
        });
        xhr.send(new FormData(form));
    });

    // ---------- Full-screen image viewer ----------
    var viewer = null, images = [], index = 0, opener = null, touchX = null;

    function buildViewer() {
        viewer = document.createElement("dialog");
        viewer.className = "att-viewer";
        viewer.setAttribute("aria-label", "Image viewer");
        viewer.innerHTML =
            '<div class="att-viewer__bar"><span class="att-viewer__name" data-v-name></span>' +
            '<span class="att-viewer__pos" data-v-pos></span>' +
            '<a class="att-viewer__btn" data-v-open target="_blank" rel="noopener" aria-label="Open the original" title="Open the original">' +
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg></a>' +
            '<button type="button" class="att-viewer__btn" data-v-close aria-label="Close" title="Close (Esc)">' +
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>' +
            '<div class="att-viewer__stage"><img class="att-viewer__img" data-v-img alt=""></div>' +
            '<button type="button" class="att-viewer__nav att-viewer__nav--prev" data-v-prev aria-label="Previous image">' +
            '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg></button>' +
            '<button type="button" class="att-viewer__nav att-viewer__nav--next" data-v-next aria-label="Next image">' +
            '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>';
        document.body.appendChild(viewer);
        viewer.querySelector("[data-v-close]").addEventListener("click", function () { viewer.close(); });
        viewer.querySelector("[data-v-prev]").addEventListener("click", function () { show(index - 1); });
        viewer.querySelector("[data-v-next]").addEventListener("click", function () { show(index + 1); });
        viewer.addEventListener("click", function (event) {
            if (event.target === viewer || event.target.classList.contains("att-viewer__stage")) viewer.close();
        });
        viewer.addEventListener("keydown", function (event) {
            event.stopPropagation();          // the page's own shortcuts (Planner: E edits) wait until it closes
            if (event.key === "ArrowLeft") { event.preventDefault(); show(index - 1); }
            else if (event.key === "ArrowRight") { event.preventDefault(); show(index + 1); }
        });
        viewer.addEventListener("touchstart", function (event) {
            touchX = event.touches.length === 1 ? event.touches[0].clientX : null;
        }, {passive: true});
        viewer.addEventListener("touchend", function (event) {
            if (touchX === null) return;
            var dx = event.changedTouches[0].clientX - touchX;
            touchX = null;
            if (Math.abs(dx) > 50) show(index + (dx < 0 ? 1 : -1));
        });
        viewer.addEventListener("close", function () {
            viewer.querySelector("[data-v-img]").removeAttribute("src");
            if (opener && document.contains(opener)) opener.focus();
        });
    }

    function show(i) {
        if (!images.length) return;
        index = (i + images.length) % images.length;
        var link = images[index];
        var img = viewer.querySelector("[data-v-img]");
        img.src = link.dataset.full;
        img.alt = link.dataset.name || "";
        viewer.querySelector("[data-v-name]").textContent = link.dataset.name || "";
        viewer.querySelector("[data-v-pos]").textContent = images.length > 1 ? (index + 1) + " / " + images.length : "";
        viewer.querySelector("[data-v-open]").href = link.dataset.full;
        var many = images.length > 1;
        viewer.querySelector("[data-v-prev]").hidden = !many;
        viewer.querySelector("[data-v-next]").hidden = !many;
    }

    document.addEventListener("click", function (event) {
        var link = event.target.closest && event.target.closest("[data-att-view]");
        if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.button) return;
        event.preventDefault();
        if (!viewer) buildViewer();
        var section = link.closest("[data-att]");
        images = Array.prototype.slice.call(section.querySelectorAll("[data-att-view]"));
        opener = link;
        if (typeof viewer.showModal === "function") viewer.showModal(); else viewer.setAttribute("open", "");
        show(images.indexOf(link));
        viewer.querySelector("[data-v-close]").focus();
    });

    window.Attachments = {upload: upload};
})();
