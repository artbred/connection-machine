"""Resolve invitation controls only inside a verified recipient's dialog."""

import re

from playwright.sync_api import Locator, ElementHandle

from exceptions import TaskSkippedException
from linkedin_profile import (
    ProfileIdentity,
    assert_profile_identity,
    canonical_profile_url,
)

ADD_NOTE_SELECTOR = "button[aria-label*='Add a note' i], button:has-text('Add a note')"
INVITE_NOTE_SELECTOR = (
    "textarea#custom-message, textarea[name='message'], "
    "textarea.connect-button-send-invite__custom-message, "
    "[contenteditable='true'][role='textbox'], [contenteditable='true']"
)
SEND_INVITATION_NAME = re.compile(
    r"^(?:Send|Send invitation|Send without a note)$", re.I
)

_DIALOG_EVIDENCE_JS = """
(dialog) => {
  const visible = el => !!(el.getClientRects().length);
  const text = el => (el.innerText || '').replace(/\\s+/g, ' ').trim();
  const headings = [...dialog.querySelectorAll('h1, h2, h3, [role="heading"]')]
    .filter(visible).map(text);
  const label = dialog.getAttribute('aria-label');
  if (label) headings.push(label);
  for (const id of (dialog.getAttribute('aria-labelledby') || '').split(/\\s+/)) {
    const el = document.getElementById(id);
    if (el) headings.push(text(el));
  }
  const paragraphs = [...dialog.querySelectorAll('p')].filter(visible).map(text);
  const links = [...dialog.querySelectorAll('a[href]')].filter(visible)
    .filter(el => {
      try { return /^\\/in\\//.test(new URL(el.href).pathname); }
      catch { return false; }
    }).map(el => ({url: el.href, name: text(el)}));
  const buttons = [...dialog.querySelectorAll('button')].filter(visible)
    .map(el => el.getAttribute('aria-label') || text(el));
  return {
    headings, paragraphs, links, buttons,
    customMessage: !!dialog.querySelector(
      'textarea#custom-message, textarea.connect-button-send-invite__custom-message'
    )
  };
}
"""


def _clean_name(name: str) -> str:
    return " ".join(name.split()).strip(" .!?").casefold()


def _recipient_names(headings: list[str], paragraphs: list[str]) -> list[str]:
    names = []
    for text in headings:
        for pattern in (
            r"^Invite (.+?)(?: to connect)?[.!?]?$",
            r"^Send (?:an? )?invitation to (.+?)[.!?]?$",
            r"^Connect with (.+?)[.!?]?$",
        ):
            match = re.fullmatch(pattern, text, re.I)
            if match:
                names.append(_clean_name(match.group(1)))
                break
    for text in paragraphs:
        match = re.search(
            r"\b(?:this|your) invitation to (.+?)(?: by | with |[.!?]?$)",
            text,
            re.I,
        )
        if match:
            names.append(_clean_name(match.group(1)))
    return names


def find_invite_dialog(
    page,
    identity: ProfileIdentity,
    *,
    verified_note_dialog: ElementHandle | None = None,
) -> Locator | None:
    """Reject ambiguous/foreign recipients, including a stale dialog over the target.

    A full recipient name in invitation-specific UI or an exact profile link is
    required. The note editor may omit both only after Add a note on the same
    previously verified DOM element. A first name alone is not identity proof.
    """
    assert_profile_identity(page, identity)
    dialogs = page.locator("[role='dialog']:visible, dialog[open]")
    matches = []
    expected_name = _clean_name(identity.name)
    first_name = expected_name.split()[0] if expected_name else ""
    for index in range(dialogs.count()):
        dialog = dialogs.nth(index)
        if not dialog.is_visible():
            continue
        evidence = dialog.evaluate(_DIALOG_EVIDENCE_JS)
        names = _recipient_names(evidence["headings"], evidence["paragraphs"])
        invitation_ui = (
            evidence["customMessage"]
            or bool(names)
            or any(
                re.search(
                    r"\b(?:invitation|add a note|send without a note)\b", text, re.I
                )
                for text in evidence["headings"] + evidence["buttons"]
            )
        )
        if not invitation_ui:
            continue
        urls = [canonical_profile_url(link["url"]) for link in evidence["links"]]
        if any(url != identity.url for url in urls):
            raise TaskSkippedException(
                "modal_recipient_mismatch", cooldown_eligible=False
            )
        # Some note explanations shorten the name; accept that only alongside
        # independent full-name or exact-URL evidence, never as identity proof.
        if any(name not in {expected_name, first_name} for name in names):
            raise TaskSkippedException(
                "modal_recipient_mismatch", cooldown_eligible=False
            )
        if not urls and expected_name not in names:
            same_note_dialog = (
                not names
                and evidence["customMessage"]
                and verified_note_dialog is not None
                and dialog.evaluate(
                    "(dialog, verified) => dialog === verified && verified.isConnected",
                    verified_note_dialog,
                )
            )
            if not same_note_dialog:
                raise TaskSkippedException(
                    "modal_recipient_mismatch", cooldown_eligible=False
                )
        matches.append(dialog)
    if len(matches) > 1:
        raise TaskSkippedException("modal_recipient_mismatch", cooldown_eligible=False)
    return matches[0] if matches else None


def require_invite_dialog(
    page,
    identity: ProfileIdentity,
    *,
    verified_note_dialog: ElementHandle | None = None,
) -> Locator:
    dialog = find_invite_dialog(
        page, identity, verified_note_dialog=verified_note_dialog
    )
    if dialog is None:
        raise TaskSkippedException("invite_not_confirmed", cooldown_eligible=False)
    return dialog
