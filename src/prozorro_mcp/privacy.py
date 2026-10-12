"""Drop named contact persons from Prozorro documents before they are bundled or shown."""

CONTACT_KEYS = frozenset({"contactPoint", "additionalContactPoints", "signerInfo"})


def strip_contacts(data):
    if isinstance(data, dict):
        return {k: strip_contacts(v) for k, v in data.items() if k not in CONTACT_KEYS}
    if isinstance(data, list):
        return [strip_contacts(v) for v in data]
    return data
