"""Read-only, positive-ownership profile extraction and bounded hydration waits.

Unknown layouts intentionally lose personalization rather than borrow nearby text.
A canonical link is corroboration, never proof that a SPA topcard has hydrated.
"""

from dataclasses import dataclass, field, replace
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
    headline: str = ""
    about: str = ""
    experience: str = ""
    audience_text: str = ""
    content: str = ""
    _topcard_selector: str = field(default="", repr=False, compare=False)
    _section_selectors: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _content_ready: bool = field(default=False, repr=False, compare=False)


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
    [...el.querySelectorAll(loading)].some(node => visible(node) && !insideExcluded(node, el, excluded));
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
  const exclusions = (root, allowedHeading = null, experience = false) => {
    const excluded = new Set(root.querySelectorAll(noise + ',script,style,button,[role="button"],.visually-hidden,.sr-only,[hidden]'));
    // LinkedIn's displayed text often has aria-hidden=true paired with a
    // visually-hidden duplicate. Keep the displayed text, not both or neither.
    for (const h of root.querySelectorAll(headings)) {
      if (h === allowedHeading) continue;
      // Job titles are headings too. Within Experience, a bounded job item
      // linked to an employer is owned prose, not a recommendation module.
      const job = h.closest('li,article');
      const employerLinked = experience && job && root.contains(job) &&
        h.matches('h3,[role="heading"][aria-level="3"]') &&
        [...h.parentElement.querySelectorAll('a[href]')].some(a => {
          try {
            const u = new URL(a.href);
            return /^(?:[a-z0-9-]+\.)*linkedin\.com$/i.test(u.hostname) &&
              /^\/company\/[^/]+\/?$/.test(u.pathname);
          } catch (_) { return false; }
        });
      if (!employerLinked) excluded.add(moduleFor(h, root));
    }
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
    const sduiColumn = sduiKey.endsWith('Topcard') &&
      sduiCard.parentElement?.matches(lazyColumn) ? sduiCard.parentElement : null;
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
      primary: sduiColumn || root.parentElement,
      cardPrefix: sduiColumn ? sduiKey.slice(0, -'Topcard'.length) : ''});
  }
  // Multiple apparent owners are unsafe even when one matches the queue.
  const unique = candidates.filter((item, i) => candidates.findIndex(c => c.root === item.root) === i);
  if (!unique.length) return {status: 'not_ready'};
  if (unique.length !== 1 || unique[0].markers.some(url => url !== expected)) return {status: 'mismatch'};
  const {root, heading, name, excluded, primary, cardPrefix} = unique[0];
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
  const text = (container, omitted, omitHeading = null) => {
    if (!container || insideExcluded(container, container, omitted)) return '';
    const chunks = [];
    const visit = node => {
      if (node.nodeType === Node.TEXT_NODE) { const s = clean(node.nodeValue); if (s) chunks.push(s); return; }
      if (node.nodeType !== Node.ELEMENT_NODE || omitted.has(node) || node === omitHeading || !visible(node)) return;
      for (const child of node.childNodes) visit(child);
      if (node.matches('div,p,li,section,article,br')) chunks.push('\n');
    };
    visit(container);
    return chunks.join(' ').replace(/ *\n */g, '\n').replace(/\n+/g, '\n').trim();
  };
  let headline = '';
  const headlineNodes = [...root.querySelectorAll('.text-body-medium,.pv-text-details__left-panel .text-body-medium,[data-view-name="profile-headline"],[data-field="headline"]')]
    .filter(el => visible(el) && !insideExcluded(el, root, excluded));
  if (headlineNodes.length === 1) headline = text(headlineNodes[0], excluded);
  // Legacy unclassed fixtures: only the immediate leaf after the name block.
  if (!headline && !headlineNodes.length) {
    let block = heading;
    if (heading.parentElement?.tagName === 'A') block = heading.parentElement;
    const next = block.nextElementSibling;
    if (next && next.matches('div,p') && !next.querySelector('a,button,' + headings) &&
        !insideExcluded(next, root, excluded) && !/\b(followers|connections?)\b/i.test(next.innerText))
      headline = text(next, excluded);
  }
  const fields = {about: '', experience: ''};
  const complete = {about: false, experience: false};
  const discovered = {about: false, experience: false};
  const sectionPaths = [];
  for (const key of Object.keys(fields)) {
    const matches = [...primary.querySelectorAll(headings)].filter(h =>
      !root.contains(h) && visible(h) && !h.closest(noise) && labelText(h).toLowerCase() === key);
    discovered[key] = matches.length > 0;
    if (matches.length !== 1) continue;
    const h = matches[0];
    let section = h.parentElement;
    while (section && section !== primary) {
      if (section.matches('section,article') || section.parentElement === primary) break;
      section = section.parentElement;
    }
    if (!section || section === primary || section.contains(root)) continue;
    const sectionCard = h.closest(sduiCards);
    if (cardPrefix && (!sectionCard?.contains(section) ||
        sectionCard.getAttribute('componentkey') !== cardPrefix + key[0].toUpperCase() + key.slice(1) ||
        sectionCard.closest(lazyColumn) !== primary)) continue;
    // Keyed sibling cards share SDUI wrapper groups, not each other's prose.
    // Unkeyed foreign modules still invalidate an enclosing branch.
    const inSiblingCard = node => {
      const card = node.closest(sduiCards);
      return cardPrefix && card && card !== sectionCard && primary.contains(card);
    };
    let nestedModule = false;
    for (let ancestor = section.parentElement; ancestor && ancestor !== primary; ancestor = ancestor.parentElement) {
      if (ancestor.matches('section,article,li') ||
          [...ancestor.querySelectorAll(headings)].some(other => !section.contains(other) && !inSiblingCard(other)) ||
          [...ancestor.querySelectorAll('a[href]')].some(a => {
            const target = profile(a.href, true);
            return target && target !== expected && !inSiblingCard(a);
          })) nestedModule = true;
    }
    if (nestedModule) continue;
    // A branch containing multiple owner sections has no bounded section root.
    if ([...section.querySelectorAll(headings)].some(other => other !== h &&
        /^(about|experience)$/i.test(labelText(other)))) continue;
    const omitted = exclusions(section, h, key === 'experience');
    if (omitted.has(section)) continue;
    sectionPaths.push(path(section));
    if (busy(section, omitted)) continue;
    fields[key] = text(section, omitted, h);
    complete[key] = !!fields[key] &&
      !/^(loading(?:\.{3}|…)?|please wait(?:\.{3}|…)?|show (?:all|more)|see more)$/i.test(fields[key]);
  }
  const audienceExcluded = new Set(excluded);
  audienceExcluded.add(heading);
  for (const node of headlineNodes) audienceExcluded.add(node);
  const audience = text(root, audienceExcluded);
  return {status: 'ready', name, headline, ...fields, audience,
    topcard: path(root), sections: sectionPaths,
    complete: Object.values(complete).some(Boolean) &&
      Object.keys(fields).every(key => !discovered[key] || complete[key]) &&
      !primary.matches('[aria-busy="true"],[data-loading="true"]')};
}
"""

_MIN_CONTENT_LENGTH = 200
_MAX_CONTENT_LENGTH = 15000
_STABILITY_SECONDS = 0.6
_POLL_MS = 100


def _extract(page, expected_url: str, name: str = "") -> dict | None:
    try:
        return page.evaluate(_PROFILE_DOM_JS, {"url": expected_url, "name": name})
    except Exception:
        # Navigation/detachment cannot establish ownership; retry only reads.
        return None


def _snapshot(result: dict, url: str) -> ProfileSnapshot:
    identity = ProfileIdentity(url, urlsplit(url).path.split("/")[2], result["name"])
    headline, about, experience = (
        result.get(key, "") for key in ("headline", "about", "experience")
    )
    parts = [identity.name]
    parts.extend(
        f"{label}:\n{value}"
        for label, value in (
            ("Headline", headline),
            ("About", about),
            ("Experience", experience),
        )
        if value
    )
    content = "\n\n".join(parts)
    ready = (
        bool(result.get("complete"))
        and max(len(about), len(experience)) >= 80
        and len(headline + about + experience) >= _MIN_CONTENT_LENGTH
    )
    return ProfileSnapshot(
        identity,
        headline,
        about,
        experience,
        result.get("audience", ""),
        content[:_MAX_CONTENT_LENGTH] if ready else "",
        result.get("topcard", ""),
        tuple(result.get("sections", ())),
        ready,
    )


def read_profile_snapshot(page, expected_url: str) -> ProfileSnapshot | None:
    """Take one synchronous DOM snapshot; no navigation, scrolling, or sleeps."""
    url = canonical_profile_url(expected_url)
    if not url:
        return None
    result = _extract(page, url)
    return (
        _snapshot(result, url) if result and result.get("status") == "ready" else None
    )


def _skip(reason: str) -> NoReturn:
    raise TaskSkippedException(reason, cooldown_eligible=False)


def wait_for_profile(
    page, expected_url: str, *, personalize: bool = True, timeout_ms: int = 8000
) -> ProfileSnapshot:
    """Wait for verified identity and stable, explicitly loaded owned sections.

    Absent sections are optional; discovered empty/busy sections must hydrate.
    Headline-only and thin profiles exhaust the wait and get a no-note fallback.
    A section not represented in the DOM cannot be predicted before it arrives.
    """
    url = canonical_profile_url(expected_url)
    if not url:
        _skip("profile_identity_mismatch")
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    identity = None
    stable_since = None
    previous = None
    scrolled = set()
    while True:
        result = _extract(page, url, identity.name if identity else "")
        if result and result.get("status") == "mismatch":
            _skip("profile_identity_mismatch")
        snapshot = (
            _snapshot(result, url)
            if result and result.get("status") == "ready"
            else None
        )
        now = time.monotonic()
        if snapshot:
            identity = snapshot.identity
            if not personalize:
                return snapshot
            state = (
                snapshot.identity,
                snapshot.headline,
                snapshot.about,
                snapshot.experience,
                snapshot._content_ready,
            )
            if snapshot._content_ready:
                if state != previous:
                    stable_since = now
                elif (
                    stable_since is not None
                    and now - stable_since >= _STABILITY_SECONDS
                ):
                    return snapshot
            else:
                stable_since = None
            previous = state
            # Trigger only already identified owner sections, never the window
            # or recommendation branches. DOM extraction itself stays read-only.
            for selector in snapshot._section_selectors:
                if (
                    not snapshot._content_ready
                    and selector not in scrolled
                    and now < deadline
                ):
                    scrolled.add(selector)
                    try:
                        page.locator(selector).scroll_into_view_if_needed(
                            timeout=max(1, min(250, int((deadline - now) * 1000)))
                        )
                    except Exception:
                        # Detached sections are re-read on the next iteration.
                        pass
        else:
            previous = None
            stable_since = None
        if now >= deadline:
            if snapshot:
                return replace(snapshot, content="", _content_ready=False)
            _skip("profile_identity_mismatch" if identity else "profile_not_ready")
        page.wait_for_timeout(min(_POLL_MS, max(1, int((deadline - now) * 1000))))


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
