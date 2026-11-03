"""Read-only profile identity, owner top card, and LLM-parse verification.

Structure is used only to prove who the page and its actions belong to; profile
prose and audience counts come from the LLM and must appear verbatim on the page.
A canonical link is corroboration, never proof that a SPA topcard has hydrated.
"""

from dataclasses import dataclass, field
import re
import time
import unicodedata
from typing import NoReturn
from urllib.parse import quote, unquote, urlsplit

from exceptions import TaskSkippedException


@dataclass(frozen=True)
class ProfileIdentity:
    url: str
    slug: str
    name: str


@dataclass(frozen=True)
class ProfileSnapshot:
    identity: ProfileIdentity
    _topcard_selector: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class ProfileDetails:
    """LLM-parsed owner fields; each kept only when found verbatim on the page."""

    headline: str = ""
    about: str = ""
    experience: str = ""
    followers_text: str = ""
    connections_text: str = ""

    def content(self, name: str) -> str:
        """Note-generation input, or "" when too thin to personalize."""
        fields = (("Headline", self.headline), ("About", self.about), ("Experience", self.experience))
        if (
            max(len(self.about), len(self.experience)) < 80
            or sum(len(value) for _, value in fields) < _MIN_CONTENT_LENGTH
        ):
            return ""
        parts = [name, *(f"{label}:\n{value}" for label, value in fields if value)]
        return "\n\n".join(parts)[:_MAX_CONTENT_LENGTH]


def canonical_profile_url(url: str) -> str | None:
    """Accept only exact LinkedIn public-profile paths, with unambiguous slugs."""
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f]", url):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*linkedin\.com", host
            )
        ):
            return None
        match = re.fullmatch(r"/in/([^/]+)/?", parsed.path)
        if not match:
            return None
        slug = unicodedata.normalize("NFC", unquote(match[1], errors="strict")).lower()
        if not slug or any(
            not (c.isalnum() or unicodedata.category(c).startswith("M") or c in "-._~")
            for c in slug
        ):
            return None
        if slug in {".", ".."}:
            return None
        return f"https://www.linkedin.com/in/{quote(slug, safe='-._~')}/"
    except (ValueError, UnicodeError):
        return None


