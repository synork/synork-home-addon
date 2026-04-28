/**
 * Synork Home — HA Frontend JS Patcher
 *
 * Runtime-patches the HA frontend to apply Synork branding:
 *   - Replaces "Home Assistant" strings with "Synork Home"
 *   - Swaps the HA logo with Synork logo
 *   - Hides/curates specific sidebar items
 *   - Adds Synork panel link to sidebar
 *
 * This is loaded as an HA frontend extra_module_url. It runs once on
 * page load and sets up MutationObservers for dynamic content.
 *
 * Maintenance: ~1 hour per HA major release to fix any selector breakage.
 */

(function () {
  "use strict";

  // ── Configuration ──────────────────────────────────────────────────────
  var SYNORK_NAME = "Synork Home";
  var SYNORK_LOGO_URL = "/local/synork/logo.svg";
  var SYNORK_ICON_URL = "/local/synork/icon.svg";

  // Sidebar items to hide (by their panel name / data-panel attribute)
  var HIDDEN_PANELS = [
    // Hide supervisor controls that could break Synork-managed OS
    // Users can still access via direct URL if needed
  ];

  // String replacements applied to text nodes
  var STRING_REPLACEMENTS = [
    ["Home Assistant", SYNORK_NAME],
    ["home-assistant", "synork-home"],
    ["Hass.io", SYNORK_NAME],
  ];

  // ── String patching ────────────────────────────────────────────────────

  function patchTextNode(node) {
    if (!node.textContent) return;
    var original = node.textContent;
    var patched = original;
    for (var i = 0; i < STRING_REPLACEMENTS.length; i++) {
      var pair = STRING_REPLACEMENTS[i];
      if (patched.indexOf(pair[0]) !== -1) {
        patched = patched.split(pair[0]).join(pair[1]);
      }
    }
    if (patched !== original) {
      node.textContent = patched;
    }
  }

  function patchElement(el) {
    if (!el || !el.childNodes) return;

    // Patch text nodes
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false);
    var textNode;
    while ((textNode = walker.nextNode())) {
      patchTextNode(textNode);
    }

    // Patch title attribute
    if (el.title) {
      for (var i = 0; i < STRING_REPLACEMENTS.length; i++) {
        var pair = STRING_REPLACEMENTS[i];
        if (el.title.indexOf(pair[0]) !== -1) {
          el.title = el.title.split(pair[0]).join(pair[1]);
        }
      }
    }

    // Patch document title
    if (document.title) {
      for (var j = 0; j < STRING_REPLACEMENTS.length; j++) {
        var pair2 = STRING_REPLACEMENTS[j];
        if (document.title.indexOf(pair2[0]) !== -1) {
          document.title = document.title.split(pair2[0]).join(pair2[1]);
        }
      }
    }
  }

  // ── Logo swapping ──────────────────────────────────────────────────────

  function patchLogos() {
    // Patch sidebar logo
    var sidebarLogos = document.querySelectorAll(
      "ha-sidebar .menu img, ha-sidebar .title img"
    );
    for (var i = 0; i < sidebarLogos.length; i++) {
      if (sidebarLogos[i].src && sidebarLogos[i].src.indexOf("synork") === -1) {
        sidebarLogos[i].src = SYNORK_LOGO_URL;
        sidebarLogos[i].alt = SYNORK_NAME;
      }
    }

    // Patch login page logo
    var loginLogos = document.querySelectorAll(
      "ha-authorize img, ha-onboarding img"
    );
    for (var j = 0; j < loginLogos.length; j++) {
      if (loginLogos[j].src && loginLogos[j].src.indexOf("synork") === -1) {
        loginLogos[j].src = SYNORK_LOGO_URL;
      }
    }

    // Patch favicon
    var favicons = document.querySelectorAll('link[rel="icon"], link[rel="shortcut icon"]');
    for (var k = 0; k < favicons.length; k++) {
      if (favicons[k].href && favicons[k].href.indexOf("synork") === -1) {
        favicons[k].href = SYNORK_ICON_URL;
      }
    }
  }

  // ── Sidebar curation ───────────────────────────────────────────────────

  function patchSidebar() {
    if (HIDDEN_PANELS.length === 0) return;

    var sidebarItems = document.querySelectorAll(
      "ha-sidebar a[data-panel], ha-sidebar paper-icon-item[data-panel]"
    );
    for (var i = 0; i < sidebarItems.length; i++) {
      var panel = sidebarItems[i].getAttribute("data-panel");
      if (panel && HIDDEN_PANELS.indexOf(panel) !== -1) {
        sidebarItems[i].style.display = "none";
      }
    }
  }

  // ── Shadow DOM traversal ───────────────────────────────────────────────

  function patchShadowRoots(root) {
    if (!root) return;
    patchElement(root);

    var elements = root.querySelectorAll("*");
    for (var i = 0; i < elements.length; i++) {
      if (elements[i].shadowRoot) {
        patchShadowRoots(elements[i].shadowRoot);
      }
    }
  }

  // ── Main patch function ────────────────────────────────────────────────

  function runPatch() {
    patchShadowRoots(document.body);
    patchLogos();
    patchSidebar();
  }

  // ── MutationObserver for dynamic content ───────────────────────────────

  var patchTimeout = null;

  function schedulePatch() {
    if (patchTimeout) return;
    patchTimeout = setTimeout(function () {
      patchTimeout = null;
      runPatch();
    }, 100);
  }

  // Observe DOM changes for dynamic content (HA is SPA, content changes)
  var observer = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      var mutation = mutations[i];
      if (mutation.type === "childList" && mutation.addedNodes.length > 0) {
        schedulePatch();
        return;
      }
    }
  });

  // ── Title observer ─────────────────────────────────────────────────────

  var titleObserver = new MutationObserver(function () {
    if (document.title && document.title.indexOf("Home Assistant") !== -1) {
      document.title = document.title.split("Home Assistant").join(SYNORK_NAME);
    }
  });

  // ── Initialize ─────────────────────────────────────────────────────────

  function init() {
    // Initial patch
    runPatch();

    // Watch for DOM changes
    observer.observe(document.body, {
      childList: true,
      subtree: true,
    });

    // Watch for title changes
    var titleEl = document.querySelector("title");
    if (titleEl) {
      titleObserver.observe(titleEl, { childList: true });
    }

    console.log("[Synork] Frontend patcher loaded");
  }

  // Run when DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
