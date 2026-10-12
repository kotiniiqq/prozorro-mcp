"""Prozorro-shaped tenders and plans for tests. Shapes follow the public API (checked 2026-10-12)."""
import itertools
from datetime import datetime, timedelta

_seq = itertools.count(1)


def _party(edrpou, name, region="Львівська область"):
    return {"name": name, "identifier": {"scheme": "UA-EDR", "id": edrpou, "legalName": name},
            "address": {"region": region, "countryName": "Україна"}}


def make_tender(*, id=None, method="aboveThreshold", status="complete", title="Закупівля борошна",
                buyer=("11111111", "КНП Лікарня №1"), region="Львівська область", amount=100000.0,
                items=(("15610000-7", "Борошно пшеничне", 400, "KGM"),), bids=(), winner=0,
                award_amount=None, created="2026-10-01T10:00:00+03:00", tender_days=10,
                documents=(), lots=None, extra=None):
    """bids: (edrpou, name, amount) tuples; winner: index into bids or None; items: (cpv, desc, qty, unit)."""
    n = next(_seq)
    tid = id or f"{n:032x}"
    start = datetime.fromisoformat(created)
    tender = {
        "id": tid,
        "tenderID": f"UA-2026-10-01-{n:06d}-a",
        "title": title,
        "status": status,
        "procurementMethodType": method,
        "dateCreated": created,
        "date": created,
        "dateModified": created,
        "procuringEntity": _party(*buyer, region=region),
        "value": {"amount": amount, "currency": "UAH", "valueAddedTaxIncluded": True},
        "tenderPeriod": {"startDate": start.isoformat(),
                         "endDate": (start + timedelta(days=tender_days)).isoformat()},
        "items": [{"id": f"i{k}", "description": desc,
                   "classification": {"scheme": "ДК021", "id": cpv, "description": desc},
                   "quantity": qty, "unit": {"code": unit, "name": unit}}
                  for k, (cpv, desc, qty, unit) in enumerate(items)],
        "documents": [{"title": t, "documentType": dt, "url": f"https://public-docs.prozorro.gov.ua/{t}",
                       "datePublished": created} for t, dt in documents],
    }
    if bids:
        tender["bids"] = [{"id": f"b{k}", "status": "active", "tenderers": [_party(e, nm)],
                           "value": {"amount": a, "currency": "UAH"}}
                          for k, (e, nm, a) in enumerate(bids)]
    if winner is not None and bids:
        e, nm, a = bids[winner]
        tender["awards"] = [{"id": "a0", "bid_id": f"b{winner}", "status": "active", "date": created,
                             "suppliers": [_party(e, nm)],
                             "value": {"amount": award_amount if award_amount is not None else a,
                                       "currency": "UAH"}}]
    elif not bids and award_amount is not None:
        tender["awards"] = [{"id": "a0", "status": "active", "date": created,
                             "suppliers": [_party("99999999", "ТОВ Постачальник")],
                             "value": {"amount": award_amount, "currency": "UAH"}}]
    if lots:
        tender["lots"] = lots
    if extra:
        tender.update(extra)
    return tender


def make_plan(*, id=None, buyer=("11111111", "КНП Лікарня №1"), region="Львівська область",
              cpv="33696500-0", description="Реагенти лабораторні", budget=250000.0,
              tender_start="2026-11-01T00:00:00+02:00", status="scheduled", tender_id=None,
              modified="2026-10-01T10:00:00+03:00"):
    n = next(_seq)
    plan = {
        "id": id or f"p{n:031x}",
        "planID": f"UA-P-2026-10-01-{n:06d}-a",
        "status": status,
        "dateModified": modified,
        "procuringEntity": _party(*buyer, region=region),
        "classification": {"scheme": "ДК021", "id": cpv, "description": description},
        "budget": {"amount": budget, "currency": "UAH", "description": description},
        "tender": {"procurementMethodType": "aboveThreshold", "tenderPeriod": {"startDate": tender_start}},
    }
    if tender_id:
        plan["tender_id"] = tender_id
    return plan