# All consumers share this ownership decision, including action/state scanners.
# It never inserts attributes, hides nodes, changes styles, or rewrites the DOM.
_PROFILE_DOM_JS = r"""
(args) => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const headings = 'h1,h2,h3,[role="heading"]';
  const sduiCards = '[componentkey^="com.linkedin.sdui.profile.card."]';
  const lazyColumn = '[data-component-type="LazyColumn"]';
  const noise = 'aside,nav,footer,[role="dialog"],[role="complementary"]';
  const visible = el => !!el && el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden';
  const labelText = el => {
    const chunks = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.closest('.visually-hidden,.sr-only,[hidden]') && visible(node.parentElement))
        chunks.push(node.nodeValue);
    }
    return clean(chunks.join(' '));
  };
  const loading = '[aria-busy="true"],.artdeco-loader,[class*="skeleton"],[data-loading="true"]';
  const busy = (el, excluded) => el.matches(loading) ||
    [...el.querySelectorAll(loading)].some(node => {
      if (insideExcluded(node, el, excluded)) return false;
      if (visible(node)) return true;
      // Boxless loading wrappers still gate their rendered section contents.
      const style = getComputedStyle(node);
      if (style.display !== 'contents' || style.visibility === 'hidden') return false;
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      for (let child = walker.nextNode(); child; child = walker.nextNode()) {
        if (!clean(child.nodeValue) || getComputedStyle(child.parentElement).visibility === 'hidden') continue;
        const range = document.createRange();
        range.selectNodeContents(child);
        if (range.getClientRects().length) return true;
      }
      return false;
    });
  // LinkedIn may wrap each column item in unkeyed, single-child layout divs.
  const columnOf = card => {
    let el = card.parentElement;
    while (el && !el.matches(lazyColumn) && el.matches('div:not([componentkey])') &&
        el.children.length === 1) el = el.parentElement;
    return el?.matches(lazyColumn) ? el : null;
  };
  const profile = (value, marker = false) => {
    try {
      const u = new URL(value, location.href);
      if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password ||
          (u.port && !['80', '443'].includes(u.port)) ||
          !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*linkedin\.com$/i.test(u.hostname)) return null;
      const m = u.pathname.match(marker ? /^\/in\/([^/]+)(?:\/.*)?$/ : /^\/in\/([^/]+)\/?$/);
      if (!m) return null;
      const slug = decodeURIComponent(m[1]).normalize('NFC').toLowerCase();
      if (!/^[\p{L}\p{N}\p{M}_.~\-]+$/u.test(slug) || ['.', '..'].includes(slug)) return null;
      return 'https://www.linkedin.com/in/' + encodeURIComponent(slug) + '/';
    } catch (_) { return null; }
  };
  const expected = args.url;
  if (profile(location.href) !== expected) return {status: 'mismatch'};
  for (const link of document.querySelectorAll('link[rel~="canonical"]')) {
    if (profile(link.href) !== expected) return {status: 'mismatch'};
  }
  const mains = [...document.querySelectorAll('main')].filter(visible);
  if (mains.length !== 1) return {status: 'not_ready'};
  const main = mains[0];
  const ownHeadings = el => [...el.querySelectorAll(headings)].filter(h => visible(h) && !h.closest(noise));
  const path = el => {
    const parts = [];
    while (el && el.nodeType === 1) {
      let index = 1;
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling)
        if (s.tagName === el.tagName) index++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  // A module is a structural subtree, not a list of forbidden text phrases.
  const moduleFor = (node, root) => {
    let current = node.parentElement;
    while (current && current !== root) {
      if (current.matches('section,article,li') || current.parentElement === root) return current;
      current = current.parentElement;
    }
    return root;
  };
  const exclusions = (root, allowedHeading = null) => {
    const excluded = new Set(root.querySelectorAll(noise + ',script,style,button,[role="button"],.visually-hidden,.sr-only,[hidden]'));
    for (const h of root.querySelectorAll(headings))
      if (h !== allowedHeading) excluded.add(moduleFor(h, root));
    for (const a of root.querySelectorAll('a[href]')) {
      const target = profile(a.href, true);
      if (target && target !== expected) excluded.add(moduleFor(a, root));
    }
    return excluded;
  };
  const insideExcluded = (el, root, excluded) => {
    for (let n = el; n; n = n.parentElement) {
      if (excluded.has(n)) return true;
      if (n === root) return false;
    }
    return true;
  };
  const candidates = [];
  for (const heading of main.querySelectorAll('h1,h2,[role="heading"][aria-level="1"],[role="heading"][aria-level="2"]')) {
    if (!visible(heading) || heading.closest(noise)) continue;
    const name = labelText(heading);
    if (!name || /^(about|experience|activity|education|skills|interests|recommendations)$/i.test(name)) continue;
    let root = heading.parentElement;
    while (root && root !== main) {
      if (root.matches('section,article,.pv-top-card,[data-view-name="profile-top-card"],[data-view-name="profile-card"]') || root.parentElement === main) break;
      root = root.parentElement;
    }
    if (!root || root === main || root.closest(noise)) continue;
    const sduiCard = root.closest(sduiCards);
    const sduiKey = sduiCard?.getAttribute('componentkey') || '';
    const sduiColumn = sduiKey.endsWith('Topcard') ? columnOf(sduiCard) : null;
    // SDUI nests keyed cards inside a layout section. Only its explicit
    // topcard/column relationship may cross that otherwise unsafe boundary.
    if (root.parentElement?.closest('section,article') && !sduiColumn) continue;
    let enclosed = false;
    for (let ancestor = root.parentElement; ancestor && ancestor !== main; ancestor = ancestor.parentElement) {
      if ([...ancestor.querySelectorAll(headings)].some(h => !root.contains(h) &&
          (h.compareDocumentPosition(root) & Node.DOCUMENT_POSITION_FOLLOWING))) enclosed = true;
    }
    if (enclosed) continue;
    // Only the primary heading of a bounded card can establish its owner.
    if (ownHeadings(root)[0] !== heading) continue;
    const excluded = exclusions(root, heading);
    const markers = [];
    for (const a of root.querySelectorAll('a[href]')) {
      if (!visible(a) || a.closest(noise)) continue;
      const href = a.getAttribute('href') || '';
      const label = labelText(a) || clean(a.getAttribute('aria-label'));
      const primary = a.contains(heading) || heading.contains(a) || label === name ||
        /contact-info(?:\/|[?#]|$)/i.test(href) || /^contact info$/i.test(label) ||
        /^(connect|invite .+ to connect|message|more)$/i.test(label);
      if (!primary) continue;
      // Ignore markers in nested modules, but do not discard a contradictory
      // self/contact/action marker merely because it points to another person.
      let nested = false;
      for (let p = a.parentElement; p && p !== root; p = p.parentElement) {
        if (p.matches('section,article,li,aside,nav,[role="dialog"]') ||
            [...p.querySelectorAll(headings)].some(h => h !== heading)) nested = true;
      }
      if (nested) continue;
      const target = profile(a.href, true);
      if (target) markers.push(target);
      else if (/\/in\//i.test(href)) markers.push('invalid');
    }
    if (!markers.length) continue;
    candidates.push({root, heading, name, markers, excluded,
      primary: sduiColumn || root.parentElement});
  }
  // Multiple apparent owners are unsafe even when one matches the queue.
  const unique = candidates.filter((item, i) => candidates.findIndex(c => c.root === item.root) === i);
  if (!unique.length) return {status: 'not_ready'};
  if (unique.length !== 1 || unique[0].markers.some(url => url !== expected)) return {status: 'mismatch'};
  const {root, heading, name, excluded, primary} = unique[0];
  if (ownHeadings(main)[0] !== heading) return {status: 'not_ready'};
  if (args.name && name !== args.name) return {status: 'mismatch'};
  if (busy(root, excluded)) return {status: 'not_ready'};
  // The owning column must not itself be a foreign module.
  if (primary !== main && (!main.contains(primary) || primary.closest(noise))) return {status: 'not_ready'};
  const owned = el => {
    if (!el || !root.contains(el) || !visible(el)) return false;
    // Buttons are valid actions, even though excluded from extracted prose.
    const actionExcluded = new Set([...excluded].filter(n => !n.matches('button,[role="button"]')));
    if (insideExcluded(el, root, actionExcluded)) return false;
    for (let n = el.parentElement; n && n !== root; n = n.parentElement)
      if (n.matches('section,article,li')) return false;
    return true;
  };
  if (args.element) return {status: 'ready', owned: owned(args.element)};
  return {status: 'ready', name, topcard: path(root)};
}
"""

