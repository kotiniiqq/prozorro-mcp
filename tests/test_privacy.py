from prozorro_mcp.privacy import strip_contacts


def test_contact_persons_are_removed_at_any_depth():
    data = {"procuringEntity": {"name": "X", "contactPoint": {"name": "Іван", "telephone": "+380"},
                                "additionalContactPoints": [{"name": "Петро"}]},
            "bids": [{"tenderers": [{"name": "ТОВ", "contactPoint": {"email": "a@b.c"},
                                     "signerInfo": {"name": "Директор"}}]}]}
    out = strip_contacts(data)
    text = repr(out)
    assert "Іван" not in text and "Петро" not in text and "a@b.c" not in text and "Директор" not in text
    assert out["procuringEntity"]["name"] == "X" and out["bids"][0]["tenderers"][0]["name"] == "ТОВ"
    assert "contactPoint" in data["procuringEntity"]  # input untouched
