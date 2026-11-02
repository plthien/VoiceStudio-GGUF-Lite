"""Pure parser regressions, including no-tag pass-through and hostile near-matches."""
import pytest
from lite.pause import parse_pause_script,PauseError,SpeechPart,PausePart,speech_text

@pytest.mark.parametrize("marker,ms",[
    ("[pause]",350),("[PAUSE]",350),("[pause 200ms]",200),
    ("[pause 200 ms]",200),("[pause 1s]",1000),("[pause 1 s]",1000),
    ("[PaUsE 1.5S]",1500),("[pause 3s]",3000),("[ pause\t1.500 s ]",1500),
    ("[pause 0.001s]",1),("[pause 10000ms]",10000),
])
def test_grammar(marker,ms):
    s=parse_pause_script("hé! "+marker+" 世界.")
    assert s.pause_frames==ms*24
    assert s.parts==(SpeechPart("hé!"),PausePart(ms),SpeechPart("世界."))
    assert speech_text(s,"")=="hé! 世界."

@pytest.mark.parametrize("bad",[
    "[pause 0s]","[pause -1s]","[pause 11s]","[pause 10.001s]",
    "[pause 1.1111s]","[pause .5s]","[pause 1e2s]",
    "[pause 1.1ms]","[pause nan s]","[pause infs]",
    "[pause 200]","[pause 1minute]","[pause 1s","[pause",
    "[pause\n2s]","[pause 1sec]","[pause 1s extra]",
    "[pause 200 m s]","[pause 1,5s]"
])
def test_invalid(bad):
    with pytest.raises(PauseError) as exc:
        parse_pause_script("Hi "+bad+" there")
    assert exc.value.code=="INVALID_PAUSE_MARKER"

def test_boundaries_and_adjacent():
    s=parse_pause_script("[pause][pause 500ms]Hi![pause 1.5s]")
    assert s.pause_count==3 and s.pause_frames==2350*24
    assert s.speech_segments==1
    assert s.parts[0]==PausePart(350) and s.parts[1]==PausePart(500)
    assert s.parts[-1]==PausePart(1500)

def test_marker_and_total_caps():
    with pytest.raises(PauseError,match="PAUSE_LIMIT_EXCEEDED"):
        parse_pause_script("hello"+"[pause]"*17)
    with pytest.raises(PauseError,match="PAUSE_LIMIT_EXCEEDED"):
        parse_pause_script("hello"+"[pause 10s]"*7)

def test_pause_only_rejected():
    with pytest.raises(PauseError,match="INVALID_REQUEST"):
        parse_pause_script("[pause 2s] [pause]")

def test_no_tag_is_none_and_other_tags_are_untouched():
    for value in ("He [laughs] loudly","[paused] hello","[pauze 1s]","Plain narration"):
        assert parse_pause_script(value) is None

def test_preserves_internal_unicode_and_punctuation():
    value="  Café\tnoir! [pause 800ms] こんにちは、 世界 [pause] 🤖?"
    s=parse_pause_script(value)
    assert [x.text for x in s.parts if isinstance(x,SpeechPart)]==["Café\tnoir!","こんにちは、 世界","🤖?"]

def test_hostile_oversized_numeric_literals_have_typed_error():
    for tag in ("[pause "+"9"*4000+"ms]","[pause "+"9"*4000+"s]"):
        with pytest.raises(PauseError,match="INVALID_PAUSE_MARKER"):
            parse_pause_script("A"+tag+"B")