_MIN_CONTENT_LENGTH = 200
_MAX_CONTENT_LENGTH = 15000
_MAX_PAGE_TEXT = 30000
_TEXT_STABLE_SECONDS = 1.0
_POLL_MS = 100
_MAIN_TEXT_JS = "() => { const m = document.querySelectorAll('main'); return m.length === 1 ? m[0].innerText : ''; }"


def _extract(page, expected_url: str) -> dict | None:
    try:
        return page.evaluate(_PROFILE_DOM_JS, {"url": expected_url, "name": ""})
    except Exception:
        # Navigation/detachment cannot establish ownership; retry only reads.
        return None


def _snapshot(result: dict | None, url: str) -> ProfileSnapshot | None:
    if not result or result.get("status") != "ready":
        return None
    identity = ProfileIdentity(url, urlsplit(url).path.split("/")[2], result["name"])
    return ProfileSnapshot(identity, result.get("topcard", ""))


def read_profile_snapshot(page, expected_url: str) -> ProfileSnapshot | None:
    """Take one synchronous DOM snapshot; no navigation, scrolling, or sleeps."""
    url = canonical_profile_url(expected_url)
    return _snapshot(_extract(page, url), url) if url else None


def _skip(reason: str) -> NoReturn:
    raise TaskSkippedException(reason, cooldown_eligible=False)


def wait_for_profile(page, expected_url: str, *, timeout_ms: int = 8000) -> ProfileSnapshot:
    """Wait (bounded) until the target's owner top card is verified."""
    url = canonical_profile_url(expected_url)
    if not url:
        _skip("profile_identity_mismatch")
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    while True:
        result = _extract(page, url)
        if result and result.get("status") == "mismatch":
            _skip("profile_identity_mismatch")
        snapshot = _snapshot(result, url)
        if snapshot:
            return snapshot
        now = time.monotonic()
        if now >= deadline:
            _skip("profile_not_ready")
        page.wait_for_timeout(min(_POLL_MS, max(1, int((deadline - now) * 1000))))


def wait_for_profile_text(page, *, timeout_ms: int = 8000) -> str:
    """Return the single <main>'s rendered text once it stops changing (bounded)."""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    last, since = "", time.monotonic()
    while True:
        try:
            text = page.evaluate(_MAIN_TEXT_JS) or ""
        except Exception:
            text = ""
        now = time.monotonic()
        if text != last:
            last, since = text, now
        elif text and now - since >= _TEXT_STABLE_SECONDS:
            break
        if now >= deadline:
            break
        page.wait_for_timeout(_POLL_MS)
    return last[:_MAX_PAGE_TEXT]


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def verify_profile_details(page_text: str, parsed) -> ProfileDetails | None:
    """Keep only LLM values whose every line appears verbatim in the page text.

    The LLM chooses which text belongs to the owner; this guard ensures it can
    never contribute words, employers, or counts that are not on the page.
    """
    if not isinstance(parsed, dict):
        return None
    haystack = _clean(page_text)

    def verified(value) -> str:
        if not isinstance(value, str):
            return ""
        lines = [_clean(line) for line in value.splitlines() if _clean(line)]
        return "\n".join(lines) if lines and all(line in haystack for line in lines) else ""

    return ProfileDetails(
        *(verified(parsed.get(key)) for key in ("headline", "about", "experience", "followers_text", "connections_text"))
    )


def assert_profile_identity(page, identity: ProfileIdentity) -> ProfileSnapshot:
    snapshot = read_profile_snapshot(page, identity.url)
    if snapshot is None or snapshot.identity != identity:
        _skip("profile_identity_mismatch")
    return snapshot


def get_profile_topcard(page, identity: ProfileIdentity):
    snapshot = assert_profile_identity(page, identity)
    return (
        page.locator(snapshot._topcard_selector) if snapshot._topcard_selector else None
    )


def is_profile_owned_element(locator, page, identity: ProfileIdentity) -> bool:
    """Use the extractor's identity and nested-module rules for a live action."""
    assert_profile_identity(page, identity)
    result = locator.evaluate(
        "(element, args) => (" + _PROFILE_DOM_JS + ")({...args, element})",
        {"url": identity.url, "name": identity.name},
    )
    return bool(result and result.get("status") == "ready" and result.get("owned"))
