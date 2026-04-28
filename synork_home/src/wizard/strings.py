"""Synork Home — Wizard UI strings (en + hu).

English strings are authoritative. Hungarian content is a placeholder
(TODO: A6B7) so the wizard ships in both languages even before native
Hungarian copy is written. Localized strings are passed to the wizard
HTML at render time via /api/wizard/state, not baked into the bundled
JS, so updates don't require rebuilding the addon image.
"""

from __future__ import annotations

# TODO(A6B7): Hungarian strings are placeholders; native content pending.
# Each "TODO[hu]:" prefix marks a string awaiting native translation.

STRINGS = {
    "en": {
        "title": "Synork Home setup",
        "step.welcome.title": "Welcome to Synork Home",
        "step.welcome.body": (
            "This add-on connects your Home Assistant to your Synork account. "
            "Setup takes about 30 seconds."
        ),
        "step.welcome.cta": "Get started",
        "step.signin.title": "Sign in to Synork",
        "step.signin.body": (
            "Use the email and password for your Synork account. "
            "Your credentials are sent directly to api.synork.dev and aren't stored on this device."
        ),
        "step.signin.email": "Email",
        "step.signin.password": "Password",
        "step.signin.cta": "Sign in",
        "step.signin.error.invalid": "That email or password didn't work. Try again.",
        "step.signin.error.network": "Couldn't reach Synork. Check this device's internet connection and try again.",
        "step.signin.error.tfa": "This account has two-factor auth enabled, which isn't supported in the wizard yet.",
        "step.household.title": "Choose a household",
        "step.household.body": "Pick the household this add-on should join, or create a new one.",
        "step.household.create": "Create a new household",
        "step.household.create.placeholder": "Household name (e.g. Our Apartment)",
        "step.household.location.label": "Location label (optional)",
        "step.household.location.placeholder": "e.g. Living room",
        "step.household.cta": "Pair this device",
        "step.pairing.title": "Pairing this device…",
        "step.pairing.body": "Linking the add-on to your Synork household.",
        "step.done.title": "All set",
        "step.done.body": (
            "This add-on is now paired with your Synork household. You can close this tab; "
            "the add-on will connect on its own. You'll find Synork Home in your sidebar."
        ),
        "step.done.cta": "Close",
        "step.done.household": "Household",
        "step.done.device": "Device ID",
        "already_paired.title": "Already set up",
        "already_paired.body": (
            "This add-on is paired with a Synork household. "
            "Use the Synork Home panel in the sidebar to manage your home."
        ),
        "already_paired.household": "Paired with",
        "error.unknown": "Something went wrong. Please try again, and check the add-on log if it persists.",
    },
    "hu": {
        "title": "Synork Home beállítás",
        # TODO[hu]: native Hungarian wording — placeholders mirror EN for now.
        "step.welcome.title": "Üdv a Synork Home-ban",
        "step.welcome.body": "TODO[hu]: brief welcome text in Hungarian.",
        "step.welcome.cta": "Kezdés",
        "step.signin.title": "Bejelentkezés a Synork-ba",
        "step.signin.body": "TODO[hu]: explain credentials sent to api.synork.dev, not stored locally.",
        "step.signin.email": "E-mail",
        "step.signin.password": "Jelszó",
        "step.signin.cta": "Bejelentkezés",
        "step.signin.error.invalid": "TODO[hu]: invalid credentials.",
        "step.signin.error.network": "TODO[hu]: cannot reach Synork.",
        "step.signin.error.tfa": "TODO[hu]: 2FA not supported in wizard yet.",
        "step.household.title": "Válassz háztartást",
        "step.household.body": "TODO[hu]: pick household or create new.",
        "step.household.create": "TODO[hu]: create new household.",
        "step.household.create.placeholder": "TODO[hu]: household name placeholder.",
        "step.household.location.label": "TODO[hu]: location label.",
        "step.household.location.placeholder": "TODO[hu]: e.g. living room.",
        "step.household.cta": "Párosítás",
        "step.pairing.title": "Párosítás folyamatban…",
        "step.pairing.body": "TODO[hu]: linking message.",
        "step.done.title": "Készen van",
        "step.done.body": "TODO[hu]: done message — addon paired, panel in sidebar.",
        "step.done.cta": "Bezárás",
        "step.done.household": "Háztartás",
        "step.done.device": "Eszközazonosító",
        "already_paired.title": "Már be van állítva",
        "already_paired.body": "TODO[hu]: already paired message; point to sidebar panel.",
        "already_paired.household": "Párosítva ezzel:",
        "error.unknown": "TODO[hu]: generic error.",
    },
}


def get_strings(language: str) -> dict:
    """Return the strings dict for a language, falling back to English."""
    return STRINGS.get(language, STRINGS["en"])
