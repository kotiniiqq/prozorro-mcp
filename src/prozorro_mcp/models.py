"""Shared constants: which procedures count as competitive, and tender links."""

COMPETITIVE = frozenset({
    "aboveThreshold", "aboveThresholdEU", "aboveThresholdUA", "aboveThresholdUA.defense",
    "belowThreshold", "priceQuotation",
    "competitiveDialogueUA", "competitiveDialogueEU",
    "competitiveDialogueUA.stage2", "competitiveDialogueEU.stage2",
    "esco", "closeFrameworkAgreementUA", "closeFrameworkAgreementSelectionUA", "simple.defense",
})

OPEN_STATUSES = ("active.tendering", "active.enquiries")


def tender_url(tender_id: str) -> str:
    return f"https://prozorro.gov.ua/tender/{tender_id}"
