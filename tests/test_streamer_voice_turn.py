"""V7.30 S1 streamer surface test: chat_input/chat_reply expose turn_id.

Pure source-text checks — streamer_app.py imports cv2 + heavy deps that
don't load in stripped CI env.
"""
import os

STREAMER = os.path.join(os.path.dirname(__file__), "..", "streamer_app.py")
INDEX_HTML = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_read_voice_turn_helper_exists():
    src = _read(STREAMER)
    assert "def _read_voice_turn():" in src
    assert "/dev/shm/voice_turn.json" in src


def test_chat_input_endpoint_emits_turn_id():
    src = _read(STREAMER)
    idx = src.find("def get_chat_input():")
    end = src.find("def ", idx + 1)
    body = src[idx:end]
    assert "_read_voice_turn()" in body
    assert "turn_id" in body
    assert "stage" in body


def test_chat_reply_endpoint_emits_turn_id():
    src = _read(STREAMER)
    idx = src.find("def chat_reply():")
    end = src.find("def ", idx + 1)
    body = src[idx:end]
    assert "_read_voice_turn()" in body
    assert "turn_id" in body


def test_voice_turn_route_registered():
    src = _read(STREAMER)
    assert "@app.route('/api/voice_turn')" in src
    assert "def get_voice_turn():" in src


def test_index_html_tracks_turn_id_state():
    html = _read(INDEX_HTML)
    assert "let lastTurnId" in html
    assert "currentUserBubble" in html
    assert "currentReplyBubble" in html


def test_index_html_has_update_in_place_helper():
    html = _read(INDEX_HTML)
    assert "function updateChatBubbleText" in html


def test_index_html_resets_refs_on_turn_rotation():
    html = _read(INDEX_HTML)
    assert "incomingTurn !== lastTurnId" in html
    assert "currentUserBubble = null" in html
    assert "currentReplyBubble = null" in html
