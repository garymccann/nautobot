// nautobot/project-static/js/object_lock.js
// Object Lock: accessible blocked controls + release-then-act live counter.
(function () {
  "use strict";

  // 1. Turn Edit/Delete actions on a locked object into focusable aria-disabled controls.
  //    A native `disabled` button drops out of the tab order and hides its reason from AT, so we
  //    keep the element focusable and suppress the action ourselves.
  function wireBlockedControls() {
    document.querySelectorAll("[data-object-lock-blocked]").forEach(function (el) {
      if (el.dataset.objectLockWired === "true") {
        return;
      }
      el.dataset.objectLockWired = "true";
      var explanationId = el.getAttribute("aria-describedby");
      function handle(event) {
        if (el.getAttribute("aria-disabled") !== "true") {
          return; // unblocked: let the real action proceed
        }
        event.preventDefault();
        event.stopPropagation();
        if (explanationId) {
          var explanation = document.getElementById(explanationId);
          if (explanation) {
            explanation.removeAttribute("hidden");
            explanation.setAttribute("tabindex", "-1");
            explanation.focus();
          }
        }
      }
      el.addEventListener("click", handle);
      el.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          handle(event);
        }
      });
    });
  }

  // 2. Inline per-claim release with a live "N remaining" counter.
  //    Release one claim via the management delete view. It needs a ConfirmationForm, so POST confirm=true + CSRF as a urlencoded body;
  //    the view 302-redirects on success and fetch follows it, so resp.ok means released.
  function wireReleaseButtons() {
    document.querySelectorAll(".object-lock-release-btn").forEach(function (btn) {
      if (btn.dataset.objectLockWired === "true") {
        return;
      }
      btn.dataset.objectLockWired = "true";
      btn.addEventListener("click", function () {
        var url = btn.dataset.releaseUrl;
        var csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
        var token = csrf ? csrf.value : "";
        var params = new URLSearchParams();
        params.set("confirm", "true");
        if (token) {
          params.set("csrfmiddlewaretoken", token);
        }
        fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": token,
            "X-Requested-With": "XMLHttpRequest",
          },
          body: params.toString(),
          credentials: "same-origin",
        }).then(function (resp) {
          // Success is a 302 the fetch follows back to a Nautobot page (resp.ok). A session-timeout
          // 302 instead lands on the login page — detect THAT by the final URL, not resp.redirected,
          // which is also true on the normal success redirect and would reject every release.
          if (!resp.ok || /\/login\//.test(new URL(resp.url).pathname)) {
            reportReleaseError();
            return;
          }
          // Remove the released claim's row.
          var row = btn.closest("tr[data-lock-id]");
          if (row) {
            row.parentNode.removeChild(row);
          }
          updateRemaining();
        }).catch(reportReleaseError);
      });
    });
  }

  function reportReleaseError() {
    // Surface the failure in the polite aria-live region instead of silently leaving the row in place.
    var region = document.querySelector(".object-lock-remaining");
    if (region) {
      region.textContent = "Release failed. Reload the page and try again.";
    }
  }

  function updateRemaining() {
    var region = document.querySelector(".object-lock-remaining");
    if (!region) {
      return;
    }
    var remaining = remainingRows().length;
    region.dataset.remaining = String(remaining);
    var countEl = region.querySelector(".object-lock-remaining-count");
    if (countEl) {
      countEl.textContent = String(remaining);
    }
    if (remaining === 0) {
      // Enable the blocked controls in place.
      document.querySelectorAll("[data-object-lock-blocked]").forEach(function (el) {
        el.setAttribute("aria-disabled", "false");
        el.classList.remove("disabled");
      });
      region.textContent = "0 lock(s) remaining. You may now proceed.";
    } else if (region.dataset.blockingOthers === "true" && allRemainingHeldByOthers()) {
      // 3. Mixed-ownership terminal state: counter cannot reach zero.
      var sources = remainingOtherSources();
      region.textContent =
        "You've released your locks. " + remaining + " remain, held by " + sources +
        ". Contact an administrator to release them.";
    }
  }

  function remainingRows() {
    return Array.prototype.slice.call(document.querySelectorAll("tr[data-lock-id]"));
  }

  function allRemainingHeldByOthers() {
    return remainingRows().every(function (row) {
      var btn = row.querySelector(".object-lock-release-btn");
      return !btn; // no release button => not releasable here (other source, or own lock without delete_objectlock)
    });
  }

  function remainingOtherSources() {
    var labels = remainingRows()
      .map(function (row) {
        return row.getAttribute("data-source-label");
      })
      .filter(Boolean);
    return labels.length ? Array.from(new Set(labels)).join(", ") : "another source";
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireBlockedControls();
    wireReleaseButtons();
  });
})();
