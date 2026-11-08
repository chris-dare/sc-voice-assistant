from app.speech import normalize, render, split_sentences


def test_ussd_and_data_and_cedis():
    out = normalize("Buy 5GB for GH₵10.50 by dialling *138#")
    assert out == "Buy 5 gigabytes for 10 cedis 50 pesewas by dialling star one three eight hash"


def test_phone_mask_is_spoken_as_pattern():
    out = normalize("Enter your number as 024XXXXXXX")
    assert out == "Enter your number as zero two four followed by 7 digits"


def test_phone_number_digit_by_digit():
    assert normalize("Call 0244 300 000") == "Call zero two four four three zero zero zero zero zero"


def test_links_and_markdown_are_not_read_out():
    out = normalize("See [Roaming rates](https://mtn.com.gh/roaming) or https://mtn.com.gh/x/. **Thanks**")
    assert "http" not in out and "**" not in out
    assert "Roaming rates, linked on screen" in out
    assert "the link on screen." in out


def test_bullets_become_sentences():
    out = normalize("To activate:\n- Dial *135#\n- Restart your phone")
    assert out == "To activate: Dial star one three five hash. Restart your phone"


def test_quick_replies_are_offered_as_options():
    content = {"type": "text", "text": "Pick one", "actions": [{"type": "reply", "text": t} for t in "ABCD"]}
    assert render(content) == "Pick one. You can say A, B or C. More options are on screen."


def test_carousel_reads_first_titles():
    content = {"type": "carousel", "items": [{"title": "1GB"}, {"title": "5GB"}]}
    assert render(content) == "I found 2 options: 1 gigabytes or 5 gigabytes."


def test_split_sentences_keeps_order():
    text = "One. Two is longer than the first sentence by quite a bit, really. Three!"
    parts = split_sentences(text)
    assert " ".join(parts) == text
